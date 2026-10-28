# AGENTS.md: how coax works

A map of coax for anyone changing it. Read [CLAUDE.md](CLAUDE.md) first for the history, the owner's rules and
the mistakes not to repeat. The README is the user guide; this file is the engineering guide.

## What coax is

A self-hosted Docker appliance that sits between IPTV providers (Xtream Codes or M3U) and Channels DVR. It gives
Channels DVR one steady MPEG-TS tuner stream per channel, M3U lineups with Gracenote station IDs, and XMLTV.
It hides provider stalls with a reserve, fails over between streams, checks backups before switching, and keeps
each channel's streams in their most stable order (Smart Standby).

Stack: Python 3.13, stdlib HTTP server, `requests`, `cryptography` (AES HLS), Jellyfin ffmpeg 8.1.3, vanilla JS
web UI with no build step. All state is one folder, `/data`. Runs as uid 1000. amd64 only.

## Layout

```
app/
  main.py            entry: load config, start hub, feed health, DVR sync, backups, cleanup, scheduler, HTTP
  config.py          /data/config.json: defaults, migration, validation, atomic writes, ConfigStore.update(mutate)
  server.py          HTTP: /stream, /output (M3U, XMLTV), /internal (engines), /api (UI), static web UI
  hub.py             one DVR connection = one Session = one engine process = one line slot; recovery
  feedhealth.py      Smart Standby: problems, blocks, ranking, promotion, the bell (notifications)
  channels_dvr.py    one-click Add to Channels DVR per lineup; debounced automatic source and guide sync
  backups.py         backup stream suggestions (name, call sign, tvg-id matching across selected groups)
  snapshots.py       backups of /data as .tar.gz (daily, manual, restore); old .json backups still restore
  cleanup.py         daily removal of stale engine files and orphaned caches
  names.py           strips provider prefixes (US|, [US], NFL| ...) from names; always on
  tz.py              time zone (Settings, else TZ, else UTC)
  engine/
    hlsloop.py       stream engine for HLS input (one process per Session)
    tsloop.py        stream engine for MPEG-TS input
    avcheck.py       content sync check: is a feed's sound off its picture compared with another feed?
  guide/
    gracenote.py     station search and matching through Channels DVR's Gracenote data; region lineup index
    xmltv.py         download, parse and merge XMLTV sources; picker ids (every display name kept)
    output.py        M3U per lineup
  providers/         xtream.py, m3u.py (streaming parser), catalog.py (cache), scheduler.py, net.py
  web/               index.html, app.js (shell, router, bell, IP pill), api.js (every route), ui.js, state.js,
                     popover.js, app.css, pages/{channels,status,lineups,providers,settings,logs}.js
tests/               unittest suite (fake engine in tests/fixtures/hub/fake_engine.py)
reference/           the original matcher and backup scripts from channels-dvr-iptv-tools (ported, not used)
scripts/make_logo.py brand asset generator
.github/workflows/   tests.yml (on push), image.yml (manual dispatch only: version + tag, amd64)
```

## Data

`/data/config.json` (mode 0600, holds passwords) is the only source of truth for settings. Keys: `server`
(port, base_url), `channels_dvr.url`, `transcode` (`off` or a preset like `1080p60`), `providers` (each with
`lines`, `groups`, `format` hls|ts, `max_streams`), `channels` (id, uuid, number, name, group, logo, enabled,
`streams` = ordered list of `{provider, stream_id}`, `guide` = mode/station_id/xmltv_id/...), `lineups`
(id, name, guide source, channel ids, sort, start_number, DVR options), `xmltv_sources`, `gracenote.regions`,
`backups`, `autosave_minutes`, `timezone`.

Always change config through `ConfigStore.update(mutate)`: it deep-copies, validates, writes atomically and calls
listeners (time zone, DVR sync). Never write the file directly.

