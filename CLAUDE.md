# CLAUDE.md: how to work on coax, and what we learned building it

Read this before changing anything. It is the working summary of the project: the owner's rules, the settled
decisions, and the lessons that cost real time to learn. Most rules here exist because a mistake was made once.

- **How the code works today:** [AGENTS.md](AGENTS.md) (layout, request flow, hub, engines, Smart Standby, tests,
  release, invariants).
- **The full record:** [HISTORY.md](HISTORY.md), the original CLAUDE.md (about 390 KB, Oct 19 to Oct 28, 2026,
  through 1.0.5-4). "HISTORY 4.7" below means section 4.7 there. Read the relevant section before reopening any
  topic it covers.
- **The user guide:** [README.md](README.md).

The provider used for all testing is called "the provider": a typical Xtream Codes panel, three lines with one
connection each, HLS with 10 s segments and about a 60 s playlist window, served from Amsterdam. Its domain
names, the owner's IP addresses and all credentials are never written into this public repo.

Contents:
1. [The owner's rules](#1-the-owners-rules)
2. [Environment](#2-environment)
3. [How coax came to be](#3-how-coax-came-to-be)
4. [Settled decisions: do not reopen](#4-settled-decisions-do-not-reopen)
5. [Lessons, by topic](#5-lessons-by-topic)
6. [Facts and numbers](#6-facts-and-numbers)
7. [Release history](#7-release-history)
8. [Open items](#8-open-items)

---

## 1. The owner's rules

The owner is David (GitHub `mackid1993`): highly technical, dictates by voice (expect "dispatcher" for
Dispatcharr, "COAC" for coax; read for meaning), and has explicit preferences. Each rule below was stated,
usually after a mistake.

### Scope
- **Do only what was asked.** Propose anything else first and build it on an explicit go. An unrequested
  "Run Auto-match first" empty state had to be reverted.
- **One change at a time**, so results stay readable. Three simultaneous changes (sticky sessions, a new
  address, a VPN change) once made the results unreadable.
- **No spaghetti:** no sidecar tools, no extra layers. **No patching other people's apps.**
- **Fix, do not label.** "That's not a known limitation. That's something you have to fix."
- **Fix forward, do not roll back.** Never roll back or deploy without the owner's go-ahead. Do not revert
  releases the owner says are good.
- **Never give up on a stream.** coax retries forever while the DVR is connected. Proposals to give up after N
  seconds, turn off tuner sharing, turn off transcoding, or disable a core feature were all rejected.
- **No knobs.** coax is an appliance: hard-code sensible behavior (user agent, prefix cleanup, Smart Standby).
- **General, not personal.** It must work for any provider, HLS or MPEG-TS, and on low-power hosts. Build for
  the masses: most users have ONE line (one login, one connection) per provider. Nothing may depend on extra
  lines; an extra line can only be an optional bonus on top of something that already works with one.
- **HLS first.** MPEG-TS is used only when HLS is not possible. Nothing may force TS where HLS can play.
- **Gracenote station IDs in the M3U are a hard requirement. Channels DVR is the platform.** Do not suggest
  another player.
- **When porting, keep every feature and every piece of documented research**, but improve rather than copy
  blindly.

### Testing
- **Proportionate testing.** No million tests, no syntax check for a one-line text change, no overnight runs
  unless asked ("If you do it right, there should be no need for overnight runs"). Long soaks mostly measure
  the provider; each live test has one specific job.
- **Think first, then one test.** Derive the math, get a review, run one confirming test. Hours were lost to
  loops of 5-minute guess-and-test.
- **Simulate rather than wait.** Reproduce from logs and fixtures, offline, instead of waiting for a live repro.
  Offline end-to-end runs (real hub, real engine, local ffmpeg feeds) are the standard for engine changes.
- **Verify UI bugs in Node** (jsdom against a fake fetch) before and after the fix.
- **Read results before tearing anything down.** Runs were lost to containers that expired first.
- **Reviewers: fresh, read-only, time-capped.** Review rounds hit diminishing returns fast; act on observed
  evidence ("we are finding bugs that don't exist").
- **Render UI changes before shipping.** Real server with scratch data, Playwright's cached Chromium at phone
  and desktop size, screenshots read (AGENTS.md "Layout checks"). Do not download browsers for it: the owner
  asked for a WebKit download to be removed.
- **Never build a Status or card change after only a syntax check.** Run the changed function against the live
  config or a test.

### The live system
- **Never restart or deploy coax while the owner is watching or working, without asking.** A restart cuts the
  stream and wipes finished, unsaved Auto-match results (it happened twice).
- **Never block the stream the owner is watching for a test.** Force a switch with reorder plus retune, then
  restore the channel order and any blocks. Live candidates go to the owner one change at a time, each run
  offline for a few seconds on real data of that feed first.
- **Never open a second connection on a provider line in use.** One connection per line; a second returns 509
  and can kill the stream. Probes use an idle line, or none. Live tests: one test stream on the whole account,
  only when the owner is off it.
- **Never touch the owner's router, DVR or other services beyond reading** unless asked.
- **Back up `config.json` before any rebuild or config edit.**
- **Believe the symptom.** What the owner sees on screen is data. The Apple TV, the Mac app, Wi-Fi, the LAN and
  "the provider" were each blamed wrongly at some point. Measure; do not argue.
- **Credentials never go into chat.** Mask logs, URLs and rows before printing. Never paste secrets into
  configs through chat (the display masks them and breaks the file); write files directly.
- **Never kill by name or text pattern on a live host** (`pkill -f` matched the agent's own shell, and once the
  profile's log text, killing a live stream). Use PIDs.

### Communication
- Concise, technical, no hedging, no hype, no em dashes, American English everywhere.
- Give a status when work runs long, but do not stop to ask permission once the owner has said go.
- **No time estimates.**
- Say exactly what was tested and what was not. Never claim a test that was not run. Never call something a
  "known issue" that was only seen in a simulation. Separate realistic risk from theoretical.
- Deliverables go to Artemis `~/Desktop` as Markdown or files.
- Forum posts and changelogs: in the owner's voice, brief, no AI speak, no invented history, nested one level
  under a bold version-and-date bullet. Only when the owner asks for one: small follow-up builds (UI fixes and
  the like) get no changelog entry.

### Repo and releases
- **README:** plain English, no versioning language, no list of fixes. Explain features, settings and the
  research behind them (keep "Why 3%"). Describe the one-click Add to Channels DVR and the automatic sync.
  Transcoding is highly recommended (always on unless "running on a potato"). Do not imply coax depends on
  provider keyframes. HLS is strongly preferred, stated near the top. Verify a whole section against the code
  when the owner points at one row. Keep the Fancy Bits / Channels DVR non-affiliation notice at the top and
  the ah4c credit (README and file headers). The HLS box says "source", not "provider" (final, after going
  back and forth). Explain the daily cleanup transparently.
- **Install docs:** no `curl` downloads. Compose files and `.env` examples are embedded in collapsed
  `<details>` blocks and must match the repo files.
- **Compose files follow ah4c's style:** no editing needed; every setting is `${VAR:-default}` with a comment;
  runs with an empty `.env`; GPU by variables (`GPU_DEVICE`, `DOCKER_RUNTIME`), never commented blocks; required
  values use `${VAR:?message}` (`LAN_SUBNETS`, `VPN_SERVICE_PROVIDER`). `.env` examples: commented, one setting
  per block, values only for what must be there, never one platform's path, never the owner's own VPN setup.
- **VPN compose:** Gluetun unpinned, configured by environment variables only. It must offer VPN DNS
  (`DNS_UPSTREAM_RESOLVER_TYPE=plain` plus `DNS_UPSTREAM_PLAIN_ADDRESSES` with the port, `IP:53`). DNS filters
  (`BLOCK_*`) default off in the compose and are left out of the `.env` example. When a compose variable is
  renamed, tell the owner to update their own Apollo `.env`.
- Screenshots use made-up channels (Raccoon Network HD, Cat Courtroom...). No Unraid labels in repo compose files.
- Issues and pull requests are not accepted; provided as is; forks welcome (MIT). Rare updates are deliberate.
- **Images:** published only by manual dispatch (`gh workflow run image.yml -f version=X -f tag=latest`),
  amd64 only. The owner names versions: a significant change gets a new number, follow-ups are `-1`, `-2`.
  Every build is stamped: `scripts/stamp-version.sh <version>` writes `app/VERSION` (gitignored; CI runs it
  with the version input, else `<tag>-<commit>`), the UI shows it at the bottom of the sidebar, `/api/version`
  returns it and the startup log line includes it. Local builds: `scripts/build-image.sh <version>`.
- **Commits:** one fix per commit, so a change can be traced or reverted on its own. After pushing a split
  series, confirm the pushed tree is byte-identical to what was tested.

---

## 2. Environment

| Host | Role |
| --- | --- |
| Apollo (Unraid, i7-12700K with iGPU, RTX 4060) | Channels DVR (:8089), coax (:7780, Compose Manager project `coax`), `coax-gluetun` |
| Gemini (Mac mini) | git work in `~/src/coax`; scratch under `~/hermes-scratch/` |
| Artemis (MacBook Pro) | the owner's desktop; deliverables to `~/Desktop`; also used for builds and offline tests (has ffmpeg, Colima) |
| Living room Apple TV | main viewing client |

- The Channels DVR Mac app is the iPad app; the Mac shows as "iPad" in `/dvr/activity`. The owner has no iPad.
  Mac "output drops" come from 120 Hz ProMotion against 59.94 fps, not from the stream.
- coax data on Apollo: `/mnt/user/appdata/coax/data/`. Deploy: back up config, then in the Compose Manager
  project folder pull and `up -d --no-deps coax` with both `compose.yaml` and `compose.override.yaml`.
- Prefer a real build to hot-copying files into the container ("Don't swap out a file").
- Apollo's containers stop at 3:00 AM Sunday for backup; `/tmp` in a container does not survive it.
- Gluetun: static WireGuard exit, env vars only, not pinned. After Gluetun is recreated, recreate every
  container on its network. Inside its namespace use `127.0.0.1`, never `localhost`. Custom WireGuard needs an
  endpoint IP (not a hostname), an IPv4 address, the preshared key if used, and `FIREWALL_OUTBOUND_SUBNETS` set
  to the LAN, or nothing reaches Channels DVR.
- Overnight or long monitors run next to coax on Apollo (a monitor on Gemini got "No route to host"), read-only.

---

## 3. How coax came to be

Short version; HISTORY section 3 has the detail.

1. **Oct 19 to 20.** The provider's streams dropped every 30 to 50 minutes in Channels DVR. Causes found: second
   connections the owner's own tools opened (a Dispatcharr web player, account saves), then real provider
   closes that exhausted Dispatcharr's 3 hard-coded retries. HLS input beat MPEG-TS because segments can be
   retried. A Gracenote station ID matcher was built (its rules live on in `app/guide/gracenote.py`).
2. **Oct 20 to 22.** m3u-editor tried and dropped (no pre-buffer). The provider delivers at 1.00x real time on
   every route: stalls are the provider not publishing on time, not bandwidth. A Streamlink loop plus a
   persistent ffmpeg remuxer gave the first real cushion.
3. **Oct 23 to 27.** An inline Python Dispatcharr stream profile ("hlsloop", later "tsloop" for MPEG-TS) grew
   into the engine coax uses today: resume at the next unsent segment, catch up at full speed, in-process
   failover to backup streams with continuous timestamps, a hidden reserve paced by video timestamps, a head
   start for each joining Channels device, null-packet keepalive, recording extension, transcoding with the
   whole chain on the GPU, and the 3% refill. A separate backup streams app added failover feeds in bulk.
4. **Why coax.** As a Dispatcharr subprocess the profile could only see a URL and stdout: no clients, no lines,
   no other channels, no state, and Dispatcharr's chunking, log filtering and launch behavior in the way. The
   owner decided to own the whole stack.
5. **Oct 27 to 28: coax.** Built by eight parallel agents, then hardened release by release (section 7).
   The engines were ported nearly verbatim so the proven track record carried over; the hub follows ah4c.

---

## 4. Settled decisions: do not reopen

Each was decided after real evidence. Reopen only with new evidence and the owner's go.

- **No sound-against-picture detection.** coax does not detect or block for sound off its picture. Every
  method failed: a single-feed onset method read +/-3 s of noise; a two-feed comparison judged against the
  failing stream and blocked a good backup (its "second reading" used the same bad reference). ML (SyncNet,
  Synchformer) was rejected. Out-of-sync feeds are the provider's (they play the same in VLC) and are disabled
  by hand with Switch and Disable on the Status card. (HISTORY 4.7, 4.8)
- **ffmpeg probe stays at 1 MB / 1 s.** The 5 s probe and `-muxdelay 0 -muxpreload 0` of 1.0.4 were believed to
  fix an HE-AAC lip-sync bug; the feed was out of sync at the provider and both were removed. (HISTORY 4.6, 4.7)
- **Transcode audio:** `aresample=async=1:min_hard_comp=0.1` per input (plus `asetpts=PTS*x,atempo` in a refill,
  with video `setpts=PTS*x`, both from 0), no `first_pts`, never `async=1000` (it stretched audio 5 s off on a
  healthy feed). Output fixed per DVR connection by the owner's setting (Dolby Digital 5.1 640k, or AAC stereo
  192k). Never an encoder restart or filter rebuild for an audio change (owner: restarts show on screen; "I
  can't accept any gaps"): the audio feeder (aslots) gives ffmpeg one input per exact format, so 5.1/stereo ads,
  codec switches, track moves and dropouts only move the audio between inputs. A rebuild in a multi-input graph
  dropped 0.13 s at random (7 of 15 runs); with fixed-format inputs, 0 of 30. `-drop_changed:a` is gone.
  `-dts_delta_threshold 86400`: ffmpeg never rewrites timestamps after a jump; the engine continues the
  timeline itself. `FfmpegArgsTest`, `TranscodeAudioTest` and `tests/test_audio_slots.py` pin these.
- **Passthrough is called passthrough** (`-c copy -copyts`). Do not rename it to "remux".
- **Transcoding is the right tool** for a fixed output format across feeds and for the 3% refill. Every
  passthrough alternative failed: a new MPEG-TS program at a switch froze the Apple TV permanently; dropping
  frames stuttered; declaring 59.94 fixed only the label; forcing a reconnect left audio off; ranking feeds
  at tune time caused 509s; stretching timestamps in passthrough dropped frames. A hybrid (passthrough until a
  switch) would bring back the mid-stream format change. The chain is GPU end to end (about 10% of one core
  for 1080p60 on VAAPI).
- **The 3% refill:** restart the encoder at the two edges with the speed fixed from the first frame, wait for a
  keyframe, correct the audio lead. Never live speed commands (`atempo` loses about 20 ms per command; a
  motion-following rate put audio 10 s off). Never change pitch, never let audio lead early. "Why 3%" stays in
  the README. (HISTORY 3.7, A.6, A.7)
- **Hub = ah4c model:** one DVR connection, one engine, one line slot, kill then release. No fan-out (Channels
  shares its own tuner). Two DVR clients on one channel use two slots (accepted).
- **Auto-match suggestions are a holding pattern, never approval.** Checked suggestions are kept (on the server,
  across reloads, devices and restarts) until Save or Discard; nothing applies them for you. The old autosave
  that applied checked items was removed (owner: "it takes stuff that you've checked off and approves them for
  you instead of just saving them"). Everything reviewable shows under **To review**.
- **A channel's guide is one source: a Gracenote station or an XMLTV id, never both.** Picking one clears the
  other, Auto-match's station replaces the XMLTV id, and `config.one_guide` drops the XMLTV id beside a station
  on every load and save (1.0.6-6; 143 of the owner's channels had both). Lineup-level "Both" (station, XMLTV
  for the channels without one) is unaffected.
- **Reordering streams never switches.** Dragging a stream on the Status card (or reordering in the stream
  editor) only saves the order (main stream at the next tune, tiebreak in the score-ranked failover list); switching is the deliberate **Switch** button.
- **The UI works on a phone** (owner: "something you might need to fix really quick with on your phone").
  Check every page and dialog at phone width before shipping a UI change.
- **Tuner sharing stays on.** A joining Channels device starts at the newest byte; coax gives each join a 5 s
  head start from the reserve.
- **Recordings:** never alter the stream for a recording (a flush made live viewers jump 60 s). Extend the
  Channels job instead: measured delay plus 45 s, at most 3 minutes, on top of the user's padding, once,
  stored on disk.
- **Smart Standby rules:** the owner picks the streams, coax picks the order. Act only on real trouble; what
  the reserve absorbs is ignored. Block for long periods (12 h, 24 h, 3 days), never delete. Only a proven
  stream (10 clean minutes) becomes main. Move once, never rubber-band. Notifications go to the bell with Undo.
- **Switching is a last resort:** stay on a slow or briefly stalled feed to keep the viewer's place; switch on a
  placeholder, a dead feed (before the reserve runs out), a stuck download, audio and video timestamps more
  than 3 s apart, or a feed that stays far below real time for minutes (HLS below 0.75x while the reserve
  drains, TS below 0.85x with the reserve nearly empty, 5 minutes).
- **Auto-match:** backups come only from the provider's selected groups (searching the whole catalog once
  matched a Polish Comedy Central), and the Gracenote region (Settings > Auto-match region) decides which
  stations are trusted. Unmatched channels stay listed ("Everything has to be matched").
- **Settings live in coax's config, typed by the user.** No environment variables for app settings (Dispatcharr
  once wiped them with `su -`), no auto-discovery of Channels DVR, and no guessing of coax's own Base URL
  (required, verified via `/healthz`; the request-Host fallback was removed).
- **User agent hard-coded** to `TiviMate/5.1.6 (Android 12)`. Prefix cleanup always on.
- **Version pinning:** coax pins everything (base image digest, Debian snapshot, pip hashes, Jellyfin ffmpeg by
  checksum, action SHAs); Gluetun is not pinned (security fixes).

---

## 5. Lessons, by topic

### Process and judgment
1. Believe the symptom; measure before explaining. The screen is data.
2. Verify a claim against code, docs or a test before stating it. Mark inferences as inferences.
3. Trace every setting to the code that reads it before recommending it (six m3u-editor retry variables only
   applied to its TS path).
4. Test the provider directly before claiming its behavior; never generalize from another provider.
5. **Before changing ffmpeg for a desync, play the provider URL in VLC on an idle line.** It settles "feed or
   coax" in minutes; skipping it cost six releases (1.0.4-2 to 1.0.4-7).
6. One good run does not prove a fix for an intermittent problem (a 40-minute in-sync run was coincidence).
7. When different fixes give the identical number, suspect the measurement (a 3.36x "burst" was a sim artifact).
8. A stream keeps the code it started with; check which build a running stream is on before diagnosing.
9. Check concurrent test traffic before saying "not us" (a test agent's streams caused the owner's 509s).
10. Read crash reports and logs before blaming code, hardware or the network.
11. Agents drop requirements; verify their output against the owner's words. Steering messages to agents that
    already finished are lost. Long-writing agents must write incrementally to files.
12. A fresh, adversarial, read-only review with the history's settled decisions in its brief finds real bugs
    (the 1.0.6 review found a 509 window, a dead handoff rule and a timestamp jump on every hand switch).

### Live safety
13. Never two connections on one line. A read must never reserve a resource. Release a slot only after the
    engine on it is dead.
14. Never restart, deploy or roll back while the owner watches, without asking.
15. Never kill by name or text pattern on a live host; use PIDs.
16. Tests run in throwaway containers or offline, limited in CPU and count; no frequent `docker top`/`exec`, no
    large captures on Apollo; read and copy results before removing anything.
17. Never print logs, URLs or rows that can contain credentials.

### Streaming engine
18. Many providers do not mark keyframes or start segments on them; never depend on it. When no keyframe is
    found, start at a packet that has timestamps.
19. Providers resend overlap after a TS reconnect; dedupe whole PES units, not packets.
20. A failover clock measures delivered video, not playlist movement. HTTP 200 is not proof of video.
21. Rejoin a backup only as deep as the outage, accumulated across hops.
22. Pace output by video timestamps, not bytes. With a GPU encoder, pace on its output, and keep feeding it
    while its output is silent (it holds seconds).
23. A joining Channels device starts at the newest byte: give each join a head start; never lower the lead by
    pausing output.
24. Keepalive must be gated on real liveness (writer and fetch loop), or it hides a wedged process.
25. Restarting an encoder at a switch or refill waits for a keyframe and corrects the audio lead.
26. Every switch, restart or hand switch must continue the output timeline (`-output_ts_offset`) from where the
    output actually ended, a partly sent segment included. A new remuxer without it jumps the clock (1.0.5-4
    jumped about 5,000 s on a hand switch in passthrough).
27. A process restarted on a new stream must not inherit the old stream's failure clock, stuck flag or player
    lead. A new DVR connection has no player lead.
28. Every download needs an overall deadline, not just a read timeout; a trickling provider otherwise holds the
    fetch loop while the reserve drains. Always close responses so no connection lingers.
29. Detection windows delay recovery detection; count them in timing margins (HLS refill waits 75 s, TS 60 s).
30. Use hysteresis or a minimum dwell on threshold logic; the HLS reserve is a sawtooth by segment size.
31. A guard that restarts something must not restart it forever, and its own kills must not be read as a
    different failure (1.0.5-4's silent-audio guard restarted every 15 s on a feed without audio and switched
    decode to the CPU).
32. A counter that an exception path resets must not be reset by the exception it counts (the TS
    short-connection handoff never fired).
33. An ended playlist after proxy errors is usually a hiccup; wait on the reserve. Only a minute of it, outside
    proxy errors, counts as an offline placeholder.
34. A line-wide error burst (407 proxy errors) is the line's fault, not the stream's; do not charge the stream.
35. Never alter the stream for a recording; extend the Channels job (idempotent, capped, keeps the user's
    padding, stored on disk, written before the PUT).
36. Keep the provider's timestamps through jumps and give audio and video one clock, or a dropout becomes a
    permanent offset.
37. Run every candidate ffmpeg command offline for a few seconds on real data of that feed before it reaches a
    live server (`-map 0:a:0` with a short probe crash-looped a live stream).
38. After any live code patch, check for undefined names (`pyflakes`); a removed helper crashed a live stream.

### Hub, Smart Standby and logs
39. Engine log wording is an API to the hub: `PROBLEMS` and the timeline patterns match any line containing the
    phrase. Grep them before adding or rewording a log line (an informational line once blocked a good stream
    for 24 h).
40. A comparison is only as good as its reference; a second reading against the same reference is not
    independent. A check that cannot tell good from bad is removed, not tuned. One noisy reading never blocks.
41. A test that blocks a stream looks exactly like a real fault; one failure must not block.
42. A disabled stream never plays, backs up or becomes main; a channel with one enabled stream is never blocked.
43. Check that the object you call a method on has it (`Hub._kill` did not exist; 1.0.5-2).
44. Never let a broad `except` return empty data or swallow an error silently; log it. Never reuse a variable
    for a different kind of value (1.0.5-4's empty stream list).

### Config, providers and UI
45. Server validation ignores unknown fields rather than rejecting a whole save.
46. Saves add to the server's current state; never send a stale full list.
47. Keep unsaved user work somewhere that survives a reload or restart; never tell the owner to relaunch
    something that holds unsaved work.
48. Update UI cards in place; rebuilding on each poll flashes.
49. Check how a host launches a subprocess before relying on its environment.
50. Provider API calls (catalog, guide, Test) use an idle line; an empty provider answer never replaces a good
    catalog; stream ids must survive reorders and renames, and channels follow renamed ids.
51. The DVR sync must compare exactly what the DVR downloads, or changes slip through (provider renumbering).
52. Keep the README accurate and complete; review it against the code after changes.

---

## 6. Facts and numbers

- **The provider:** 10 s HLS segments (EXTINF matches real duration), about a 60 s playlist, no
  `EXT-X-PROGRAM-DATE-TIME`, no broadcast clock in the stream. Delivery 1.00x with dips; segment downloads
  about 10x real time; publish gaps 6 to 15 s. TS connect burst about 20 s then real time; TS reconnect overlap
  5 to 21 s. Errors seen: 403, 406, 407, 500, 509, 522. Offline placeholder: an `#EXT-X-ENDLIST` playlist (HLS),
  a redirect to `black.*` (TS). Frequent channel restarts with timeline jumps. "4K" streams are 1080p. Feeds of
  one channel differ in frame rate and resolution. About 55,900 streams, 10,246 with a tvg-id.
- **Channels DVR:** tuner sharing `dvr.tuner-sharing` (stays on); joiners attach at the write head; about a 6 s
  watchdog; "901 Tuner Unreachable" after 20 s; `/dvr/activity` lists live streams and recordings separately;
  `PUT /dvr/jobs/{id}` with a new end time works on a running job; custom M3U sources at
  `/providers/m3u/sources/<key>`; loads the first 750 channels of a custom source; uses mpv.
- **coax engine constants:** HLS reserve about 56 s plus a 4 s player lead; TS reserve about 20 s, lead 3 s;
  5 s head start per joining device; keepalive after 2 s (HLS) / 3 s (TS); no new video 60 s hands off (sooner
  near an empty reserve); A/V timestamp hold 3 s; refill 3% below half reserve (after 75 s HLS / 60 s TS, not
  within 90 s of a tune or switch, edges 60 s apart) until three quarters; recording extension delay + 45 s,
  max 3 min.
- **coax hub constants:** frozen engine 25 s; no video 90 s retunes (every second retune moves line); backoff
  doubling to 15 s, none for a hand switch; line cooldown 5 s; null packets only on 188-byte boundaries after
  video.
- **Smart Standby:** placeholder blocks at once; `dead` blocks on the 3rd confirmed takeover within 6 h;
  `frozen`/`novideo` rank only; blocks 12 h / 24 h / 3 days within a week; proven = 10 clean minutes.
- **ffmpeg (1.0.6):** input `-dts_delta_threshold 86400 -fflags +discardcorrupt -probesize 1000000
  -analyzeduration 1000000` (tsloop transcode adds 5 MB / 3 s); passthrough `-map 0:v? -map 0:a? -c copy
  -copyts`; transcode `-i pipe:0 -map 0:v:0? -map 0:i:<best audio PID>?`, audio
  `aresample=async=1:min_hard_comp=0.1`, 48 kHz, AC-3 5.1 640k with the Dolby setting, else AAC stereo 192k, fixed per DVR connection (1.0.7-4); output `-output_ts_offset ... -f mpegts`.
  Presets `1080p60` (6M), `1080p30`/`720p60` (4.5M), `720p30` (3M), plus 50 Hz variants. Jellyfin ffmpeg 8.1.3.
- **Performance:** one 1080p60 VAAPI transcode about 10% of one core for the container; engine memory about
  78 MB. Overnight runs at 0% buffering on both the old profile and coax.
- **Dispatcharr baseline (history):** 25.5 h with 388 catch-ups, 57 playlist retries, 11 switches.
- **The owner's lineup:** 247 channels, 207 Gracenote stations; Gracenote region US 10954. Bravo: main `45446`,
  backups `174571`, `2151823` (11 to 12 s sound ahead). Comedy Central HE-AAC backup `2151800` is out of sync at
  the provider and disabled. Real stream ids never go in the README.
- **Tests:** 353 after the audio inputs (1.0.7-4), about 50 s.

---

## 7. Release history

- **1.0.0** (Oct 27): first public release.
- **1.0.1** never give up: frozen and no-video recovery, null packets between engines, backoff without limit,
  resume across restarts, history and the Unsteady badge. **-1** HLS hint. **-2** daily cleanup. **-3** encoder
  restart at a switch (did not fix Bravo; the cause was the feed).
- **1.0.2 to 1.0.2-9** Smart Standby (scores, long blocks, promotion of proven streams, the bell).
- **1.0.3** backend and UI bug round. **-1** full `.tar.gz` backups. **-3** ffmpeg keeps provider timestamps
  through jumps (`-dts_delta_threshold 86400`).
- **1.0.4** HE-AAC "fix" (5 s probe, zero mux delay) that fixed nothing, transcode audio filter, ended-playlist
  wait, chromaprint sync check. **-1 to -7** sync check fixes, then six engine guesses at a desync that was in
  the feed.
- **1.0.5** Switch and Disable on the Status card; 1.0.4 probe and mux changes removed. **-2** Switch and Disable
  stop the playing engine. **-3** sync check removed, Blocked pill, stream ids in logs. **-4** stream list back.
- **1.0.6** (Oct 28) from a fresh adversarial review, one commit per fix:
  - silent-audio guard: once, then backoff; never for a feed without sound; its kills never disable GPU decode;
  - Switch and Disable: old line held until the old engine is dead; output timeline continues in passthrough on
    both engines; no crash backoff; backup line reused;
  - MPEG-TS: short-connection handoff works; keep-feeding for GPU encoders; no crash on a splice without a
    detectable keyframe; correct reserve figure; redirect ticket only right away;
  - HLS: overall download deadlines, master playlist hop limit, AES key cache bound, state file at most 5
    times a second; ended-playlist minute keeps counting while no backup answers;
  - 407 proxy errors are not charged to a stream;
  - providers: idle line for API calls, empty answers ignored, stable M3U ids, renamed ids followed, per-stream
    format; logs mask the short Xtream form and every configured login;
  - Smart Standby ignores disabled streams; DVR sync pushes provider renumbering and ignores disabled
    channels; the backup check no longer opens every archive every 30 s.
  Tested offline on Artemis (real hub and engines, local HLS and TS feeds). Checked live on Apollo afterwards:
  full VAAPI chain, ~10% of one core, no false guard restarts.
- **1.0.6-1** (Oct 28) from a second adversarial audit of 1.0.6:
  - HLS first: 1.0.6 had started playing M3U `/live/...ts` streams as MPEG-TS even with the provider on HLS;
    now only a stream with no HLS form (or a provider set to MPEG-TS) uses TS;
  - TS short-connection handoff no longer waits 5 s on a channel without a working backup and no longer
    counts against the stream;
  - a hand Switch back to the tuned stream plays it (the engine had resumed its last failover stream);
  - M3U ids that change with a rotating URL token are still followed; disabled streams keep their places in
    Smart Standby reorders; the silent-audio guard judges each stream on its own; logins are masked from
    startup; Switch refuses cleanly if a retune just moved the channel's line;
  - HLS continues the output timeline over lost video (a skipped segment, or an outage longer than the
    playlist) instead of a freeze with silence; a busy answer to a backup probe no longer marks the backup;
  - build version stamped into the image, shown in the sidebar and the startup log.
  Forum changelog for 1.0.6 and 1.0.6-1: `~/Desktop/coax-1.0.6-changelog.md` on Artemis. Short-form Xtream
  addresses (`/<user>/<pass>/<id>` without `/live/`) still play as MPEG-TS: they have no known HLS form, and
  guessing one could break streams that only look like that pattern.
- **1.0.6-2** (Oct 28) phones: the sidebar fits the visible screen; the public IP (flag and country name) and
  the version move to a bar above the page at 900 px or less.
- **1.0.6-3** (Oct 29) phones: the sidebar is fixed (sticky moved at the end of a scroll); Providers buttons,
  Settings (transcode help, region, backups, XMLTV sources), filters, stream rows and every dialog fit the
  screen. Status card: drag streams into a new order (saved with the streams endpoint's `order` mode, never
  switches). Checked by rendering every page and dialog at iPhone size against the real server.
- **1.0.6-4** (Oct 29) Status page no longer jumps up when a card's corrections list was scrolled and the page
  sits at the bottom (the list's scroll was restored before it was reopened, forcing a layout of a shorter page).
- **1.0.6-5** (Oct 29) Channels page: Auto-match suggestions are held on the server until Save or Discard (the
  autosave that applied checked items is gone); To review shows every channel with suggestions; filters and
  counters agree (No backups counts playable backups only, Unmatched guide excludes left as is); selection is
  pruned to visible rows; Save clears only what it applied; auto-number follows the order shown. From an audit
  plus three review rounds; tested against the real server with a fake Xtream panel in Chromium.
- **1.0.6-6** (Oct 29) a channel's guide is one source: the per-channel "both" mode is gone, the Guide dialog
  clears the XMLTV id when a station is picked (and the other way around), Auto-match no longer turns an XMLTV
  channel into "both", and stored "both" channels become Gracenote at load with the XMLTV id dropped.
- **1.0.6-7** (Oct 29) from a targeted audit (six read-only agents, every finding reproduced), one commit per
  fix: Gracenote is the default (new channels start as Gracenote with the provider tvg-id as an Auto-match hint;
  a channel set to XMLTV stays XMLTV); provider Total stream limit saved; Guide and Streams dialogs keep held
  suggestions and fetch fresh streams; lineup M3U follows coax order and the page's A to Z; provider-sorted
  lineups keep their numbers through promotions (`number_stream`); a provider without an XMLTV URL no longer
  freezes a lineup's guide; Settings XMLTV sources get fresh ids and refresh the guide at once; provider ids are
  never reused (`next_provider_id`) and a delete drops its held suggestions; renamed M3U ids carry Smart
  Standby state and held suggestions; empty Xtream categories are a failed refresh; Status card Disable avoids
  blocked streams, badge and focus fixed, Undo refuses over a newer order, Unblock clears confirmed deaths;
  Auto-match backups only from picked groups. Then two fresh reviews (Python, UI) and a confirmation pass; their
  findings fixed (Status page scroll jump from refocus, legacy "both" without a station keeps XMLTV, M3U query ids
  from identity keys only, Xtream empty categories refused only over a catalog with named groups, A to Z key
  matching the browser's ICU order, verified against Node on the owner's 208 names). Deploying it renumbers 15
  channels of the owner's A to Z Trex lineup in Channels DVR, to match the Channels page.
- **1.0.6-8** (Oct 29) on/off switches on provider and lineup cards. A provider switched off is not refreshed,
  tuned, guide-downloaded or offered as a backup; channels whose every stream is on it leave the playlists (a
  playing stream keeps going until the next tune). A lineup switched off serves an empty playlist, so Channels
  DVR drops its channels; they come back with the same numbers.
- **1.0.6-9** (Oct 29) Channels page hides a provider switched off: its catalog streams, its pickers and every
  channel whose streams are all on it.
- **1.0.6-10** (Oct 29) lineups switched off are hidden on the Channels page and never get new channels (their
  members are kept); with every lineup off, new channels go into a new lineup.
- **1.0.7** (Oct 30) transcode keeps surround audio (the track with the most channels, 5.1 stays 5.1, AAC 384k).
  Switched off means hidden: the Channels page and Status counts leave out channels that are only on providers or
  only in lineups switched off; Status lists no lines (unless still holding a stream) or refreshes of a provider
  switched off.
- **1.0.7-1** (Oct 30) transcode sends Dolby Digital (AC-3 640k, re-encoded so the refill and gap correction still
  work) when the provider sends Dolby, else AAC 384k. Quieter logs: decoder noise and keepalive at tune start,
  routine playlist gaps and normal catch-ups are no longer logged as problems. Checked offline on both engines
  (Dolby from the first byte; AAC to Dolby mid-stream with a continuous timeline).
- **1.0.7-2, -3** (Oct 30, the other agent) audio layout changes no longer freeze; Dolby channels always send 5.1,
  stereo ads in the front left and right; quieter logs; CDN session tokens masked.
- **1.0.7-4** (Oct 30) audio rebuilt so a transcode never restarts or drops sound for an audio change: one
  ffmpeg input per exact audio format (aslots), the provider's track cut into frames and routed to its input,
  silence elsewhere. Settings > Transcode: "Always send Dolby Digital 5.1 (640 kbps)" (else AAC stereo 192k) and
  "Audio language" (all 482 ISO 639-2 languages, most spoken first). Track choice: preferred language, else the
  main track's, never audio description when there is another, sticky through stereo ads. Refill timestamps
  scaled from 0, so a 5.1/stereo switch during a refill no longer puts the sound ahead (it did, 1.2 s at 40 s in).
  MPEG-TS engine no longer crashes on every reconnect (`QUIET` clash from 1.0.7-2). Tested offline, each case
  repeated, gaps counted: 5.1/stereo flips (Dolby, AAC), Dolby to AAC on one track, a six-codec chain (E-AC-3,
  MP2, HE-AAC, AC-3, AAC, DTS), no audio, late audio, a 30 s dropout, two languages, audio description, an empty
  second track, an encoder starting mid-stream, a PTS wrap, a two-packet PMT, the refill, stereo with the setting
  off; end to end with the real hub on HLS and MPEG-TS (with a reconnect). GPU paths not tested (no GPU on
  Artemis); live test on Apollo pending. After the review: 7 always-open inputs plus formats the stream shows (CPU
  about a third of 17), AAC stereo back to 192k, slot inputs at `min_hard_comp=0.01` (sync within about 10 ms of
  a direct transcode, codec switches included; was up to a frame off), fMP4 HLS passes through, ID3 and split
  packets resynced, no backward timestamps from the video-clock fill, E-AC-3 dependents kept, exact 44.1 kHz
  silence durations. PCM fallback for rare formats written but off (its PCM reached the mix late offline).
- **1.0.7-6** (Oct 30) stereo upmixed to 5.1 when the output is Dolby 5.1 (stereo and mono inputs mixed apart,
  then `surround=chl_out=5.1`): the owner's Neural:X never engaged on ads, since coax always sends 5.1, so ads
  played quietly from the front pair only. From three parallel audits: a failover to a backup with another codec
  family starved the encoder (the feeder kept the old stream's tracks), an encoder rebuilt before its first byte
  lost the restart's timeline offset, audio 1.2 s or more off its video stayed silent, audio description could
  win over the main track, the video-clock silence overwrote unsent audio, long ID3 tags lost a chunk; all fixed.
  A codec family with no open input now asks for a keyframe restart instead of feeding another decoder.
- **1.0.7-7** (Oct 30) fix of a 1.0.7-6 regression: the feeder's audio clock counted every forward step between
  video timestamps, and B-frames (nearly all broadcast H.264) made it run about 1.65x fast: after a 5 s audio
  dropout the sound came back 3.5 s late, after a 20 s one never. Now it advances only past the newest video
  timestamp, and a provider clock jump shared by the pictures is followed.

---

## 8. Open items

- Two Channels DVR clients on the same coax channel use two provider slots (ah4c model, accepted).
- Auto-match does not search by a call sign that is only in parentheses in the name (out-of-market locals such
  as KSHB): name cleanup strips it.
- The transcode audio filter has not had a 10+ minute live watch on a healthy HE-AAC feed.
- Audit of every engine switch trigger against "no switch while the reserve is healthy and the feed still
  delivers" (playlist errors after 5 retries, about 18 s, mark a stream failed).
- A health check at tune time, every ~30 minutes and on failover: discussed, deferred. If built, it must decide
  in seconds at tune time; multi-minute measurement runs were rejected.
- `desync` entries in `feedhealth` `TEXT`/`RANK_W` and `verified()` have no producer; harmless.
- A long, untouched coax run to compare with the Dispatcharr baseline.
- 1.0.6 GPU paths (VAAPI/NVENC keep-feeding in tsloop, the GPU-decode fallback) to be confirmed on Apollo.
