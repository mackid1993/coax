# coax HTTP API

All JSON API routes live under `/api/`. Request bodies are JSON (`Content-Type: application/json`). Errors return
`{"error": "message"}` with status 400 (validation), 403 (internal API from a non-local address), 404 (unknown
id), 405, 409 (conflict), 413 (body over 16 MB), 502 (Channels DVR failed), 503 (module or hub unavailable)
or 500. Passwords are always returned as `"********"`. Sending `"********"` back keeps the stored password.

## Plain routes

| Method | Path | Response |
|---|---|---|
| GET | `/` and static files from `app/web/` | `index.html` and other `.html` use `Cache-Control: no-cache`, files under `vendor/` use `public, max-age=604800`, everything else uses `no-cache`. All have ETag (304 on `If-None-Match`). |
| GET | `/healthz` | `ok` (text/plain) |
| GET | `/output/m3u/<lineup>.m3u` | `audio/x-mpegurl` from `app.guide.output.m3u` (404 when the lineup is unknown) |
| GET | `/output/xmltv/<lineup>.xml` | cached XMLTV file, or an empty `<tv>` document when there is none yet |
| GET | `/stream/<uuid>.ts` | `video/mp2t`, no Content-Length, `Connection: close`. Returns 503 when the channel is unknown or disabled, or when no line is free. |
| GET | `/internal/channels/<id>/streams[/]` | `[{"url": "..."}]` (hub.backup_urls, else every stream built with the first line). Only from 127.0.0.1. |
| GET | `/internal/channels/<id>[/]` | `{"id", "uuid", "name", "number"}`. Only from 127.0.0.1. |

## Config

| Method | Path | Body | Response |
|---|---|---|---|
| GET | `/api/config` | | full config, masked (includes `next_channel_id` and `xmltv_sources`) |
| PUT | `/api/config` | partial config. `server` and `channels_dvr` are merged key by key, other top-level keys replace. Usual use: `{"server": {...}, "channels_dvr": {"url"}, "transcode"}` | full masked config |

Settings fields: `server.port` (1-65535; takes effect after a restart), `server.base_url` (http(s) URL or empty),
`server.new_client_behind_seconds` (0-600), `server.channel_shutdown_delay` (0-3600), `channels_dvr.url`,
`transcode` (`off`, `1080p60`, `1080p30`, `720p60`, `720p30`, `1080p50`, `1080p25`, `720p50`, `720p25`).

## Providers

Provider view = the config provider (masked) plus `catalog: {fetched_at, streams, groups} | null` and
`refresh: <refresh result> | null`.

| Method | Path | Body | Response |
|---|---|---|---|
| GET | `/api/providers` | | `{"providers": [provider view]}` |
| POST | `/api/providers` | `{name, type: "xtream"\|"m3u", server, m3u_url, xmltv_url, user_agent, format: "hls"\|"ts", refresh_hours, auto_add, groups, lines: [{name, username, password, max_streams, search, replace}]}`. Ids (`p1`, `l1`, ...) are assigned. M3U without lines gets one unlimited line. | provider view |
| GET | `/api/providers/<pid>` | | provider view |
| PUT | `/api/providers/<pid>` | same fields as create (any subset). Lines keep their `id`; a line without `id` is new. | provider view |
| DELETE | `/api/providers/<pid>[?force=1]` | | `{"deleted": pid, "channels_detached": n}`. Returns 409 when channels use it; with `force=1` those stream refs are removed. |
| POST | `/api/providers/<pid>/test` | | `{"ok": bool, "message": str, ...}` (Xtream: `status`, `exp_date`, `max_connections`, `active_cons`; M3U: `streams`; on failure `error`) |
| POST | `/api/providers/test` | unsaved provider (same as create; include `id` to reuse masked stored passwords) | same as above |
| POST | `/api/providers/<pid>/refresh[?wait=1]` | optional `{"wait": true}` | without wait: `{"provider", "state": "running"}`. With wait: refresh result. |
| GET | `/api/providers/<pid>/groups` | | `{"provider", "fetched_at", "groups": [{"name", "count", "selected"}]}` in provider order |
| PUT | `/api/providers/<pid>/groups` | `{"groups": ["US\| NEWS", ...]}` | same as GET |

Refresh result: `{provider, name, state: "running"|"ok"|"error", ok, error, started_at, at, finished_at,
seconds, streams, groups, added (new streams in selected groups), vanished: [stream_id] (max 500),
vanished_count, updated, auto_added}`.

## Catalog

`GET /api/catalog/streams?provider=&group=&q=&offset=0&limit=100&all_groups=0`

* `provider` (optional) limits the results to one provider. `group` matches one exact group name. `q` is matched
  word by word against name, tvg_id and stream_id.
* Only selected groups are included unless `all_groups=1`. `limit` is 1-1000.
* Response: `{"total", "offset", "limit", "items": [{"provider", "stream_id", "name", "group", "logo",
  "tvg_id", "channels": [channel ids already using it]}]}`