Other files: `config.json.bak` (previous), `backups/`, `notifications.json` (bell), `cache/` (catalogs, XMLTV,
Gracenote index, `feedhealth.json`, `history/<channel>.jsonl`, recording job files). Engines write state files in
`/tmp` (`hlsloop-<md5>.json`, rewritten every second).

## Request flow

1. Channels DVR reads `/output/m3u/<lineup>.m3u` (stream URLs `/stream/<uuid>.ts`, Gracenote ids, logos) and
   `/output/xmltv/<lineup>.xml`.
2. A tune hits `/stream/<uuid>.ts` (`server.h_stream`). `Hub.open()` picks the channel's streams in order
   (blocked streams last), takes the first line with a free slot (per-line and provider-wide limits; a line
   released in the last 5 s is used last) and returns a `Session`, or 503.
3. `Session.iter_bytes()` spawns the engine (`hlsloop.py` or `tsloop.py <url> <ua> <channel_id>`, environment
   `COAX_PORT`, `COAX_SESSION`, `COAX_RESTART`) and copies its stdout to the socket, flushing every chunk.
4. The engine logs to stderr. `Session._watch` parses those lines into the Status timeline, the speed/transcode
   pills, history, and Smart Standby signals.
5. The engine calls back into coax on 127.0.0.1 only: `/internal/channels/<id>/streams` (failover list, from
   `Hub.backup_urls`: playing stream first, then by Smart Standby rank, blocked streams left out while any other
   remains) and `/internal/channels/<id>` (uuid for the DVR viewer buffer).
6. On disconnect, `Session.close()` kills the engine's whole process group, reaps it, then releases the slot.

## The hub (app/hub.py)

Follows ah4c: no fan-out (Channels DVR shares its own tuner), kill then release. It never ends a stream on its
own while the DVR is connected:
- Engine exits: restart on the same line, resuming from its state file (`COAX_RESTART=1` trusts it for 60 s).
- Frozen (no bytes for 25 s after video flowed): kill and restart.
- No real video for 90 s (only null packets): retune; every second retune moves to another free line of the
  provider (`_move_line`, which resets backup slots and the playing stream).
- Between engines the hub sends MPEG-TS null packets (only after video has flowed, only on a 188-byte boundary)
  so the DVR keeps the connection. Backoff doubles to 15 s.
- While idle it checks whether the DVR closed the socket (`client_gone`, set by the server), so a slot is never
  held for a client that left.
