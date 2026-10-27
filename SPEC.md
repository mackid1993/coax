# coax: specification

coax is a small, frozen IPTV appliance for a Channels DVR server. Playlists come in (Xtream Codes or M3U), and out go
a steady tuner stream per channel, an M3U with Gracenote station IDs, and an optional XMLTV guide. It replaces
Dispatcharr for this one job, reusing the stream engine already proven in production
(`app/engine/hlsloop.py`, `app/engine/tsloop.py`, copied from mackid1993/channels-dvr-iptv-tools).

## Non-negotiables
- **Appliance.** Fixed scope, built once, frozen. No auto-update, no code downloaded at runtime, no telemetry.
- **Pinned everything.** Base image by digest, Debian packages from a fixed snapshot.debian.org date, Python
  dependencies by exact version and hash (`--require-hashes`), GitHub Actions by commit SHA, Gluetun by digest.
  Published image tags are versions (`1.0.0`), never `latest` in docs or compose.
- **No database.** All state is JSON under `/data`. Config writes are atomic (write temp file, fsync, rename).
  Keep one rolling backup `config.json.bak`.
- **No environment variables for configuration.** Everything is set in the web UI and stored in `/data/config.json`.
- **One bind mount:** `./data:/data`.
- **LAN only, no authentication.** Document clearly that it must not be exposed to the internet.
- **Stdlib first.** Server is stdlib `http.server.ThreadingHTTPServer`. The only third-party Python dependency is
  `requests` (the engine already uses it) and its pinned dependencies. No server-side web framework and no CDN:
  every UI asset is committed to the repo and served from the image (works offline, never changes under you).
- **Python 3.13 in the image.** Code must also run on 3.11+ for local tests.
- **Reliability over features.** Every background job catches and logs its own errors and never takes the server
  down. A provider outage during refresh keeps serving the last good catalog. Streaming never waits on refresh.
- **Code style:** readable names, short comments where logic is not obvious, American English, no em dashes.
  No single-quote restrictions apply anymore (the engine is no longer pasted into a text box).

## Layout
```
app/
  main.py              entry point: load config, start server, start scheduler (owner: web)
  config.py            load/save/validate config.json (owner: web)
  server.py            HTTP routes (owner: web)
  web/                 index.html, app.css, app.js (owner: web)
  engine/              hlsloop.py, tsloop.py (adapted, owner: engine)
  hub.py               sessions, line slots, fan-out, engine processes (owner: hub)
  providers/           xtream.py, m3u.py, catalog.py, scheduler hook (owner: providers)
  guide/               gracenote.py, xmltv.py, output.py (owner: guide)
tests/                 unittest only (python -m unittest discover tests)
Dockerfile, docker-compose.yml, docker-compose.vpn.yml, requirements.txt (+hashes), .github/workflows/ (owner: packaging)
README.md (owner: packaging, with sections contributed by others)
```
Each owner writes only inside their paths plus `tests/test_<owner>_*.py`. Shared contracts below are fixed; if one
must change, say so in your final report instead of changing it silently.

## Config schema (`/data/config.json`)
```json
{
  "version": 1,
  "server": {"port": 7780, "base_url": "http://192.168.1.50:7780", "new_client_behind_seconds": 30,
             "channel_shutdown_delay": 10},
  "channels_dvr": {"url": "http://192.168.1.50:8089"},
  "transcode": "off",
  "providers": [{
    "id": "p1", "name": "My provider", "type": "xtream",
    "server": "http://host:80", "m3u_url": "", "xmltv_url": "",
    "user_agent": "", "format": "hls", "refresh_hours": 24,
    "lines": [{"id": "l1", "name": "Line 1", "username": "...", "password": "...", "max_streams": 1,
               "search": "", "replace": ""}],
    "auto_add": false,
    "groups": ["US| NEWS"]
  }],
  "channels": [{
    "id": 1, "uuid": "8c2b0d0e-...", "number": "101", "name": "Comedy Central", "group": "Entertainment",
    "logo": "", "enabled": true,
    "streams": [{"provider": "p1", "stream_id": "2151800"}, {"provider": "p1", "stream_id": "99"}],
    "guide": {"mode": "gracenote", "station_id": "10149", "xmltv_id": "", "xmltv_provider": "p1"}
  }],
  "lineups": [{"id": "main", "name": "Main", "channels": [1]}]
}
```
- `type`: `xtream` or `m3u`. For `m3u`, `lines` has one entry with empty credentials; `max_streams` 0 means unlimited.
- `format`: `hls` (default, engine `hlsloop.py`) or `ts` (engine `tsloop.py`).
- `transcode`: same values as the engine settings block (`off`, `1080p60`, `1080p30`, `720p60`, `720p30`, `1080p50`,
  `1080p25`, `720p50`, `720p25`).