## Channels

Channel view = config channel plus `lineups: [lineup ids]`, `missing_streams: n`, and on each stream
`{provider, stream_id, name, group, missing}`. `missing` is true when the stream vanished from the catalog.
Flagged channels are never deleted.

| Method | Path | Body | Response |
|---|---|---|---|
| GET | `/api/channels` | | `{"channels": [channel view], "next_id"}` (config order) |
| POST | `/api/channels` | `{name, number?, group?, logo?, enabled?, streams?: [{provider, stream_id}], guide?: {mode, station_id, xmltv_id, xmltv_provider}, lineups?: [ids]}`. Default lineup is the first one, default number is the highest + 1. | channel view |
| GET | `/api/channels/<id>` | | channel view |
| PUT | `/api/channels/<id>` | any of `number, name, group, logo, enabled, streams, guide (merged), lineups` | channel view |
| DELETE | `/api/channels/<id>` | | `{"deleted": [id]}` |
| POST | `/api/channels/bulk-add` | `{streams: [{provider, stream_id}], start_number?, group?, enabled? (true), guide_mode? ("none"), lineups?, allow_duplicates? (false)}` | `{"created": [ids], "skipped": [refs already used]}` |
| POST | `/api/channels/bulk-update` | `{ids: [..], set: {enabled?, group?, logo?, guide?}}` | `{"updated": ids}` |
| POST | `/api/channels/bulk-delete` | `{ids: [..]}` | `{"deleted": ids}` |
| POST | `/api/channels/reorder` | `{ids: [..]}` (the listed channels take their own positions in the new order; a full list sets the whole order; lineup order follows) | `{"order": [all ids]}` |
| POST | `/api/channels/auto-number` | `{start? (1), step? (1), ids? (default all, in list order), only_enabled? (false)}` | `{"numbers": {"<id>": "101", ...}}` |
| POST | `/api/channels/<id>/streams` | `{streams: [{provider, stream_id}], mode?: "append"\|"replace"}` (attach backups; duplicates ignored) | channel view |

Channel ids are positive integers that are never reused (`next_channel_id`). Each channel gets a uuid4 when it
is created. Numbers look like `101` or `101.1`. Guide mode is one of `gracenote`, `xmltv`, `both` or `none`.

## Guide

| Method | Path | Response |
|---|---|---|
| GET | `/api/gracenote/search?q=` | `{"results": [station]}` (station: `station_id, call_sign, name, type, affiliate, logo, lineups, countries, outside`) |
| GET | `/api/gracenote/suggest?channel=<id>` or `?name=`, optional `countries=USA:10001,CAN:M5V3L9`, `wide=1` | `{"result": {"status": "matched"\|"review"\|"unmatched", "reason", "best", "candidates": [station + confidence, reason]}}` |
| POST | `/api/gracenote/bulk-suggest` body `{ids?: [..], countries?: "USA:10001" \| [["USA","10001"]]}`. Default ids: channels without a station id whose guide mode is not xmltv. | `{"results": {"<channel id>": {status, reason, best, candidates, channel_id, name}}}` |
| GET | `/api/xmltv/ids?provider=<pid>&q=&limit=200` | `{"provider", "total", "items": [{"id", "name", "icon"?}]}` (from the cached picker list) |

The Channels DVR URL must be set first. Otherwise these routes return 400 with
"set the Channels DVR URL in Settings first".

## XMLTV sources (standalone guide URLs, Settings)

Config: top-level `xmltv_sources: [{"id": "x1", "name", "url", "refresh_hours": 24}]`. Source ids share a
namespace with provider ids, so a channel's `guide.xmltv_provider` may be a provider id or a source id, and
`/api/xmltv/ids?provider=` (or `?source=`) accepts both.

| Method | Path | Body | Response |
|---|---|---|---|
| GET | `/api/xmltv/sources` | | `{"sources": [source]}` |
| POST | `/api/xmltv/sources` | `{name, url, refresh_hours?}`. The id (`x1`, `x2`, ...) is assigned. | source |
| PUT | `/api/xmltv/sources/<id>` | any of `name, url, refresh_hours` | source |
| DELETE | `/api/xmltv/sources/<id>` | | `{"deleted": id, "channels_cleared": n}` (channels using it get empty `xmltv_id`/`xmltv_provider`) |

## One-screen channel setup

`POST /api/automatch` body `{ids: [channel ids], countries?: "USA:10001"}` ->
```json
{"<channel id>": {"guide": [gracenote candidate, ...], "guide_status": "matched|review|unmatched|error",
                  "guide_reason": "...", "guide_best": candidate | null,
                  "backups": [{"provider", "stream_id", "name", "score", "reason"}]}}
```
Guide candidates come from `app.guide.gracenote.bulk_suggest` and are ranked, with `confidence` from 0 to 1.
Backups come from `app.backups.suggest(channel, catalogs)`, where `catalogs` is `{provider_id: catalog}`.
`backups` is `[]` when that module is missing. On failure an entry also carries `guide_error` or `backups_error`.
Nothing is saved.

