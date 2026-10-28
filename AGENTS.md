# AGENTS.md: how coax works

A map of coax for anyone changing it. Read [CLAUDE.md](CLAUDE.md) first for the owner's rules, the settled
decisions and the mistakes not to repeat; [HISTORY.md](HISTORY.md) holds the full record behind it.
The README is the user guide; this file is the engineering guide.

## What coax is

A self-hosted Docker appliance that sits between IPTV providers (Xtream Codes or M3U) and Channels DVR. It gives
Channels DVR one steady MPEG-TS tuner stream per channel, M3U lineups with Gracenote station IDs, and XMLTV.
It hides provider stalls with a reserve, fails over between streams, confirms a backup answers before switching,
and keeps each channel's streams in their most stable order (Smart Standby). It does not judge a feed's sound
against its picture; feeds that are out of sync are disabled by hand.

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
    fmtprobe.py      reads the provider's format (codecs, resolution, frame rate, bitrates) for the Status card
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
`streams` = ordered list of `{provider, stream_id}`, optionally `"disabled": true`, `guide` =
mode/station_id/xmltv_id/...), `lineups`
(id, name, guide source, channel ids, sort, start_number, DVR options), `xmltv_sources`, `gracenote.regions`,
`backups`, `autosave_minutes`, `timezone`.

Always change config through `ConfigStore.update(mutate)`: it deep-copies, validates, writes atomically and calls
listeners (time zone, DVR sync). Never write the file directly.

Other files: `config.json.bak` (previous), `backups/`, `notifications.json` (bell), `cache/` (catalogs, XMLTV,
Gracenote index, `feedhealth.json`, `history/<channel>.jsonl`, recording job files). Engines write state files in
`/tmp` (`hlsloop-<md5>.json`, rewritten at most 5 times a second while playing).

## Request flow

1. Channels DVR reads `/output/m3u/<lineup>.m3u` (stream URLs `/stream/<uuid>.ts`, Gracenote ids, logos) and
   `/output/xmltv/<lineup>.xml`.
2. A tune hits `/stream/<uuid>.ts` (`server.h_stream`). `Hub.open()` picks the channel's streams in order
   (`Hub._candidates` skips disabled refs; blocked streams last), takes the first line with a free slot
   (per-line and provider-wide limits; a line released in the last 5 s is used last) and returns a `Session`, or 503.
3. `Session.iter_bytes()` spawns the engine (`hlsloop.py` or `tsloop.py <url> <ua> <channel_id>`, environment
   `COAX_PORT`, `COAX_SESSION`, `COAX_RESTART`, `COAX_NEWSTREAM` after a hand switch) and copies its stdout to the
   socket, flushing every chunk.
4. The engine logs to stderr. `Session._watch` parses those lines into the Status timeline, the speed/transcode
   pills, history, and Smart Standby signals.
5. The engine calls back into coax on 127.0.0.1 only: `/internal/channels/<id>/streams` (failover list, from
   `Hub.backup_urls`: playing stream first, then by Smart Standby rank, disabled streams never, blocked streams
   left out while any other remains; `server.fallback_stream_urls` also skips disabled streams) and
   `/internal/channels/<id>` (uuid for the DVR viewer buffer).
6. On disconnect, `Session.close()` kills the engine's whole process group, reaps it, then releases the slot.

## The hub (app/hub.py)

Follows ah4c: no fan-out (Channels DVR shares its own tuner), kill then release. It never ends a stream on its
own while the DVR is connected:
- Engine exits: restart on the same line, resuming from its state file (`COAX_RESTART=1` trusts it for 60 s).
- Frozen (no bytes for 25 s after video flowed): kill and restart.
- No real video for 90 s (only null packets): retune; every second retune moves to another free line of the
  provider (`_move_line`, which drops backup slots and keeps the stream that is playing).
- Between engines the hub sends MPEG-TS null packets (only after video has flowed, only on a 188-byte boundary)
  so the DVR keeps the connection. Backoff doubles to 15 s.
- While idle it checks whether the DVR closed the socket (`client_gone`, set by the server), so a slot is never
  held for a client that left.