- Engine log lines carry `[ref xxxxxxxx]` (md5 of the stream URL). The hub maps refs back to (provider, line,
  stream) to follow switches (`_follow`: holds another provider's line in `extra_slots`), mark failures
  (`_note_bad`), and report sync verdicts (`_desync`).
- Status for the UI: `Hub.status()` (live reserve read from the engine state file), `line_status()`.
- **Engine log wording is an API.** `PROBLEMS` in `hub.py` (for example `r"offline placeholder"` ->
  `placeholder`, an instant block) matches any engine line containing the phrase, informational or not, and
  so do the Status timeline patterns. Grep them before adding or rewording an engine log line; an
  informational line once blocked a good stream for 24 h.

## The engines (app/engine/)

Single-file programs ported from the Dispatcharr stream profiles; they keep that style (short names, globals,
threads). `hlsloop.py` is the recommended path.

- **Reserve:** HLS joins about 60 s behind live and sends about 4 s ahead to the DVR; MPEG-TS has about 20 s from
  the provider's connect burst. Output is paced by video timestamps.
- **Remuxer / transcoder:** one ffmpeg reads the engine's queue on stdin and writes MPEG-TS to stdout, in both
  modes (ffmpeg is always in the chain). `remux()` restarts it with `-output_ts_offset` so the output timeline
  is continuous across switches, provider restarts and jumps. The command is built in `ffcmd()` (same shape in
  both engines). **Passthrough** (that is its name; do not rename it to "remux") is `-c copy -copyts`, no GPU.
  Transcoding: whole chain on the GPU (VAAPI `-hwaccel vaapi`, `deinterlace_vaapi`,
  `scale_vaapi`, `h264_vaapi`; NVIDIA CUDA decode, `bwdif_cuda`, `scale_cuda`, `h264_nvenc`), falling back to CPU
  decode (`GPUDEC`) if GPU decode fails; audio always AAC 48 kHz stereo 192k.
- **ffmpeg flags that must stay** (pinned by `FfmpegArgsTest` in `tests/test_engine_adapt.py`):
  - `-probesize 5000000 -analyzeduration 5000000` right before `-i pipe:0`, both modes. HE-AAC (SBR) only
    shows its real format after decoding; a 1 s probe made ffmpeg mux before it understood the audio and the
    sound was visibly out of lip sync on the TV while every timestamp check said in sync.
  - `-muxdelay 0 -muxpreload 0` right before `-f mpegts pipe:1`, both modes (part of the same fix; which of
    the two did the work is not isolated, keep both).
  - `-dts_delta_threshold 86400`: never let ffmpeg rewrite timestamps after a jump (a 10 s audio dropout
    became a permanent offset); the engine continues the timeline itself.
  - Transcode audio `aresample=async=1:min_hard_comp=0.1:first_pts=0` (plus `atempo` in a refill): corrects
    only real gaps above 0.1 s. Never `async=1000`, which stretches audio to fit timestamps (5 s off offline).
  - Any change here: run the candidate command for a few seconds offline on real data of the affected feed
    before it reaches a live server (`-map 0:a:0` with a short probe crash-looped a live stream).
- **Encoder restarts** (refill edges and stream switches with transcoding on) wait for the next keyframe
  (`kfo`, `iskey` reads H.264/HEVC NAL units itself; providers often do not mark keyframes) and correct the
  audio lead (`avlead`, `eofs`), so audio and video start together.
- **Refill:** below half reserve for 75 s (HLS) or 60 s (TS), outside the first 90 s and at least 60 s after the
  last edge, play 3% slower (video `setpts`, audio `atempo`) until three quarters full. `rstep()`.
- **Keepalive:** null packets when no video for 2 s (HLS) or 3 s (TS); stopped on purpose when the fetch loop is
  stuck so the hub restarts the engine.
- **Failover:** `handoff(why)` takes `feeds()` (coax's order), skips streams marked bad (`fbad`, 60 s doubling to
  10 min, 24 h for out-of-sync), probes candidates and switches, joining the backup at the outage depth.
- **A/V hold:** a feed whose own audio/video timestamps are more than 3 s apart is held back (`PSK`). There is
  no A/V timestamp monitor in the engines (one was tried in 1.0.4 debugging and removed: timestamps were
  consistent while the picture and sound on the TV were not).
- **Ended playlist (HLS, `PHG = [first_seen, logged]`):** a playlist with `#EXT-X-ENDLIST` (often right after
  407 proxy errors) does not switch while the reserve (`Qs[0] + wlead`) is above 15 s and the loop is not
  stalled; the engine polls every 2 s and logs "provider sent an ended playlist; staying on this stream while
  the reserve lasts". When the reserve runs low it hands off: "provider is serving an offline placeholder"
  (blocks the stream) only if the ended playlist persisted 60 s, else "provider sent an ended playlist and the
  reserve ran low" (moves playback, blocks nothing). TS detects the placeholder by a redirect to `black.*`.
- **Viewer buffer:** polls Channels DVR `/dvr/activity`; a joining device gets a 5 s head start.
- **Recordings:** extends coax recordings in Channels DVR by the delay plus 45 s (max 3 min); job files in
  `cache/*-jobs-<channel>.json`.
- **10-minute status line:** `status: ... reserve Ns ...; last 10 min: N playlist retries, N catch-ups,
  N switches, Ns keepalive`. The hub saves it to history and uses it for credit and the Unsteady badge.

### Sync check (content, not timestamps)

`avcheck.py offset(ref, cand)` returns `offset` = (sound lag) minus (picture lag), or None with a reason when the
match is not clear. 2 s or more off means the feed's sound is off its picture. It needs two feeds of the same
channel covering the same moments; one feed alone cannot be judged.

1. **Sound first, by audio fingerprint:** ffmpeg's chromaprint muxer (`-f chromaprint -fp_format raw`, 32-bit
   items every `HOP = 4096/3/11025` s) on each clip; the best lag within `MAXLAG` (90 s) needs a bit error of at
   most 0.30 and a margin of at least 0.08 over the runner-up more than 1 s away, else "sound does not match
   clearly" (no verdict).
