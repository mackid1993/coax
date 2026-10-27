# coax: gaps against Dispatcharr (live TV for Channels DVR)

Scope of this review: what Dispatcharr (github.com/Dispatcharr/Dispatcharr, main branch, shallow clone) does for
live IPTV feeding a Channels DVR server, compared with SPEC.md plus the code already in this repo
(`app/engine/hlsloop.py`, `app/engine/tsloop.py`, `reference/*.py`). VOD, series, catch-up/timeshift, users/auth,
plugins, backups, Xtream output API and DVR recording inside Dispatcharr are out of scope by design and are listed
only once at the end of the table.

File references are paths in the Dispatcharr repo. Recommendation key: **must** = needed to replace Dispatcharr for
this use case without regressions; **nice** = worth doing if cheap; **out** = out of scope for the appliance.

## Gap table

| # | Feature | What Dispatcharr does | SPEC covers? | Rec. |
|---|---------|-----------------------|--------------|------|
| 1 | Slot accounting when the engine fails over | The proxy moves the connection slot to the new profile on every stream switch (`apps/m3u/connection_pool.py` `move_credential_slot_on_profile_switch`, `apps/channels/models.py` `Channel.update_stream_profile`, `apps/proxy/live_proxy/input/manager.py` `_try_next_stream`). Alternates are only offered when their profile has capacity (`apps/proxy/live_proxy/url_utils.py` `get_alternate_streams`). | Partly. The engine does its own failover (`feeds()`/`handoff()`), so the hub never learns that the session moved to a stream on another provider. SPEC holds a slot only on the first line. A failover to provider B uses a B connection that B's pool does not count, so a later tune can exceed B's `max_streams` and trip provider bans. | must |
| 2 | Which streams the engine may fail over to | `get_alternate_streams` filters out streams whose account/profile is inactive or full at switch time, in channel order, rotated to start after the current stream. | Contradictory. "Backups across providers" says other-provider streams use "a currently free line"; the hub contract says `backup_urls` uses "the session's allocated line" for every stream. The engine re-calls the streams endpoint on every `handoff()`, so a fresh, capacity-aware list per call is possible. | must |
| 3 | First-stream slot selection | `Channel.get_stream` walks streams in `channelstream__order`, and for each stream tries the default profile first, then the other active profiles; first free slot wins. 503 only when every profile of every stream is full (`apps/proxy/live_proxy/views.py`). | Contradictory. "Connection pools" walks all streams; the hub section says "one slot on one line of the provider of its first stream". | must |
| 4 | Search/replace on M3U accounts with several profiles | Every account has a default profile (`^(.*)$` -> `$1`) plus extra profiles, each with its own `max_streams` and regex pair (`apps/m3u/models.py` `M3UAccountProfile`, `create_profile_for_m3u_account`). This is how one M3U account becomes N logins. | Contradictory. SPEC allows `search`/`replace` per line but says an `m3u` provider has exactly one line, which makes the regex pointless. | must |
| 5 | Regex semantics | `transform_url` (`apps/proxy/live_proxy/url_utils.py`) converts JS-style `$1` and `$<name>` to Python, uses a regex timeout, and returns None (profile skipped) when the pattern does not match. Empty pattern = unchanged URL. | No. Users migrating from Dispatcharr will paste `$1`-style patterns. | must |
| 6 | Default User-Agent | Per-account UA, else a global default (`apps/m3u/models.py` `get_user_agent_string`, `core/models.py` stream settings); proxy fallback `VLC/3.0.20 LibVLC/3.0.20` (`apps/proxy/config.py`). Used for both API/playlist fetches and streaming. | No. `provider.user_agent` may be empty and the engine takes the UA as a required argv; an empty UA header is blocked by many panels. | must |
| 7 | Stable stream identity for M3U providers | Stream hash from a configurable field list (name, url, tvg_id, group, m3u_id); for XC accounts the provider `stream_id` replaces the URL so credential changes do not break identity (`apps/channels/models.py` `Stream.generate_hash_key`, `apps/m3u/utils.py` `parse_m3u_hash_keys`). | Partly. SPEC hashes the URL only. Many M3U URLs embed tokens or credentials that rotate, so every refresh would orphan every channel. | must |
| 8 | Live stream format per stream | Stream profiles (ffmpeg/streamlink/proxy) per account or channel; the proxy sniffs the upstream content type. | Partly. `format` is per provider. M3U playlists often mix `.m3u8` and raw TS URLs, and `hlsloop.py` only rewrites the Xtream `/live/u/p/id.ts` form to `.m3u8`. A `.ts` M3U URL under `format: hls` fails. | must |
| 9 | Start-of-stream behavior | The proxy registers the client and sends TS keepalive (null) packets while the channel initializes (`apps/proxy/live_proxy/views.py` around `_wait_for_initialization`, `apps/proxy/live_proxy/utils.py` null packet builder); `channel_init_grace_period` 60 s, `stream_startup_timeout` 8 s, `connection_timeout` 10 s (`core/models.py` proxy settings, `apps/proxy/config.py`). | No. SPEC does not say when `/stream/<uuid>.ts` sends headers or what flows before the engine's first output. Channels DVR gives up on a tune that sends nothing for several seconds. | must |
| 10 | Engine restart target | On upstream death the stream manager retries with a window (`MAX_RETRIES` 3, `RETRY_WINDOW_SECONDS` 1800), then switches stream (`MAX_STREAM_SWITCHES` 10, `FAILOVER_ROTATION_COOLDOWN` 60 s) (`apps/proxy/config.py`, `input/manager.py`). | Partly. SPEC restarts the engine up to 5 times per 10 min but does not say on which stream/line, or whether the slot is re-allocated. The engine persists its current stream in `/tmp/<engine>-<md5>.json` (`cur`), which the hub can read. | must |
| 11 | Shutdown delay and late joiners | `_wait_for_shutdown_delay` (`apps/proxy/live_proxy/server.py`) keeps the upstream alive and cancels shutdown if a client reconnects during the delay. Default 0 s. | Yes (`channel_shutdown_delay` 10). Just state that a reconnect inside the delay reuses the session and the slot. | covered |
| 12 | New-client join-behind | `new_client_behind_seconds` default 5 (`core/models.py`, `apps/proxy/live_proxy/config_helper.py`), applied by the proxy on top of whatever the stream profile emits. | Yes, but SPEC defaults to 30. The engine was tuned behind Dispatcharr's proxy, so the default should equal the value used in production Dispatcharr, and the engine already keeps its own client lead and multi-viewer head start (`watch()`, `xb`). | verify |
| 13 | Ghost/slow client cleanup | Client heartbeats, `CLIENT_RECORD_TTL` 60 s, ghost detection at 50 s, `STREAM_TIMEOUT` 20 s (`apps/proxy/config.py`, `client_manager.py`). | Partly ("slow clients are dropped"). No threshold. | must (one number) |
| 14 | Provider account info | XC `user_info` (status, `exp_date`, `max_connections`, `active_cons`, allowed output formats) stored per profile, refreshed, expiration warnings (`apps/m3u/models.py`, `apps/m3u/tasks.py` `refresh_account_info`, `check_account_expirations`). | Only "test connection". | nice (prefill `max_streams` from `max_connections`, show expiry on Status) |
| 15 | Group discovery and new groups | Groups tracked per account with `enabled`, `last_seen`; stale group relations cleaned; option `auto_enable_new_groups_live` (`apps/m3u/tasks.py` `process_groups`, `cleanup_stale_group_relationships`). | Partly. Selection by group name; no rule for groups that are renamed or new. | nice (show "new groups since last refresh"; renamed group = unselected) |
| 16 | Stream filters | Ordered regex include/exclude filters on group, name or URL per account (`apps/m3u/models.py` `M3UFilter`, `tasks.py` `_stream_passes_m3u_filters`). | No. | nice |
| 17 | Auto channel sync | Per group: auto create/update/delete channels on refresh, start/end number range, rename regex, name-match regex, group override, logo/EPG override, sort order, compact numbering, UUIDs preserved (`apps/m3u/tasks.py` `sync_auto_channels`, `apps/channels/models.py` `ChannelGroupM3UAccount`). | Partly (`auto_add` per provider as disabled channels). | nice (per-group start number is the only piece worth adding) |
| 18 | Stale streams | Removed after `stale_stream_days` (7) unseen (`apps/m3u/models.py`, `tasks.py` `cleanup_streams`). | Yes, differently: vanished streams are flagged, never deleted. Fine for an appliance. Add: auto re-link a vanished stream when a stream with the same tvg-id+name reappears under a new id. | nice |
| 19 | EXTINF parsing | Case-insensitive attributes, `#EXTGRP`, `#EXTVLCOPT` kept as `vlc_opts`, `tvg-chno`/`channel-number`, `tvg-shift`, catch-up flags (`apps/m3u/tasks.py` `parse_extinf_line`, `iter_m3u_entries`). | Mostly. Missing `#EXTGRP`, `#EXTVLCOPT` and case-insensitive keys. | must (`#EXTGRP`, case-insensitive); nice (`#EXTVLCOPT:http-user-agent`/`http-referrer` passed as UA) |
| 20 | Local M3U file | Account may use an uploaded file instead of a URL (`file_path`). | No. | nice (path under `/data/playlists/`) |
| 21 | Provider channel number | `stream_chno` from XC `num` or M3U `tvg-chno`, used for ordering auto-created channels (`apps/channels/models.py` `Stream`). | No. | nice (sort/auto-number by provider number) |
| 22 | Shared logins across providers | `ServerGroup` and credential fingerprints share one counter when two accounts use the same login (`apps/m3u/connection_pool.py`). | No. Matters only if a user adds the same login twice (for example once as Xtream and once as M3U). | nice (warn on duplicate username+host) |
| 23 | Engine credential rewrite on same host | n/a (Dispatcharr builds each alternate URL with its own profile). | Conflict. `feeds()` in both engines rewrites `/live/<u>/<p>/` of every backup on the same host to the session's credentials. Two coax providers on the same panel host with different logins would have their backups silently moved onto the session's login. | must (document: same host = same provider; or hub returns URLs on another host only) |
| 24 | M3U output attributes | `#EXTM3U x-tvg-url=".." url-tvg=".."`, then `tvg-id`, `tvg-name`, `tvg-logo`, `tvg-chno`, optional `tvc-guide-stationid`, `group-title`; Content-Type `audio/x-mpegurl`; stream URL `/proxy/ts/stream/<uuid>` (`apps/output/views.py` `generate_m3u`). Does not emit `channel-id`. | Yes and better (`channel-id`, `channel-number`). Missing: `x-tvg-url` header, `tvg-chno` alias, content type, attribute escaping. | must (escaping, content type); nice (header, `tvg-chno`) |
| 25 | Name consistency the engine depends on | Dispatcharr emits `tvg-name` equal to the name after the comma. The engine's viewer buffer (`watch()` in both engines) maps a channel by comparing Channels DVR `GuideName` with the text after the comma and requires the playlist entry count to equal the Channels device channel count. | No. If coax ever emits a `tvg-name` different from the display name, or a name containing `"`, the mapping drifts and the viewer buffer and recording extension switch off. | must |
| 26 | Logos | Logos cached and served by Dispatcharr (`cachedlogos=true` default), direct URL optional (`apps/output/views.py`). | Direct provider URL only. Provider logo hosts are often HTTP-only or dead. | nice (logo override per channel already in schema; no cache needed) |
| 27 | XMLTV sources independent of providers | EPG sources are their own objects (XMLTV URL or file, Schedules Direct, dummy) with priority; any channel can map to any source (`apps/epg/models.py`, `tasks.py`). | No. SPEC ties XMLTV to a provider (`guide.xmltv_provider`). Third-party guides (epg.best, iptv-org, a Channels user's own XMLTV) are common for non-US channels. | must (a guide-only source list; small) |
| 28 | XMLTV import formats | `.gz`, `.zip`, `.xz` and plain (`apps/epg/tasks.py` extract helpers). | gzip or plain. | nice (stdlib `zipfile`, `lzma`) |
| 29 | XMLTV programme fidelity | Programmes stored and re-emitted with title, sub-title, desc, categories, episode-num, icon and custom fields (`apps/output/epg.py`). | Partly. SPEC says rewrite ids; does not say every programme child element is copied verbatim. Channels DVR needs `category` (Movie, Sports, News, Children), `series-id`/`episode-num` to group and record correctly. | must (state "copy verbatim") |
| 30 | XMLTV window | `days`/`prev_days` query parameters trim output (`apps/output/epg.py`). | No. Large provider guides make the file big but Channels handles it. | nice (keep now minus 1 day onward) |
| 31 | EPG auto-match | Exact `tvg_id` match, then normalized fuzzy name with region bonus, optional ML (`apps/channels/epg_matching.py`). | Partly. Gracenote suggest is covered by the reference matchers; XMLTV is a manual picker. | must (prefill `xmltv_id` from the stream's `tvg-id` when it exists in the source); nice (name fuzzy) |
| 32 | Dummy EPG | Generates placeholder programmes for unmatched channels (`apps/output/dummy_epg.py`). | No. Channels DVR creates its own placeholder airings when a channel has no guide. | out (optionally emit `tvc-guide-placeholders`) |
| 33 | Channel numbering | Float numbers (2.1), uniqueness, compact renumbering, next free number (`apps/channels/compact_numbering.py`, `models.py` `get_next_available_channel_number`). | Partly (string number, auto-number). Uniqueness within a lineup not stated. | must (unique per lineup, decimals allowed) |
| 34 | Channel profiles / output subsets | Channel profiles select which channels each M3U/EPG URL exposes (`apps/output/views.py` profile_name). | Yes (lineups). | covered |
| 35 | Refresh scheduling | Per-account `refresh_interval` hours (0 = off), status and last message, single refresh at a time, retries on DB errors (`apps/m3u/tasks.py`). | Yes. Add "0 = manual only". | covered |
| 36 | HDHomeRun emulation | `apps/hdhr` exposes discover/lineup JSON for HDHR tuners. | No. Channels DVR reads M3U directly; HDHR adds nothing here. | out |
| 37 | VOD, series, catch-up/timeshift, Xtream output API, users and per-user profile access, plugins, backups, preemption, DVR inside Dispatcharr, Schedules Direct, stream preview | `apps/vod`, `apps/timeshift`, `apps/output/views.py` `xc_*`, `apps/accounts`, `apps/plugins`, `apps/backups`, `apps/epg/sd_*`. | No. | out |

## Things in SPEC.md that conflict with how Channels DVR consumes M3U/XMLTV

1. **`guide.mode: both`.** Channels DVR treats `tvc-guide-stationid` as the guide for that channel (Gracenote data
   from Channels' own service). `tvg-id` is only used to match XMLTV when no station id is set. Emitting both does
   not merge or fall back per program. Keep `both` only if it is defined as "station id in the M3U, XMLTV kept as a
   documented manual fallback", otherwise drop it. Verify against a live Channels DVR before freezing the schema.
2. **Size of a lineup.** Channels DVR documents a 750-channel limit per M3U source. The UI should warn when a
   lineup exceeds 750 enabled channels and point users to splitting it into lineups (each added as its own source).
3. **Channel number collisions.** Channels DVR merges channels with the same number across sources and keeps the
   prioritized one. Numbers must be unique within a lineup, and the UI should warn that numbers colliding with
   another Channels DVR source get merged.
4. **Source stream format.** coax always serves MPEG-TS at `/stream/<uuid>.ts`. The README must tell users to pick
   "MPEG-TS" (not HLS) when adding the M3U source, and to paste the coax XMLTV URL into that same source's XMLTV
   field (not as a separate guide lineup).
5. **`tvg-name`.** Channels DVR reads `tvg-name` as the call sign / guide name. The engine's viewer buffer compares
   that guide name with the text after the comma, so `tvg-name` and the display name must be identical
   (see gap 25).
6. **Stream URL contract and the engine.** The engine finds its channel by looking for `/output/m3u` in the
   Channels DVR source URL and the channel uuid in the stream URL. This holds for
   `/output/m3u/<lineup>.m3u` plus `/stream/<uuid>.ts`. It breaks if users add the M3U through any other path
   (for example a reverse proxy rewriting the path), so the README should say to use the URL exactly as shown.
7. **Engine file writes.** `watch()` writes `hlsloop-jobs-<id>.json`/`tsloop-jobs-<id>.json` to `/data` when it is
   writable. In coax `/data` holds `config.json`; point this at `/data/cache/` (an allowed minimal adaptation).

No other conflicts found: `channel-id`, `channel-number`, `tvg-logo`, `group-title`, `tvc-guide-stationid`,
`tvg-id` matching `<channel id>` and `<programme channel>` all match the Channels DVR custom channel docs.

## Spec additions (ready to paste into SPEC.md)

Under **Groups, refresh, and connection pools**:

- **Slot follows the stream.** A session holds exactly one slot at a time. When the engine moves to a stream on a
  different provider or line, the hub moves the slot: it reserves the new line before the URL is handed out and
  releases the old one after the switch. The hub learns the engine's current stream from the engine's state file
  (`cur`) or from its next call to the internal streams endpoint, whichever is simpler; the line used for a URL is
  recorded when the URL is issued.
- **Tune order.** On a new tune the hub walks the channel's streams in order; for each stream it tries that
  provider's lines in order (first line first) and takes the first line with a free slot. 503 only when no stream
  has a free line. This replaces "the provider of its first stream" in the hub section.
- **Backup list.** `GET /internal/channels/{id}/streams` is computed fresh on every call: the session's current
  stream first, then the remaining streams in channel order, starting after the current one. Streams on the
  session's provider use the session's line. Streams on other providers are included only if that provider has a
  line with a free slot right now, built with that line. Inactive providers are skipped.
- **M3U lines.** An `m3u` provider may have several lines like an `xtream` provider. Each line has its own
  `max_streams` and an optional regex `search`/`replace` applied to the stream URL. Empty pattern means the URL is
  used unchanged. A pattern that does not match makes that line unusable for that stream (skip it, log once).
  Replacement accepts `$1` and `$<name>` (Dispatcharr style) as well as `\1` and `\g<name>`. Regex evaluation is
  bounded (reject patterns that fail to compile at save time).
- **Duplicate logins.** When two lines (in any providers) share the same host and username, the UI warns that
  their `max_streams` are not shared and that the engine treats same-host backups as the session's login.

Under **providers**:

- **User-Agent.** Add `server.default_user_agent` (default `"VLC/3.0.20 LibVLC/3.0.20"`). A provider's
  `user_agent` overrides it. The resolved value is used for player_api, playlist, XMLTV fetches and as the engine's
  UA argument; it is never empty.
- **Stream identity.** Xtream `stream_id` is the provider's `stream_id`. M3U `stream_id` is a SHA-256 (first 16 hex
  chars) of `tvg-id + "\0" + name + "\0" + group` when `tvg-id` is non-empty, otherwise of `name + "\0" + group`,
  with the URL appended only to break ties between otherwise identical entries in one playlist. The URL is
  refreshed on every catalog refresh and is not part of identity.
- **Parsing.** `#EXTINF` attribute keys are case-insensitive. `#EXTGRP:` sets the group when `group-title` is
  missing. `#EXTVLCOPT:http-user-agent=` on an entry overrides the UA for that stream. `tvg-chno`/`channel-number`
  and Xtream `num` are stored as `provider_number` for sorting.
- **Per-stream format.** `format` gains `auto` (new default): a URL whose path ends in `.m3u8` uses `hlsloop.py`,
  an Xtream `/live/...ts` URL uses `hlsloop.py` (the engine rewrites it), anything else uses `tsloop.py`. `hls`
  and `ts` force the engine.
- **Account info.** "Test connection" stores Xtream `user_info.max_connections`, `exp_date` and `status`; adding a
  line prefills `max_streams` from `max_connections`; the Status page shows expiry.
- `refresh_hours: 0` means manual refresh only.

Under **guide**:

- **Guide sources.** Add top-level `"guide_sources": [{"id": "g1", "name": "...", "url": "..."}]` for XMLTV feeds
  not tied to a provider. `guide.xmltv_provider` may reference a provider id or a guide source id. Accept `.gz`,
  `.zip` (first `.xml` member), `.xz` and plain XML, detected by magic bytes, not extension.
- **Programme fidelity.** Every `<programme>` child element and attribute is copied verbatim; only the `channel`
  attribute is rewritten. `<channel>` output contains `display-name` (the coax channel name) and `icon` (the
  channel logo when set).
- **Auto prefill.** When a channel is created from a stream with a `tvg-id` that exists in the provider's XMLTV,
  set `guide.xmltv_id` to it and `guide.mode` to `xmltv` unless a Gracenote suggestion is accepted.

Under **output.m3u**:

- First line `#EXTM3U x-tvg-url="{base_url}/output/xmltv/<lineup>.xml"`. Each entry also carries
  `tvg-chno` equal to `channel-number`. `tvg-name` is exactly the display name after the comma. In attribute values,
  `"` is replaced with `'` and newlines with spaces; the display name has newlines removed. Response
  `Content-Type: audio/x-mpegurl; charset=utf-8`.
- Channel numbers are unique within a lineup (decimals like `4.1` allowed). The UI warns above 750 enabled channels
  in a lineup.

Under **hub**:

- **Tune start.** `/stream/<uuid>.ts` answers `200` with headers as soon as a slot is reserved and sends TS null
  packets every 0.5 s until the engine's first bytes arrive. If the engine sends nothing within 20 s, the session
  is closed and the slot released.
- **Restarts.** A restart re-runs slot allocation starting from the engine's last current stream; the old slot is
  released first. After 5 restarts in 10 minutes the session ends and clients are disconnected.
- **Slow clients.** A client whose send buffer has been full for 10 s is dropped.
- **Shutdown delay.** A client that arrives during `channel_shutdown_delay` joins the existing session; no new slot
  is taken.
- `new_client_behind_seconds` default must equal the value the engine ran behind in production Dispatcharr
  (Dispatcharr's own default is 5); confirm before release.

Under **engine**:

- The recording job file (`*-jobs-<id>.json`) is written to `/data/cache/`, not `/data/`.