- Engine log lines carry `[ref xxxxxxxx]` (md5 of the stream URL). The hub maps refs back to (provider, line,
  stream) to follow switches (`_follow`: holds another provider's line in `extra_slots`) and mark failures
  (`_note_bad`). `_label` rewrites the engine's `stream N of M` (a position in its working list, which starts
  with the playing stream and omits disabled streams) to `stream <id>` in logged lines and timeline text; a
  failover reads "Switched to stream <id>".
- Status for the UI: `Hub.status()` (live reserve read from the engine state file; `feeds` per session from
  `_feeds_view` lists every stream of the channel with playing, disabled, `blocked`, `blocked_until`,
  `block_why`), `line_status()`. `_feeds_view` logs any failure with `log.exception` before returning `[]`
  (1.0.5-4: a reused variable once emptied the list silently); `FeedsViewTest` covers it.
- **Unblock.** `POST /api/channels/<id>/unblock {provider, stream_id}` -> `Hub.unblock` ->
  `FeedHealth.unblock` (ends the blocks, clears strikes, persists) and clears `hub._bad` for the stream;
  LookupError if it is not blocked.
- **Switch and Disable (manual).** `POST /api/channels/<id>/switch {instance, provider, stream_id}` calls
  `Hub.switch(channel_id, instance, provider, stream_id)`: it updates the session (`url`, `fmt`, `cur_key`,
  `stream_no`, and the line and slots when the provider changes) under the hub lock and the session lock (so
  the copy loop cannot start an engine on the old address meanwhile), sets `fresh`, then stops the engine with
  the session's `_kill` (the hub has no `_kill`; calling it was the 1.0.5-2 bug). Slots it gives up go to
  `drop_after_kill` and are released only after that kill (kill before release; `_release` drops them too).
  A line the session already holds for a backup on the target provider is reused. The copy loop respawns
  the engine at once (no crash backoff) with `COAX_NEWSTREAM=1`: both engines keep the output clock and
  continue the timeline from where the old engine's output ended (`pend`; hlsloop's `oend` includes a
  partly sent segment, tsloop restores `qend` from the old `prim`), and drop the old stream's segment state,
  failure clock (`rg`) and stuck flag (`rstk`). A fresh DVR connection (no `COAX_RESTART`) may resume the
  position from a state file under 4 s old but never its player lead, head start or stuck flag. `POST /api/channels/<id>/feed {provider, stream_id, disabled}` sets or clears `disabled` on
  the ref (400 if it would leave no enabled stream) and calls `Hub.feed_disabled`, which switches
  any session playing that stream to the next enabled candidate at once.
- **Credentials in logs.** `app/main.py` `_Redact` is a logging filter on the console and the Logs page buffer;
  it runs `hub._redact`, which masks usernames and passwords in URL paths (including the short
  `/<user>/<pass>/<id>.ts` form) and queries, and every configured line login of 6+ characters
  (`_learn_secrets`, refreshed from `Hub._config`). Every log record goes through it.
- **Proxy errors.** Both engines note the last HTTP 407 (`PXE`, a response hook). A stream that fails, or a
  backup that does not answer, within 30 s of one is logged "... during provider proxy errors": the hub still
  tries it last for a while (`_bad`) but does not report `dead` to Smart Standby, since the whole line hiccuped.
- **Catalog and guide calls** (`player_api.php`, `xmltv.php`, Test) use an idle line: `xtream.first_line` asks
  `Hub.busy_lines` (wired in `main.py` through `xtream.BUSY_LINES`) and takes the last idle enabled line, the
  first line only when all are busy.