2. **Then the picture,** a 20 Hz picture-change signal, searched only within `PICWIN` (40 s) of where the sound
   placed the program; correlation at least 0.45 and 0.15 above the runner-up, else no verdict.

No verdict means no action. The old method (sound-energy envelope vs picture change over +/-90 s) gave noise
readings (65.9 s, -50 s, +63 s) that blocked good streams; the MPEG-7 `signature` filter never matches across
different resolutions or frame rates. Both ffmpeg pieces exist in the image's Jellyfin ffmpeg. Verified: an
HE-AAC backup read -0.1 s in 3.7 s (picture 0.93 vs 0.37, sound bit error 0.09).

- **HLS, before a switch** (`scan` / `scanrun`): at the first real trouble (2+ playlist errors, a stalled
  playlist, reserve empty), in a background thread, fetch each other stream's last 60 s (`grabwin`, not played,
  skipping streams on the line in use) and compare with the last 60 s of the trusted stream (`SYNC["ref"]`).
  In step: `TRUST`. Off: `SCANBAD`, blocked, never switched to. `handoff` then prefers coax's order without
  `SCANBAD` and holds rather than switch to a bad one (falls back after 2 minutes with nothing else).
- **HLS and MPEG-TS, after a switch** (`syncswitch` / `synckeep` / `synccheck`): the new stream's first
  35 s (HLS, plus pre-fetched overlapping segments) or 25 s (TS) against the old stream's end. Off: leave it
  (`SYNCBAD`) and block it. On TS this is the only content check, and it has no verdict when the clips do not
  overlap.
- Verdicts are logged as `sync check: stream N is in step ... [ref]` or `... has its sound Xs ahead of its
  picture; ... [ref]` and reach feed health through the hub.

## Smart Standby (app/feedhealth.py)

Keeps each channel's saved stream order stable and good, acting only on real trouble.

- `problem(ch, key, kind)`: `desync` and `placeholder` block at once (`INSTANT`; so the engines only emit them
  on a clear sync verdict or a 60 s ended playlist); `frozen`/`novideo` block on the 3rd within
  an hour; `dead` (a stream failed and another took over, confirmed by `switched()`) blocks on the 3rd within
  6 hours. Absorbed problems (stalls, keepalive) are never blocking.
- Blocks: 12 h, 24 h, 3 days within a week (desync 24 h, 3 days). Never deleted. `is_blocked()` is used by the
  hub at tune time and in the failover list. If all streams are blocked, the least bad still plays.
- On a block, `_fix_order` moves the stream last; if it was main, the proven stream with the most clean playback
  becomes main. Proven = passed a sync check within a week (`verified`) or 10+ clean minutes (`good`, credited
  every 10 clean minutes). An unproven stream never becomes main.
- After trouble and a switch, `_try_promote` moves the stream that proves stable ahead of the troubled one
  (main if needed). No other code moves the order back.