`POST /api/channels/apply` body
`{channels: [{id, guide?: {mode, station_id, xmltv_id, xmltv_provider}, streams?: [{provider, stream_id}, ...]}]}`
-> `{"updated": [ids], "channels": [channel view]}`. `guide` is merged, and `streams` replaces the channel's
ordered list (primary first, then backups). Everything is validated first and saved in one atomic config write.
If any entry is invalid, the response is 400 and nothing is saved.

## Lineups

A lineup's `guide` is one of `gracenote`, `xmltv`, `both` or `per-channel` (the default; uses each channel's
`guide.mode`). `gracenote` and `both` put `tvc-guide-stationid` on every channel that has a station id, `xmltv`
never does. The output URLs are `/output/m3u/<id>.m3u` and `/output/xmltv/<id>.xml`.

| Method | Path | Body | Response |
|---|---|---|---|
| GET | `/api/lineups` | | `{"lineups": [{"id", "name", "guide", "channels": [ids]}]}` |
| POST | `/api/lineups` | `{name, id?, guide?, channels?: [ids], all_channels?: bool}`. The id comes from the name when it is not given. | lineup |
| PUT | `/api/lineups/<id>` | `{name?, guide?, channels?}` | lineup |
| DELETE | `/api/lineups/<id>` | | `{"deleted": id}` |
| GET | `/api/lineups/<id>/dvr` | | `{"key", "dvr", "last_sync", "defaults": {guide, limit, default_limit, numbering, start_number, logos, name}, "dvr_url_set", "base_url_set", "in_dvr", "source", "error"}` |
| POST | `/api/lineups/<id>/dvr` | `{guide: "gracenote"\|"xmltv"\|"gracenote_xmltv", limit?, numbering?: ""\|"ignore", start_number?, logos?: ""\|"guide", name?}` (missing fields use `defaults`) | `{"key", "name", "created", "refreshed", "guide_lineup", "guide_started", "url", "xmltv_url"}` |
| POST | `/api/lineups/<id>/dvr/sync` | | `{"at", "ok", "error", "refreshed", "guide_started", "source_updated", "guide_lineup"?}` |

### Channels DVR source per lineup

`POST /api/lineups/<id>/dvr` creates or updates the custom M3U source `coax<id letters and digits>` in Channels
DVR (`PUT {dvr}/providers/m3u/sources/<key>`), reloads it (`POST .../<key>/refresh`), then looks up its DVR
lineup in `GET {dvr}/dvr/lineups` (key `M3U-<key>`, up to 5 tries) and starts a guide download with
`PUT {dvr}/dvr/lineups/<lineup id>`. Guide options: `gracenote` (station IDs, no XMLTV URL), `xmltv`
(`guide_preference: xmltv` plus the lineup's XMLTV URL) and `gracenote_xmltv` (station IDs preferred, XMLTV URL as
fallback). The choice is saved as the lineup's `guide` (`gracenote`, `xmltv`, `both`) and the source options
as `lineup.dvr: {key, name, limit, numbering, start_number, logos, added_at}`. The default stream limit is the sum
of line limits of the lineup's providers, each capped by the provider limit (empty when any is unlimited).

After any config change (3 s debounce), every lineup with `dvr` set is compared with what was last synced: a
changed playlist reloads the source; new channels, changed guide ids or a changed lineup guide also rebuild the
XMLTV file (xmltv and both lineups) and start a DVR guide download. A changed guide mode or base URL rewrites the
source first. Failures are logged under `coax.dvr` and shown as `last_sync`.

Errors: 400 when `channels_dvr.url` or `server.base_url` is empty or an option is invalid, 404 for an unknown
lineup, 502 with the DVR's message when the DVR rejects a request or cannot be reached.

## Status and log

`GET /api/status` ->
```json
{"uptime": 12.3, "hub_running": true,
 "sessions": [hub.status() items: {channel_id, uuid, channel, state, provider_id, provider, line_id, line,
              stream_id, uptime, bytes, bitrate_bps, buffer_bytes, restarts, engine_pid,
              clients: [{ip, user_agent, connected, bytes}]}],
 "lines": [{"provider", "provider_name", "line", "line_name", "used", "max", "channels": [{"id", "name"}]}],
 "refresh": {"<pid>": refresh result (manual or scheduled, newest wins)},
 "channels": {"total": 10, "enabled": 8}}
```
`max` 0 means unlimited.

`GET /api/log?since=<seq>&limit=500` returns `{"lines": [{"seq", "ts", "level", "name", "msg"}], "last_seq"}`.
The server keeps the last 2000 log lines in memory. To poll, pass the previous `last_seq` as `since`.