- **Catalog refresh.** An empty answer never replaces a catalog that had streams. M3U stream ids skip an id
  two entries could both claim; `catalog.diff` maps a vanished id to a new one by identical URL, or by the URL
  without token and credential parts when that URL is unique in both the whole old and the whole new catalog, and `App._apply_renamed` points
  channel refs at the new id.
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
- **ffmpeg flags** (pinned by `FfmpegArgsTest` and `DropChangedTest` in `tests/test_engine_adapt.py`):
  - Probe: the base `-probesize 1000000 -analyzeduration 1000000` (tsloop transcode adds 5 MB / 3 s). The
    1.0.4 5 s probe and `-muxdelay 0 -muxpreload 0` were removed in 1.0.5: they were believed to fix an HE-AAC
    lip-sync problem, but that feed was out of sync at the provider itself (see HISTORY.md 4.7). The test
    asserts no `-muxpreload` in `ffcmd()`. The restart timestamp shift (`tsfix.py`) and the keyframe-start
    filter are gone too.
  - `-dts_delta_threshold 86400`: never let ffmpeg rewrite timestamps after a jump (a 10 s audio dropout
    became a permanent offset); the engine continues the timeline itself.
  - Transcode audio `aresample=async=1:min_hard_comp=0.1` (plus `atempo` in a refill): corrects only real gaps
    above 0.1 s. Never `async=1000`, which stretches audio to fit timestamps (5 s off offline). No `first_pts`:
    when an audio parameter change makes ffmpeg rebuild the filter, `first_pts=0` inserts silence.
  - Transcode input `-drop_changed:a 1` (right before `-i pipe:0`): audio frames whose parameters change are
    dropped instead of rebuilding the filter. Guard: `silent(pid, b)` in both engines; if a transcode encoder
    sends video but no audio for 10 s, at least 15 s after it started, it is killed so it restarts with the
    new format. At most once per encoder, then again only after 1, 2, 4 ... 10 minutes while sound stays
    missing; never for a feed that has had no sound since the engine started. Its kills are kept in `SK`, so
    `remux()` never mistakes them for a GPU decoding failure (which would move decode to the CPU for good).
  - Any change here: run the candidate command for a few seconds offline on real data of the affected feed
    before it reaches a live server (`-map 0:a:0` with a short probe crash-looped a live stream).
- **Provider format:** `fmtprobe.py` reads the incoming stream once per stream and the engine logs
  `provider format: {json}`; the hub turns it into the card's "From provider" line.
- **Encoder restarts** (refill edges and stream switches with transcoding on) wait for the next keyframe
  (`kfo`, `iskey` reads H.264/HEVC NAL units itself; providers often do not mark keyframes) and correct the
  audio lead (`avlead`, `eofs`), so audio and video start together.
- **Refill:** below half reserve for 75 s (HLS) or 60 s (TS), outside the first 90 s and at least 60 s after the
  last edge, play 3% slower (video `setpts`, audio `atempo`) until three quarters full. `rstep()`.
- **Downloads (hlsloop):** every playlist, probe and segment is read through `grab()`, with an overall deadline
  (segments `max(3 * tgt, 30)` s, playlists 30 s, probes 6 s) and a 2 MB cap on playlists, and the response is
  always closed. A trickling provider then fails into the normal retry and failover path instead of holding
  the fetch loop, and a `.ts` URL where a playlist was expected is refused. A master playlist chain is
  followed at most 3 hops.
- **Keepalive:** null packets when no video for 2 s (HLS) or 3 s (TS); stopped on purpose when the fetch loop is
  stuck so the hub restarts the engine.
- **Failover:** `handoff(why)` takes `feeds()` (coax's order), skips streams marked bad (`fbad`, 60 s doubling to
  10 min), probes candidates (confirms they answer) and switches, joining the backup at the outage depth. A probe
  that gets a busy answer (429/503/509, `BZ`) does not mark the backup (up to three rounds in a row, `BZN`):
  the next backup is tried, and if only busy answers came back the next round is in about 5 s, not 30. hlsloop continues the output timeline (`pend`, `plost`) over video lost on the same stream: a skipped
  broken segment, or segments that left the playlist during an outage ("lost N segments ...").
  No new video for 60 s hands off, sooner when the reserve is nearly empty (HLS about three segments, TS about
  8 s). Slow feed: HLS below 0.75x while the reserve drains, TS below 0.85x with the reserve nearly empty, for
  5 minutes.
- **A/V hold:** a feed whose own audio/video timestamps are more than 3 s apart (`pts0` / `PSK` in hlsloop)
  keeps playing up to 30 s while the engine tries the next stream, then switches; tsloop passes such a stream
  over at connect. There is
  no A/V timestamp monitor in the engines (one was tried in 1.0.4 debugging and removed: timestamps were
  consistent while the picture and sound on the TV were not).