- `rank()` = (blocked, weighted fading problems minus clean minutes); desync weighs most. Used by `backup_urls`.
- Every change goes to the bell (`notifications.json`, max 200) with Undo (restores the old order and lifts the
  block; refuses with 409 if the channel's streams changed), dismiss and clear. Messages are plain English with
  the provider's name.

## Channels DVR integration (app/channels_dvr.py)

`add_or_update` creates or updates the lineup's custom M3U source (`PUT /providers/m3u/sources/<key>`), refreshes
it and downloads the guide. `Syncer` listens to config changes, debounces 3 s, and refreshes every added lineup's
source (and guide when channels or guide ids changed). Sources must never need a manual Refresh in Channels DVR.

## Web UI (app/web)

No framework, no build. `app.js` routes `#/<page>` and lazy-imports `pages/*.js`; each page exports
`mount(el)` and `unmount()`. `api.js` holds every route. Patterns to keep:
- Channels page state `S`; Auto-match results are applied once per job (`sessionStorage coax.amSeen`).
  Suggestions are saved with `/api/channels/apply` using `add_streams` (append to the server's current list,
  never send a stale full list). Unsaved suggestions are mirrored in `localStorage coax.unsaved` until saved.
  Autosave (Settings, default 2 min) never runs while a dialog is open.
- Status cards are keyed per session (`uuid~instance`) and updated in place (rebuilding flashes).
- Popovers via `data-pop` / `data-pop-sub`. Mono inputs disable ligatures (the `http://` slash bug).
- The bell polls `/api/notifications` every 15 s.

## Build, test, release

- Tests: on Gemini, `cd ~/src/coax && .venv/bin/python -m unittest discover tests` (278, about 30 s).
- UI checks: Gemini has no Node. Copy `app/web` to the Hermes container and drive the real page modules with
  jsdom against a fake `fetch` (set `"type": "module"` in a package.json next to them; stub `scrollTo`,
  `location`, `CSS.escape`).
- Image: `Dockerfile` (python:3.13-slim-trixie by digest, Debian snapshot, Jellyfin ffmpeg pinned by SHA-256; the
  build fails without `h264_vaapi`, `h264_nvenc` and the VAAPI/CUDA filters).
- Release: `gh workflow run image.yml -f version=X -f tag=latest`; it tests, smoke-runs the image, then pushes
  `ghcr.io/mackid1993/coax:X` and `:latest` (amd64).
- Compose: `docker-compose.yml` and `docker-compose.vpn.yml` need no editing; every setting is a
  `${VAR:-default}` read from `.env` (`TAG`, `CONTAINER_NAME`, `HOST_PORT`, `HOST_DIR` (coax appends `/data`), `TZ`, `GPU_DEVICE`,
  `DOCKER_RUNTIME`, plus the common Gluetun values; `LAN_SUBNETS` and `VPN_SERVICE_PROVIDER` are required). The README embeds these files and the
  `.env` examples verbatim in collapsed blocks; change them together.
- Deploy on Apollo (only when the owner agrees): back up `config.json`, then in
  `/boot/config/plugins/compose.manager/projects/coax`: `docker compose -p coax -f compose.yaml -f
  compose.override.yaml pull coax && ... up -d --no-deps coax`.
- Testing a failover live: block one stream inside the coax network namespace with an iptables string match on
  `/<stream_id>` (REJECT tcp-reset), always remove it afterwards, and never test on a channel the owner is
  watching without asking. Prefer reorder plus retune for forced tests; never block the stream the owner is
  watching; restore channel order and blocks afterwards; back up `config.json` first.
- A/V sync work: the owner's eyes on the TV are the ground truth, not timestamps. Iterate with live candidates
  he watches, one change at a time, each run offline for a few seconds on real feed data first. After a live
  hot-swap of engine code, check for undefined names before he tunes.

## Invariants

- One DVR connection, one engine, one slot. Kill the engine before releasing the slot.
- Never two connections on one provider line. Probes use other lines or the DVR's shared tuner.
- Never show a stream known to be out of sync when another stream works.
- Never promote an unchecked stream; never move the order without real trouble; never rubber-band.
- Never delete a user's stream; block it.
- Config changes only through `ConfigStore.update`.
- Every exception in background threads is caught; coax never crashes from feed health, history, cleanup or sync.
- One noisy reading never blocks: unclear checks return no verdict.
- Informational engine log lines never contain hub trigger phrases.
- ffmpeg always probes 5 s and adds no mux delay; passthrough is called passthrough.