- Channel `id`: positive integer, never reused. `uuid`: generated once at creation, never changes.
- `guide.mode`: `gracenote`, `xmltv`, `both` or `none`.
- Passwords are stored in config.json (file mode 0600) and never logged or returned by the API in full
  (return `"********"`; saving `"********"` keeps the old value).
- `config.py` fills defaults for missing keys and migrates by `version`.

## Lineups (profiles) and guide sources
- A **lineup** is a named profile, for example "Cable" and "NFL". Each lineup is added to the DVR as its own
  custom source, with its own M3U URL (`/output/m3u/<lineup>.m3u`) and XMLTV URL (`/output/xmltv/<lineup>.xml`).
- A channel can belong to any number of lineups. Lineups never share or reserve tuner slots; slots come only from
  provider lines.
- Each lineup has a **guide source**: `gracenote` (station IDs in the M3U, `tvc-guide-stationid`), `xmltv`
  (no station IDs; guide comes from the lineup's XMLTV file), `both` (station IDs where a channel has one, XMLTV
  for the rest), or `per-channel` (use each channel's own `guide.mode`). Example: "Cable" = gracenote,
  "NFL" = xmltv.
- **XMLTV is a first-class guide source**, not an extra. Per channel the user can pick the XMLTV channel id from
  any provider's XMLTV feed (searchable picker with display names), or from an extra standalone XMLTV URL added in
  Settings (`xmltv_sources: [{"id","name","url","refresh_hours"}]`). The XMLTV output for a lineup contains only
  that lineup's channels, ids rewritten to `coax-<channel id>`, and is regenerated after every guide refresh.
- The Lineups page shows, per lineup, its channel count, guide source, both URLs with copy buttons, and a
  one-line instruction for adding it as a custom source in the DVR (for an xmltv lineup: set the source's guide
  to the XMLTV URL).
- Config: `"lineups": [{"id": "cable", "name": "Cable", "guide": "gracenote", "channels": [1, 2]},
  {"id": "nfl", "name": "NFL", "guide": "xmltv", "channels": [40, 41]}]` and top-level `"xmltv_sources": []`.
  `output.m3u` and `xmltv.refresh` follow the lineup's guide setting.
- **Add to Channels DVR** (`app/channels_dvr.py`): one click per lineup creates or updates its custom M3U source
  in the DVR (guide: Gracenote only, XMLTV only, or Gracenote preferred with XMLTV fallback; stream limit; channel
  numbering; logos; name), reloads it and starts a guide download. Afterwards coax keeps the source in sync: a
  ConfigStore listener debounces changes for 3 s, reloads the DVR source when the lineup's playlist changes and
  also starts a guide download when channels are added or guide ids change. See API.md.

## Groups, refresh, and connection pools (must work like Dispatcharr)
- **Group selection.** After adding a provider, coax fetches its live categories. The user picks which groups to
  bring in (searchable checklist with stream counts, select all/none). Only streams in selected groups are
  offered in the Channels tab. Selection is stored in `provider.groups`.
- **Scheduled refresh.** Each provider refreshes on its `refresh_hours` schedule and on demand. A refresh updates
  stream names, logos and URLs, adds new streams in selected groups to the catalog, and reports streams that
  disappeared (channels pointing at a vanished `stream_id` are flagged in the UI, never deleted). Optional per
  provider: `auto_add` new streams in selected groups as disabled channels.
- **Connection pools.** A provider can have several lines (separate logins on the same service, like three Trex
  lines), each with its own `max_streams`. Several providers can be configured side by side, and a channel's
  streams can mix providers. When a client tunes a channel, the hub walks the channel's streams in order and
  takes the first stream whose provider has a line with a free slot; that line's credentials build the URL. A
  tune fails with 503 only when no stream of the channel has any free line. A second client on the same channel
  shares the running session and uses no extra slot.
- **Line URL building.** Xtream lines substitute their own username and password into the stream URL. M3U lines
  may set an optional regex `search`/`replace` pair applied to the stream URL (same idea as Dispatcharr's
  account profiles); empty means use the URL unchanged.
- **Backups across providers.** The backup list handed to the engine uses the session's line for streams on the
  same provider, and a currently free line for streams on other providers. The engine already treats busy
  responses (429/503/509) as retry-and-move-on, so a slot taken in the meantime is handled.
- The Status page shows every line with used/max slots and which channel holds each slot.

## Contracts between modules

### providers (`app/providers/`)
- `catalog.refresh(provider: dict) -> dict` downloads and caches to `/data/cache/catalog-<provider_id>.json`:
  `{"fetched_at": iso, "groups": ["..."], "streams": [{"stream_id": "...", "name": "...", "group": "...",
  "logo": "...", "tvg_id": "...", "url": "..."(m3u only)}]}`. On failure, raises, and the old cache stays.
- `catalog.load(provider_id) -> dict | None` reads the cache.
- `stream_url(provider: dict, line: dict, stream_id: str) -> str`.
  Xtream: `{server}/live/{username}/{password}/{stream_id}.ts` (the engine itself converts to `.m3u8` for HLS).
  M3U: the stored `url`.
- `xmltv_url(provider) -> str`: `xmltv_url` if set, else for Xtream `{server}/xmltv.php?username=..&password=..`
  using the first line.
- `scheduler.start(get_config)` runs a daemon thread that refreshes each provider every `refresh_hours`
  (and at startup if the cache is missing or stale), and the guide refresh (see guide). Never raises.
- Xtream: `player_api.php?username&password` (account info, used by "test connection"),
  `&action=get_live_categories`, `&action=get_live_streams`. Use the first line's credentials. Timeouts, retries
  with backoff, the provider `user_agent` when set. Only live streams; ignore VOD and series.
- M3U: parse `#EXTINF` attributes (`tvg-id`, `tvg-name`, `tvg-logo`, `group-title`), handle `#EXTM3U` header
  attributes, BOM, CRLF, missing attributes, commas in names. `stream_id` for M3U = stable hash of the URL.

### guide (`app/guide/`)
- `gracenote.search(cdvr_url, query) -> list` via Channels DVR `GET {cdvr}/tms/stations/{query}`; and
  `gracenote.suggest(channel_name, ...)` ranking candidates. Port the matching rules from
  `reference/m3u_editor_stationid_matcher.py` and `reference/dispatcharr_stationid_matcher.py` (call sign, name
  cleaning, lineup/country restriction, HD/feed suffixes). Keep their strictness.
- `xmltv.refresh(config)` downloads each needed XMLTV source (gzip or plain, streaming parse with
  `xml.etree.ElementTree.iterparse`, bounded memory), keeps only channels referenced by enabled channels with
  mode `xmltv` or `both`, rewrites `<channel id>` and `programme channel` to `coax-<channel id>`, writes
  `/data/cache/xmltv-<lineup_id>.xml` atomically. Keeps the old file on failure. Also caches the list of
  available XMLTV channel ids and display names per provider for the UI picker (`/data/cache/xmltv-ids-<pid>.json`).
- `output.m3u(config, lineup_id) -> str`. One entry per enabled channel in the lineup:
  `#EXTINF:-1 channel-id="coax-<id>" tvg-id="coax-<id>" tvg-name=".." tvg-logo=".." group-title=".."
  channel-number=".." tvc-guide-stationid="<station_id>",<name>` (station id only for `gracenote`/`both`),
  next line `{base_url}/stream/{uuid}.ts`.
- `output.xmltv_path(lineup_id)` for the server to stream the cached file.

### hub (`app/hub.py`)
- `Hub(get_config)`; `hub.open(uuid, client_ip, user_agent) -> Session | None`; `session.iter_bytes()` yields
  MPEG-TS chunks (188-byte aligned) to one client; `session.close()`. Returns None (server answers 503) when the
  channel is unknown/disabled or no line has a free slot.
- One engine process per active channel, shared by every client of that channel (tuner sharing). A new client
  starts `new_client_behind_seconds` back in a ring buffer (estimate seconds from bitrate; never mid-packet; start
  at a packet boundary). Slow clients are dropped rather than slowing others.
- Line slots: a channel session takes one slot on one line of the provider of its first stream, choosing the
  first line with a free slot (`max_streams`, 0 = unlimited). Slot released when the engine exits. Thread-safe.
- Engine command: `[sys.executable, "-u", "app/engine/hlsloop.py" | "tsloop.py", url, user_agent, str(channel_id)]`
  with `url` built by `providers.stream_url` for the allocated line. stderr lines go to the coax log with the
  channel name prefix.
- If the engine exits while clients are still attached, restart it (up to 5 times per 10 minutes) without
  dropping the clients. When the last client leaves, keep the engine for `channel_shutdown_delay` seconds.
- `hub.backup_urls(channel_id) -> [str]`: every stream of the channel, in order, built with the session's
  allocated line. `hub.status() -> list` for the UI (channel, clients with IPs, line, uptime, bytes).
- Study Dispatcharr's open-source proxy (github.com/Dispatcharr/Dispatcharr, `apps/proxy/live_proxy/`) for the
  fan-out and join-behind behavior and match it; that is what the engine was tuned against.

### engine (`app/engine/`)
Minimal adaptation of the two proven profiles. Change only what is required:
- Remove the settings block. Read `channels_dvr.url`, `transcode` and `server.port` from `/data/config.json`
  (path overridable by `COAX_CONFIG` for tests only).
- Replace `dapi()` with calls to the internal API: `GET http://127.0.0.1:{port}/internal/channels/{id}/streams`
  returns `[{"url": "..."}]` (same shape as Dispatcharr's), and `GET /internal/channels/{id}` returns
  `{"uuid": "..."}`. No API key.
- The viewer buffer finds the stream in Channels DVR sources whose URL contains `/output/m3u`; coax serves its
  M3U at `/output/m3u/<lineup>.m3u` so this keeps working. Verify the matching still holds against
  `output.m3u` (stream URLs contain the uuid).
- Update log wording that names Dispatcharr. Keep all stream logic byte-for-byte otherwise. Keep the readable
  structure; it no longer needs to be minifiable.

### server (`app/server.py`) routes
- `GET /` UI. `GET /healthz` -> `ok`.
- `GET /output/m3u/<lineup>.m3u`, `GET /output/xmltv/<lineup>.xml`.
- `GET /stream/<uuid>.ts` -> hub session, `Content-Type: video/mp2t`, chunked or close-delimited, no buffering.
- `GET /internal/channels/<id>/streams`, `GET /internal/channels/<id>` (only from 127.0.0.1).
- JSON API under `/api/`: config get/put (passwords masked), providers CRUD, test connection, refresh now,
  catalog groups and streams (paged/filterable), channels CRUD and bulk add from selected streams, auto-number,
  gracenote search/suggest/bulk-suggest, xmltv ids, lineups CRUD, status, log tail.

## UI
Single page, tabs: **Providers** (add Xtream or M3U, lines with max streams, test, refresh), **Channels**
(pick groups, browse/filter streams, add selected as channels, reorder, number, attach backup streams from any
provider, enable/disable), **Guide** (per channel mode, Gracenote search and one-click suggestions with
confidence, XMLTV id picker), **Lineups**, **Settings** (base URL, Channels DVR URL, transcode with the engine's
help text, join-behind and shutdown delay), **Status** (live sessions, line usage, last refresh results, log
tail).

**One-screen channel setup (the main screen).** Picking channels, Gracenote mapping and backup streams happen
together on one screen, in one pass, so setting up a lineup takes minutes:
- Left: the catalog for the selected groups, searchable, multi-select. "Add selected" creates channels.
- Main table, one row per channel: logo, number, name, enabled toggle, **Guide** cell (Gracenote match with
  confidence badge, or XMLTV id, or none), **Backups** cell (ordered chips of backup streams with provider name),
  drag handle for order.
- **One button, "Auto-match", runs both matchers for every selected row at once**: Gracenote suggestions
  (`app/guide/gracenote.bulk_suggest`, ported from the reference matchers) and backup-stream suggestions
  (`app/backups.py`: `suggest(channel, catalogs) -> [{"provider","stream_id","name","score","reason"}]`, ported
  from `reference/dispatcharr_backup_streams.py` `find_backups`: name cleaning, tvg-id match, similarity, across
  every provider and line, excluding the channel's own streams). Results fill the table as pending suggestions.
- High-confidence suggestions are pre-checked; the user reviews, then "Accept all checked" saves everything in one
  request. Each cell can be opened to pick a different candidate, search Gracenote by hand, or add/reorder/remove
  backups by drag and drop.
- Filters: unmatched guide, no backups, low confidence. Counters at the top ("212 channels, 198 guide matched,
  180 with backups").
- API: `POST /api/automatch` with channel ids -> `{channel_id: {"guide": [...], "backups": [...]}}`, and
  `POST /api/channels/apply` to save guide and backup choices for many channels at once.

**It must look great, not like a default admin page.** Spend real effort on design:
- A deliberate visual identity that alludes to coaxial cable and broadcast TV without using any trademarked
  names: a dark theme by default with a light theme toggle, one confident accent color, subtle depth (layered
  surfaces, soft shadows, gentle gradients), generous spacing, crisp typography.
- Self-hosted fonts committed under `app/web/vendor/` (OFL-licensed, e.g. Inter or Geist for UI and JetBrains Mono
  for numbers and logs), with their license files.
- Polished components: sidebar navigation with inline SVG icons, cards, tables with sticky headers and paging or
  virtual scrolling for 50,000+ streams, searchable multi-selects, toggle switches, toasts, modals, skeleton
  loaders, empty states with guidance, drag-to-reorder channels, channel logos with graceful fallbacks.
- Motion that feels smooth but restrained (150 to 250 ms transitions, respects `prefers-reduced-motion`).
- A Status page that feels alive: per-channel cards with viewer count, line usage meters, uptime, a throughput
  sparkline, and the log tail in a monospace panel, updated by polling every 2 to 5 seconds.
- Keyboard accessible, visible focus, WCAG AA contrast, responsive down to tablet width.
- A small vendored JS library is allowed if it clearly helps (for example Alpine.js or Preact with htm),
  committed under `app/web/vendor/` at a pinned version with its license and SHA-256 noted in the README;
  otherwise modern vanilla JS modules. No build step, and no npm at runtime or in the image.

## Packaging
- `Dockerfile`: `python:3.13-slim-trixie` pinned by digest; apt from `snapshot.debian.org` at a fixed date with
  `ffmpeg`, `intel-media-va-driver-non-free` and `mesa-va-drivers` (amd64), `vainfo`; non-root user with
  `video`/`render` group access documented; `HEALTHCHECK` on `/healthz`; `EXPOSE 7780`.
- **GPU support for Intel, AMD and NVIDIA.** The engine already picks NVENC, then VAAPI, then CPU (`hwpick`).
  The image must make all three work: `intel-media-va-driver-non-free` (iHD) plus `i965-va-driver` for older
  Intel (amd64 only), `mesa-va-drivers` for AMD, and an ffmpeg build whose `-encoders` list includes both
  `h264_vaapi` and `h264_nvenc` (NVENC loads at runtime from the libraries the NVIDIA Container Toolkit injects).
  The Dockerfile fails the build if either encoder is missing. Compose has commented blocks for `/dev/dri` with
  `group_add` (Intel/AMD) and for the NVIDIA runtime (`deploy.resources.reservations.devices`, driver nvidia,
  capabilities gpu/video/compute/utility, `NVIDIA_DRIVER_CAPABILITIES=all`; a container-runtime variable, not app
  configuration). README has one short section per vendor, including Unraid notes, and says how to see in the log
  which encoder was chosen.
- `requirements.txt` with exact versions and hashes.
- `docker-compose.yml`: coax only, `./data:/data`, optional commented `/dev/dri`, restart unless-stopped.
- `docker-compose.vpn.yml`: same plus Gluetun pinned by digest, `VPN_SERVICE_PROVIDER=custom`, WireGuard settings
  passed as environment variables from a `.env` file (Gluetun builds the tunnel itself; no config file), coax with `network_mode: service:gluetun`, port published on gluetun,
  `FIREWALL_OUTBOUND_SUBNETS` note for reaching the LAN. Gluetun is MIT-licensed; reference it, do not vendor it.
- GitHub Actions: tests on push, build amd64+arm64 image on push to main, publish to
  `ghcr.io/mackid1993/coax` with version tags from git tags (`v1.0.0` -> `1.0.0`) and `main` for testing.
  All actions pinned by SHA. Workflow `permissions:` minimal.
- README: what it is, nominative reference to Channels DVR plus a not-affiliated disclaimer, quick start for both
  compose files, GPU, settings walkthrough, guide modes, troubleshooting, LAN-only warning, MIT.