- **Ended playlist (HLS, `PHG = [first_seen, logged]`):** a playlist with `#EXT-X-ENDLIST` (often right after
  407 proxy errors) does not switch while the reserve (`Qs[0] + wlead`) is above 15 s and the loop is not
  stalled; the engine polls every 2 s and logs "provider sent an ended playlist; staying on this stream while
  the reserve lasts". When the reserve runs low it hands off: "provider is serving an offline placeholder"
  (blocks the stream) only if the ended playlist persisted 60 s, else "provider sent an ended playlist and the
  reserve ran low" (moves playback, blocks nothing). `PHG` is reset only by a normal playlist or an actual
  switch, so while no backup answers the minute keeps counting. TS detects the placeholder by a redirect to
  `black.*`.
- **TS short connections:** a connection that ends with under 15 s of video and under 60 s long counts in
  `short`; a longer connection or any other error resets it. Three in a row with reserve plus lead at 15 s or less hand off with
  `charge=False` (logged "failed after short connections", so the hub tries the stream last for a while but
  does not count it in Smart Standby) and reconnect at once, switched or not.
- **Stream format (HLS first):** `providers.stream_format` picks the engine. A `.m3u8` address is always HLS;
  an Xtream `/live/<u>/<p>/<id>` address (`.ts` or no extension) is HLS unless the provider is set to `ts`
  (hlsloop's `HSX` and `hub._HSX` rewrite it to `<id>.m3u8`); any other `.ts` address, and the short Xtream
  form `/<u>/<p>/<id>` without `/live/` or an extension, is TS (no HLS form); anything else follows the
  provider's setting.
- **Hand switch state:** on `COAX_NEWSTREAM` both engines also clear `rcur`, so the stream the hub chose plays,
  not the old engine's last failover stream. The silent-audio guard's "has had sound" flag resets on every
  stream switch.
- **TS transcode pacing:** like hlsloop, while the encoder's output has been silent for 0.7 s (`OWT`) the
  writer keeps feeding it (within the lead cap), because a GPU encoder holds a few seconds.
- **Viewer buffer:** polls Channels DVR `/dvr/activity`; a joining device gets a 5 s head start.
- **Recordings:** extends coax recordings in Channels DVR by the delay plus 45 s (max 3 min); job files in
  `cache/*-jobs-<channel>.json`.
- **10-minute status line:** `status: ... reserve Ns ...; last 10 min: N playlist retries, N catch-ups,
  N switches, Ns keepalive`. The hub saves it to history and uses it for credit and the Unsteady badge.

### Sound off its picture: not detected

coax has no content sync check (removed in 1.0.5-3, with `avcheck.py` and all `sync*`, `scan*`, `TRUST`,
`SCANBAD`, `SYNCBAD` engine code and `hub._desync`). The two-feed method compared a new stream with the stream
that was failing and blocked a good backup; its "second reading" used the same reference, so it was not
independent. A single-feed onset method gave +/-3 s of noise; ML was rejected. Feeds that are off are the
provider's (they play the same in VLC) and are disabled by hand on the Status card. Do not add a
detector back without the owner's go.

## Smart Standby (app/feedhealth.py)

Keeps each channel's saved stream order stable and good, acting only on real trouble.

- `problem(ch, key, kind)`: `placeholder` blocks at once (`INSTANT`; HLS emits it only after a 60 s ended
  playlist, TS on a `black.*` redirect); `desync` is also in `INSTANT`, `TEXT` and `RANK_W` but nothing
  produces it since 1.0.5-3; `frozen`/`novideo` (the hub's own restarts) are recorded
  for ranking only and never block on their own (since 1.0.4-1); `dead` (a stream failed and another took over,
  confirmed by `switched()`) blocks on the 3rd within 6 hours. Absorbed problems (stalls, keepalive) are never
  blocking. tsloop short connections hand off only when reserve plus lead is 15 s or less and never report
  `placeholder`. A channel with fewer than two enabled streams is never blocked (disabled streams do not
  count), and a disabled stream never becomes main or is promoted. When `_fix_order` rearranges a channel,
  disabled streams keep their places and "main" means the first enabled stream.
- Blocks: 12 h, 24 h, 3 days within a week. Never deleted. `is_blocked()` is used by the hub at tune time and
  in the failover list. If all streams are blocked, the least bad still plays. `block_info(ch, key)` returns
  `{until, kind, why}` (why = `TEXT`) for the card; `unblock(ch, key)` lifts it by hand.
- On a block, `_fix_order` moves the stream last; if it was main, the proven stream with the most clean playback
  becomes main. Proven = 10+ clean minutes (`good`, credited every 10 clean minutes); `verified` (a passed sync
  check) has had no producer since 1.0.5-3. An unproven stream never becomes main.
- After trouble and a switch, `_try_promote` moves the stream that proves stable ahead of the troubled one
  (main if needed). No other code moves the order back.
- `rank()` = (blocked, weighted fading problems minus clean minutes). Used by `backup_urls`.
- Every change goes to the bell (`notifications.json`, max 200) with Undo (restores the old order and lifts the
  block; refuses with 409 if the channel's streams changed), dismiss and clear. Messages are plain English with
  the provider's name.

## Channels DVR integration (app/channels_dvr.py)

`add_or_update` creates or updates the lineup's custom M3U source (`PUT /providers/m3u/sources/<key>`), refreshes
it and downloads the guide. `Syncer` listens to config changes and to provider refreshes (`App.on_refresh`
calls `on_change`), debounces 3 s, and refreshes every added lineup's source when its M3U text changed (and the
guide when enabled channels or guide ids changed). The M3U it compares is `App.lineup_m3u`, the same text the
DVR downloads, so a provider renumbering a lineup sorted by provider number is pushed too. Disabled channels
are not in the M3U and do not count as members. Sources must never need a manual Refresh in Channels DVR.

## Web UI (app/web)

No framework, no build. `app.js` routes `#/<page>` and lazy-imports `pages/*.js`; each page exports
`mount(el)` and `unmount()`. `api.js` holds every route. Patterns to keep:
- Channels page state `S`; Auto-match results are applied once per job (`sessionStorage coax.amSeen`).
  Suggestions are saved with `/api/channels/apply` using `add_streams` (append to the server's current list,
  never send a stale full list). Unsaved suggestions are mirrored in `localStorage coax.unsaved` until saved.
  Autosave (Settings, default 2 min) never runs while a dialog is open.
- Status cards are keyed per session (`uuid~instance`) and updated in place (rebuilding flashes). The tune
  card lists `feeds` (Playing now / Disabled badges, Switch button, Disable checkbox, hover popover for
  cut-off text; a blocked stream shows a purple dashed `Blocked · 23h 12m` pill with the reason on hover that
  unblocks on click) and a corrections list that keeps its scroll position. The Channels stream editor and
  backup chips show disabled streams too.
- Popovers via `data-pop` / `data-pop-sub`. Mono inputs disable ligatures (the `http://` slash bug).
- The bell polls `/api/notifications` every 15 s.

## Build, test, release

- Tests: on Gemini, `cd ~/src/coax && .venv/bin/python -m unittest discover tests` (309, about 40 s).
- A change to a Status or card function is run against the live config (or a test that exercises it) before a
  build; a syntax check is not enough.
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
- A/V sync work: first play the provider URL directly in a separate player (VLC) on an idle line. If it is
  out of sync there, the feed is bad and coax is not the cause; Disable it. Only then touch ffmpeg. The
  owner's eyes on the TV are the ground truth, not timestamps. Iterate with live candidates
  he watches, one change at a time, each run offline for a few seconds on real feed data first. After a live
  hot-swap of engine code, check for undefined names before he tunes.

## Invariants

- One DVR connection, one engine, one slot. Kill the engine before releasing the slot.
- Never two connections on one provider line. Probes use other lines or the DVR's shared tuner.
- coax does not judge sound against picture; an out-of-sync feed is disabled by hand.
- Never promote an unchecked stream; never move the order without real trouble; never rubber-band.
- Never delete a user's stream; block it.
- A disabled stream is never tuned, used as a backup or promoted; at least one stream of a channel stays
  enabled.
- Config changes only through `ConfigStore.update`.
- Every exception in background threads is caught; coax never crashes from feed health, history, cleanup or sync.
- One noisy reading never blocks.
- A broad `except` never returns empty data silently; it logs.
- Informational engine log lines never contain hub trigger phrases.
- Passthrough is called passthrough.
- A desync is checked against the provider directly (another player) before any ffmpeg change.
