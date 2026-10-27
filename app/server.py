"""HTTP server: tuner streams, M3U/XMLTV output, the engine's internal API, the JSON API and the web UI.

Stdlib only (http.server.ThreadingHTTPServer). Other coax modules are imported lazily so the server keeps running,
with a clear error on the affected routes, if one of them fails to import.

See API.md for the full list of routes with request and response shapes.
"""

import collections
import copy
import email.utils
import importlib
import inspect
import json
import socket
import logging
import os
import posixpath
import re
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app import config as configmod

log = logging.getLogger("coax.server")

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
MAX_BODY = 16 * 1024 * 1024
STREAM_CHUNK_TIMEOUT = 60
DISCONNECT_ERRORS = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError)
LOCAL_ADDRESSES = ("127.0.0.1", "::1", "::ffff:127.0.0.1")

MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".map": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/plain; charset=utf-8",
}


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def bad_request(message):
    return ApiError(400, message)


def not_found(message="not found"):
    return ApiError(404, message)


def lazy(module_name):
    """Import a coax module on first use. Raises ApiError 503 when it is missing or broken."""
    try:
        return importlib.import_module(module_name)
    except Exception as exc:  # noqa: BLE001 - any import failure becomes a clear 503
        log.error("module %s is not available: %s", module_name, exc)
        raise ApiError(503, "%s is not available: %s" % (module_name, exc))


def call_flexible(func, *args, **kwargs):
    """Call func passing only the keyword arguments its signature accepts."""
    try:
        params = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return func(*args, **kwargs)
    if any(p.kind == p.VAR_KEYWORD for p in params.values()):
        return func(*args, **kwargs)
    return func(*args, **{k: v for k, v in kwargs.items() if k in params})


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------- log ring buffer

class RingLogHandler(logging.Handler):
    """Keeps the last N formatted log records in memory for the Status page."""

    def __init__(self, capacity=2000):
        super().__init__()
        self.records = collections.deque(maxlen=capacity)
        self.seq = 0
        self._ring_lock = threading.Lock()

    def emit(self, record):
        try:
            message = record.getMessage()
            if record.exc_info:
                message += "\n" + logging.Formatter().formatException(record.exc_info)
            entry = {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created))
                + ".%03d" % int(record.msecs),
                "level": record.levelname,
                "name": record.name,
                "msg": message,
            }
            with self._ring_lock:
                self.seq += 1
                entry["seq"] = self.seq
                self.records.append(entry)
        except Exception:  # noqa: BLE001 - logging must never raise
            self.handleError(record)

    def tail(self, since=0, limit=500):
        with self._ring_lock:
            items = [r for r in self.records if r["seq"] > since]
            last = self.seq
        if limit and len(items) > limit:
            items = items[-limit:]
        return {"lines": items, "last_seq": last}


LOG_BUFFER = RingLogHandler(10000)


# ---------------------------------------------------------------- catalog cache

class CatalogCache:
    """Keeps parsed provider catalogs in memory, reloading when the cache file changes."""

    def __init__(self, data_dir):
        self.data_dir = data_dir
        self._lock = threading.Lock()
        self._entries = {}

    def _path(self, provider_id):
        return os.path.join(self.data_dir, "cache", "catalog-%s.json" % provider_id)

    def invalidate(self, provider_id=None):
        with self._lock:
            if provider_id is None:
                self._entries.clear()
            else:
                self._entries.pop(provider_id, None)

    def get(self, provider_id):
        """Return an entry dict {catalog, by_id, group_counts} or None when there is no catalog yet."""
        try:
            stamp = os.stat(self._path(provider_id)).st_mtime_ns
        except OSError:
            stamp = None
        with self._lock:
            entry = self._entries.get(provider_id)
            if entry and (entry["stamp"] == stamp if stamp is not None else time.time() - entry["at"] < 5):
                return entry
        try:
            catalog = lazy("app.providers.catalog").load(provider_id)
        except ApiError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("could not load catalog for %s: %s", provider_id, exc)
            catalog = None
        if not catalog:
            return None
        streams = catalog.get("streams") or []
        by_id = {}
        group_counts = collections.Counter()
        for stream in streams:
            by_id[str(stream.get("stream_id"))] = stream
            group_counts[stream.get("group") or ""] += 1
        entry = {"stamp": stamp, "at": time.time(), "catalog": catalog, "by_id": by_id,
                 "group_counts": group_counts}
        with self._lock:
            self._entries[provider_id] = entry
        return entry


# ---------------------------------------------------------------- application state

class App:
    """Everything the request handler needs. One per server."""

    def __init__(self, store, hub=None, log_buffer=None):
        self.store = store
        self.hub = hub
        self.log_buffer = log_buffer or LOG_BUFFER
        self.catalogs = CatalogCache(store.data_dir)
        self.started = time.time()
        self.refresh_lock = threading.Lock()
        self.refresh_results = {}   # provider id -> last result dict
        self.refreshing = set()
        self.dvr = None             # app.channels_dvr.Syncer, set by main (created on demand otherwise)
        self.automatch_job = None   # {id, state, done, total, phase, started, finished, result, error}
        self.automatch_lock = threading.Lock()

    # -- refresh
    def start_refresh(self, provider_id, wait=False):
        cfg = self.store.get()
        provider = find_provider(cfg, provider_id)
        with self.refresh_lock:
            if provider_id in self.refreshing:
                return {"provider": provider_id, "state": "running"}
            self.refreshing.add(provider_id)
            self.refresh_results[provider_id] = dict(self.refresh_results.get(provider_id) or {},
                                                     state="running", started_at=now_iso())
        if wait:
            return self._refresh(copy.deepcopy(provider))
        threading.Thread(target=self._refresh, args=(copy.deepcopy(provider),), name="refresh-" + provider_id,
                         daemon=True).start()
        return {"provider": provider_id, "state": "running"}

    def _refresh(self, provider):
        pid = provider["id"]
        started = time.time()
        result = {"provider": pid, "name": provider.get("name", ""), "started_at": now_iso(), "at": now_iso()}
        try:
            catalog_module = lazy("app.providers.catalog")
            old_entry = self.catalogs.get(pid)
            old_ids = set(old_entry["by_id"]) if old_entry else None
            report = catalog_module.refresh(provider) or catalog_module.load(pid) or {}
            self.catalogs.invalidate(pid)
            streams = report.get("streams") or []
            new_ids = {str(s.get("stream_id")) for s in streams}
            selected = set(provider.get("groups") or [])
            if "added_selected" not in report:
                # Older catalog modules return only the catalog; work out the change report here.
                previous = old_ids if old_ids is not None else set()
                report = dict(report)
                report["added"] = [str(s.get("stream_id")) for s in streams
                                   if str(s.get("stream_id")) not in previous]
                report["added_selected"] = [str(s.get("stream_id")) for s in streams
                                            if str(s.get("stream_id")) not in previous
                                            and s.get("group") in selected]
                report["removed"] = sorted(previous - new_ids)
            removed = list(report.get("removed") or [])
            result.update(ok=True, error="", state="ok", streams=len(streams),
                          groups=len(report.get("groups") or []),
                          added=len(report.get("added_selected") or []),
                          vanished=removed[:500], vanished_count=len(removed),
                          updated=report.get("updated", 0))
            if old_ids is not None:
                auto = self.on_refresh(provider, report)
                if auto:
                    result["auto_added"] = auto
        except Exception as exc:  # noqa: BLE001 - a refresh failure keeps the old catalog
            message = exc.message if isinstance(exc, ApiError) else redact(str(exc), provider)
            result.update(ok=False, state="error", error=message)
            log.error("refresh of %s failed, keeping the last good catalog: %s", provider.get("name") or pid,
                      message)
        result["finished_at"] = now_iso()
        result["seconds"] = round(time.time() - started, 2)
        with self.refresh_lock:
            self.refreshing.discard(pid)
            self.refresh_results[pid] = result
        return result

    def on_refresh(self, provider, report):
        """Hook after every successful refresh (manual or scheduled): auto_add new streams. Returns count."""
        self.catalogs.invalidate(provider.get("id"))
        try:
            cfg = self.store.get()
            current = next((p for p in cfg["providers"] if p["id"] == provider.get("id")), None)
            if not current or not current.get("auto_add"):
                return 0
            ids = [str(i) for i in report.get("added_selected") or []]
            streams = report.get("streams") or []
            if not ids or (streams and len(report.get("added") or []) >= len(streams)):
                return 0   # nothing new, or a first download (everything counts as new)
            by_id = {str(s.get("stream_id")): s for s in report.get("streams") or []}
            entry = None if by_id else self.catalogs.get(current["id"])
            if entry:
                by_id = entry["by_id"]
            return self._auto_add(current["id"], [by_id[i] for i in ids if i in by_id])
        except Exception:  # noqa: BLE001 - never break a refresh
            log.exception("auto add failed for provider %s", provider.get("id"))
            return 0

    def _auto_add(self, provider_id, streams):
        """Add new streams as disabled channels unless a channel already uses them."""
        if not streams:
            return 0

        def mutate(cfg):
            used = {(r["provider"], r["stream_id"]) for c in cfg["channels"] for r in c["streams"]}
            created = []
            for stream in streams:
                ref = (provider_id, str(stream.get("stream_id")))
                if ref in used:
                    continue
                channel = configmod.new_channel(cfg, channel_fields_from_stream(provider_id, stream))
                channel["enabled"] = False
                channel["number"] = next_free_number(cfg)
                cfg["channels"].append(channel)
                if cfg["lineups"]:
                    cfg["lineups"][0]["channels"].append(channel["id"])
                created.append(channel["id"])
                used.add(ref)
            return created
        created = self.store.update(mutate)
        if created:
            log.info("auto-added %d disabled channels from %s", len(created), provider_id)
        return len(created)

    def refresh_status(self):
        results = {}
        try:
            scheduler = importlib.import_module("app.providers.scheduler")
            getter = getattr(scheduler, "last_results", None) or getattr(scheduler, "status", None)
            if callable(getter):
                for item in _as_list(getter()):
                    if isinstance(item, dict) and item.get("provider"):
                        results[item["provider"]] = item
        except Exception:  # noqa: BLE001 - scheduler is optional for the status page
            pass
        with self.refresh_lock:
            for pid, item in self.refresh_results.items():
                existing = results.get(pid)
                if not existing or (item.get("at") or item.get("started_at") or "") >= (
                        existing.get("at") or existing.get("started_at") or ""):
                    results[pid] = dict(item)
        return results


def _as_list(value):
    if isinstance(value, dict):
        out = []
        for key, item in value.items():
            if isinstance(item, dict):
                item = dict(item)
                item.setdefault("provider", key)
                out.append(item)
        return out
    return list(value or [])


# ---------------------------------------------------------------- config lookups

def find_provider(cfg, provider_id):
    for provider in cfg["providers"]:
        if provider["id"] == provider_id:
            return provider
    raise not_found("unknown provider %s" % provider_id)


def find_channel(cfg, channel_id):
    for channel in cfg["channels"]:
        if channel["id"] == channel_id:
            return channel
    raise not_found("unknown channel %s" % channel_id)


def find_lineup(cfg, lineup_id):
    for lineup in cfg["lineups"]:
        if lineup["id"] == lineup_id:
            return lineup
    raise not_found("unknown lineup %s" % lineup_id)


def fix_url(value):
    """Trim, repair 'http:/host' to 'http://host', drop trailing slashes."""
    text = str(value or "").strip()
    text = re.sub(r"^(https?):/*", lambda m: m.group(1).lower() + "://", text, flags=re.I)
    return text.rstrip("/")


def check_base_url(url):
    """Refuse a Base URL coax cannot reach itself at (catches typos and wrong ports)."""
    if os.environ.get("COAX_SKIP_URL_CHECK") == "1":  # unit tests use fake hosts
        return
    if not re.match(r"^https?://[^\s/]+", url):
        raise bad_request("Base URL must look like http://192.168.1.50:7780")
    try:
        with urllib.request.urlopen(url + "/healthz", timeout=4) as r:
            if r.status == 200 and r.read(16).strip() == b"ok":
                return
    except Exception as exc:  # noqa: BLE001
        raise bad_request("coax can't reach itself at %s (%s). Check the address and port." %
                          (url, getattr(exc, "reason", None) or type(exc).__name__))
    raise bad_request("%s answered, but it is not this coax" % url)


def channel_fields_from_stream(provider_id, stream):
    from app.names import clean_name
    tvg_id = stream.get("tvg_id") or ""
    return {
        "name": clean_name(stream.get("name")) or "Stream %s" % stream.get("stream_id"),
        "group": clean_name(stream.get("group")),
        "logo": stream.get("logo") or "",
        "streams": [{"provider": provider_id, "stream_id": str(stream.get("stream_id"))}],
        "guide": {"mode": "none", "station_id": "", "xmltv_id": tvg_id,
                  "xmltv_provider": provider_id if tvg_id else ""},
    }


def parse_int(value, what, default=None, low=None, high=None):
    if value in (None, ""):
        if default is None:
            raise bad_request("%s is required" % what)
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise bad_request("%s must be a whole number" % what)
    if isinstance(value, bool):
        raise bad_request("%s must be a whole number" % what)
    if low is not None and number < low:
        raise bad_request("%s must be at least %s" % (what, low))
    if high is not None and number > high:
        raise bad_request("%s must be at most %s" % (what, high))
    return number


def stream_refs(value, what="streams"):
    if not isinstance(value, list):
        raise bad_request("%s must be a list of {provider, stream_id}" % what)
    refs = []
    for item in value:
        if not isinstance(item, dict) or not item.get("provider") or item.get("stream_id") in (None, ""):
            raise bad_request("each entry of %s needs provider and stream_id" % what)
        refs.append({"provider": str(item["provider"]), "stream_id": str(item["stream_id"])})
    return refs


def number_value(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def format_number(value):
    return str(int(value)) if float(value).is_integer() else ("%.1f" % value)


# ---------------------------------------------------------------- request handler

class Handler(BaseHTTPRequestHandler):
    server_version = "coax"
    sys_version = ""
    protocol_version = "HTTP/1.0"

    # Routes: (method, regex, handler name). Groups become string arguments.
    ROUTES = [
        ("GET", r"/healthz", "h_healthz"),
        ("GET", r"/output/m3u/([A-Za-z0-9_-]+)\.m3u", "h_output_m3u"),
        ("GET", r"/output/xmltv/([A-Za-z0-9_-]+)\.xml", "h_output_xmltv"),
        ("GET", r"/stream/([A-Za-z0-9-]+)\.ts", "h_stream"),
        ("GET", r"/internal/channels/(\d+)/streams/?", "h_internal_streams"),
        ("GET", r"/internal/channels/(\d+)/?", "h_internal_channel"),
        ("GET", r"/api/config", "h_config_get"),
        ("PUT", r"/api/config", "h_config_put"),
        ("GET", r"/api/providers", "h_providers_list"),
        ("POST", r"/api/providers", "h_providers_create"),
        ("POST", r"/api/providers/test", "h_providers_test_unsaved"),
        ("GET", r"/api/providers/([A-Za-z0-9_-]+)", "h_provider_get"),
        ("PUT", r"/api/providers/([A-Za-z0-9_-]+)", "h_provider_update"),
        ("DELETE", r"/api/providers/([A-Za-z0-9_-]+)", "h_provider_delete"),
        ("POST", r"/api/providers/([A-Za-z0-9_-]+)/test", "h_provider_test"),
        ("POST", r"/api/providers/([A-Za-z0-9_-]+)/refresh", "h_provider_refresh"),
        ("GET", r"/api/providers/([A-Za-z0-9_-]+)/groups", "h_provider_groups"),
        ("PUT", r"/api/providers/([A-Za-z0-9_-]+)/groups", "h_provider_groups_save"),
        ("GET", r"/api/catalog/streams", "h_catalog_streams"),
        ("GET", r"/api/channels", "h_channels_list"),
        ("POST", r"/api/channels", "h_channels_create"),
        ("POST", r"/api/channels/bulk-add", "h_channels_bulk_add"),
        ("POST", r"/api/channels/bulk-update", "h_channels_bulk_update"),
        ("POST", r"/api/channels/bulk-delete", "h_channels_bulk_delete"),
        ("POST", r"/api/channels/reorder", "h_channels_reorder"),
        ("POST", r"/api/channels/auto-number", "h_channels_auto_number"),
        ("GET", r"/api/channels/(\d+)", "h_channel_get"),
        ("PUT", r"/api/channels/(\d+)", "h_channel_update"),
        ("DELETE", r"/api/channels/(\d+)", "h_channel_delete"),
        ("POST", r"/api/channels/(\d+)/streams", "h_channel_attach"),
        ("GET", r"/api/gracenote/search", "h_gracenote_search"),
        ("GET", r"/api/gracenote/suggest", "h_gracenote_suggest"),
        ("POST", r"/api/gracenote/bulk-suggest", "h_gracenote_bulk_suggest"),
        ("GET", r"/api/xmltv/ids", "h_xmltv_ids"),
        ("GET", r"/api/xmltv/sources", "h_xmltv_sources_list"),
        ("POST", r"/api/xmltv/sources", "h_xmltv_sources_create"),
        ("PUT", r"/api/xmltv/sources/([A-Za-z0-9_-]+)", "h_xmltv_source_update"),
        ("DELETE", r"/api/xmltv/sources/([A-Za-z0-9_-]+)", "h_xmltv_source_delete"),
        ("POST", r"/api/automatch", "h_automatch"),
        ("POST", r"/api/automatch/start", "h_automatch_start"),
        ("GET", r"/api/automatch/job", "h_automatch_job"),
        ("POST", r"/api/channels/apply", "h_channels_apply"),
        ("GET", r"/api/lineups", "h_lineups_list"),
        ("POST", r"/api/lineups", "h_lineups_create"),
        ("PUT", r"/api/lineups/([A-Za-z0-9_-]+)", "h_lineup_update"),
        ("DELETE", r"/api/lineups/([A-Za-z0-9_-]+)", "h_lineup_delete"),
        ("GET", r"/api/lineups/([A-Za-z0-9_-]+)/dvr", "h_lineup_dvr_status"),
        ("POST", r"/api/lineups/([A-Za-z0-9_-]+)/dvr", "h_lineup_dvr_add"),
        ("POST", r"/api/lineups/([A-Za-z0-9_-]+)/dvr/sync", "h_lineup_dvr_sync"),
        ("GET", r"/api/status", "h_status"),
        ("GET", r"/api/network", "h_network"),
        ("GET", r"/api/backups", "h_backups"),
        ("POST", r"/api/backups", "h_backup_create"),
        ("POST", r"/api/backups/(config-[0-9A-Za-z.-]+\.json)/restore", "h_backup_restore"),
        ("GET", r"/api/backups/(config-[0-9A-Za-z.-]+\.json)", "h_backup_download"),
        ("DELETE", r"/api/backups/(config-[0-9A-Za-z.-]+\.json)", "h_backup_delete"),
        ("GET", r"/api/log/download", "h_log_download"),
        ("GET", r"/api/log", "h_log"),
    ]
    COMPILED = [(m, re.compile("^" + p + "$"), n) for m, p, n in ROUTES]

    @property
    def app(self):
        return self.server.app

    def log_message(self, fmt, *args):
        log.debug("%s %s", self.address_string(), fmt % args)

    # -- dispatch
    def do_GET(self):
        self._dispatch("GET")

    def do_HEAD(self):
        self._dispatch("HEAD")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def _dispatch(self, method):
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        self.query = {k: v[-1] for k, v in urllib.parse.parse_qs(parsed.query, keep_blank_values=True).items()}
        is_api = path.startswith("/api/")
        try:
            lookup = "GET" if method == "HEAD" else method
            path_matched = False
            for route_method, pattern, name in self.COMPILED:
                match = pattern.match(path)
                if not match:
                    continue
                path_matched = True
                if route_method != lookup:
                    continue
                if path.startswith("/internal/") and self.client_address[0] not in LOCAL_ADDRESSES:
                    raise ApiError(403, "internal API is only available from 127.0.0.1")
                if method == "HEAD" and name == "h_stream":
                    uuid = match.group(1)
                    ok = any(str(c.get("uuid")) == uuid and c.get("enabled", True)
                             for c in self.app.store.get().get("channels") or [])
                    if not ok:
                        raise ApiError(503, "channel unavailable")
                    return self._send_bytes(200, b"", "video/mp2t", head_only=True)
                result = getattr(self, name)(*match.groups())
                if result is not None:
                    self._send_json(200, result)
                return
            if path_matched:
                raise ApiError(405, "method %s not allowed for %s" % (method, path))
            if is_api or path.startswith("/internal/"):
                raise not_found("no such API route: %s %s" % (method, path))
            if method in ("GET", "HEAD"):
                return self._serve_static(path, head_only=(method == "HEAD"))
            raise ApiError(405, "method not allowed")
        except ApiError as exc:
            self._send_error(exc.status, exc.message, is_api)
        except configmod.ConfigError as exc:
            self._send_error(400, str(exc), is_api)
        except DISCONNECT_ERRORS:
            self.close_connection = True
        except Exception as exc:  # noqa: BLE001 - one bad request never takes the server down
            log.exception("error handling %s %s", method, path)
            try:
                self._send_error(500, "internal error: %s" % exc, is_api)
            except DISCONNECT_ERRORS:
                pass

    # -- response helpers
    def _send_bytes(self, status, body, content_type, extra_headers=None, head_only=False):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if not head_only and self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8", {"Cache-Control": "no-store"})

    def _send_error(self, status, message, as_json=True):
        if as_json:
            self._send_json(status, {"error": message})
        else:
            self._send_bytes(status, (message + "\n").encode("utf-8"), "text/plain; charset=utf-8",
                             {"Cache-Control": "no-store"})

    def _body(self):
        length = self.headers.get("Content-Length")
        if not length:
            return {}
        try:
            length = int(length)
        except ValueError:
            raise bad_request("invalid Content-Length")
        if length < 0 or length > MAX_BODY:
            raise ApiError(413, "request body is too large")
        raw = self.rfile.read(length)
        if not raw.strip():
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise bad_request("request body is not valid JSON: %s" % exc)
        return data

    def _body_object(self):
        data = self._body()
        if not isinstance(data, dict):
            raise bad_request("request body must be a JSON object")
        return data

    # -- static files
    def _serve_static(self, path, head_only=False):
        if path in ("", "/"):
            path = "/index.html"
        relative = posixpath.normpath(urllib.parse.unquote(path)).lstrip("/")
        if relative.startswith("..") or "\x00" in relative:
            raise ApiError(404, "not found")
        full = os.path.realpath(os.path.join(WEB_DIR, relative))
        root = os.path.realpath(WEB_DIR)
        if not (full == root or full.startswith(root + os.sep)) or not os.path.isfile(full):
            raise ApiError(404, "not found")
        stat = os.stat(full)
        etag = '"%x-%x"' % (stat.st_mtime_ns, stat.st_size)
        ext = os.path.splitext(full)[1].lower()
        content_type = MIME_TYPES.get(ext, "application/octet-stream")
        if relative == "index.html" or ext == ".html":
            cache = "no-cache"
        elif relative.startswith("vendor/"):
            cache = "public, max-age=604800"
        else:
            cache = "no-cache"   # revalidated with the ETag; cheap and never stale after an upgrade
        headers = {"Cache-Control": cache, "ETag": etag,
                   "Last-Modified": email.utils.formatdate(stat.st_mtime, usegmt=True),
                   "X-Content-Type-Options": "nosniff"}
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            return
        with open(full, "rb") as handle:
            body = handle.read()
        self._send_bytes(200, body, content_type, headers, head_only=head_only)

    # ------------------------------------------------------------ plain routes
    def h_healthz(self):
        self._send_bytes(200, b"ok", "text/plain; charset=utf-8", {"Cache-Control": "no-store"})

    def h_output_m3u(self, lineup_id):
        cfg = self.app.store.get()
        find_lineup(cfg, lineup_id)
        if not (cfg["server"].get("base_url") or "").strip():
            raise ApiError(409, "Set the Base URL in coax Settings first")
        if find_lineup(cfg, lineup_id).get("sort") == "provider":
            cfg = self._with_provider_numbers(cfg)
        text = lazy("app.guide.output").m3u(cfg, lineup_id)
        self._send_bytes(200, text.encode("utf-8"), "audio/x-mpegurl; charset=utf-8", {"Cache-Control": "no-cache"})

    def h_output_xmltv(self, lineup_id):
        cfg = self.app.store.get()
        find_lineup(cfg, lineup_id)
        path = lazy("app.guide.output").xmltv_path(lineup_id)
        if not path or not os.path.isfile(path):
            body = b'<?xml version="1.0" encoding="UTF-8"?>\n<tv generator-info-name="coax"></tv>\n'
            return self._send_bytes(200, body, "application/xml; charset=utf-8", {"Cache-Control": "no-cache"})
        size = os.path.getsize(path)
        self.send_response(200)
        self.send_header("Content-Type", "application/xml; charset=utf-8")
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(path, "rb") as handle:
            while True:
                chunk = handle.read(256 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def h_stream(self, uuid):
        hub = self.app.hub
        if hub is None:
            raise ApiError(503, "stream hub is not running")
        session = hub.open(uuid, self.client_address[0], self.headers.get("User-Agent", ""))
        if session is None:
            raise ApiError(503, "channel unavailable: unknown, disabled, or no free provider line")
        self.close_connection = True
        sent = 0
        try:
            # A DVR that vanishes without closing the socket must not hold the line forever.
            self.connection.settimeout(STREAM_CHUNK_TIMEOUT)
            self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            for opt, val in (("TCP_KEEPIDLE", 30), ("TCP_KEEPINTVL", 10), ("TCP_KEEPCNT", 3)):
                if hasattr(socket, opt):
                    self.connection.setsockopt(socket.IPPROTO_TCP, getattr(socket, opt), val)
        except OSError:
            pass
        try:
            self.send_response(200)
            self.send_header("Content-Type", "video/mp2t")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.flush()
            for chunk in session.iter_bytes():
                if not chunk:
                    continue
                self.wfile.write(chunk)
                self.wfile.flush()
                sent += len(chunk)
        except DISCONNECT_ERRORS:
            pass
        except OSError as exc:
            log.debug("stream client %s went away: %s", self.client_address[0], exc)
        finally:
            try:
                session.close()
            except Exception:  # noqa: BLE001
                log.exception("error closing session for %s", uuid)
            log.debug("stream %s to %s ended after %d bytes", uuid, self.client_address[0], sent)

    def h_internal_streams(self, channel_id):
        channel_id = int(channel_id)
        cfg = self.app.store.get()
        channel = find_channel(cfg, channel_id)
        urls = None
        hub = self.app.hub
        if hub is not None and hasattr(hub, "backup_urls"):
            urls = hub.backup_urls(channel_id)
        if urls is None:
            urls = fallback_stream_urls(cfg, channel)
        return [{"url": u} for u in urls if u]

    def h_internal_channel(self, channel_id):
        channel = find_channel(self.app.store.get(), int(channel_id))
        return {"id": channel["id"], "uuid": channel["uuid"], "name": channel["name"],
                "number": channel["number"]}

    # ------------------------------------------------------------ config
    def h_config_get(self):
        cfg = self.app.store.masked()
        try:
            name, source = lazy("app.tz").effective(self.app.store.get())
            cfg["timezone_effective"] = {"name": name, "source": source, "now": time.strftime("%Y-%m-%d %H:%M %Z")}
        except ApiError:
            pass
        return cfg

    def h_config_put(self):
        body = self._body_object()

        server = body.get("server") if isinstance(body.get("server"), dict) else None
        if isinstance(body.get("channels_dvr"), dict) and "url" in body["channels_dvr"]:
            body["channels_dvr"]["url"] = fix_url(body["channels_dvr"]["url"])
        if server is not None and "base_url" in server:
            server["base_url"] = fix_url(server["base_url"])
            if not server["base_url"]:
                raise bad_request("Base URL is required: how Channels DVR reaches coax, e.g. http://192.168.1.50:7780")
            check_base_url(server["base_url"])
        if "timezone" in body:
            tzname = str(body.get("timezone") or "").strip()
            if tzname and not lazy("app.tz").valid(tzname):
                raise bad_request("unknown time zone %s (use a name like America/New_York)" % tzname)
            body["timezone"] = tzname
        body.pop("timezone_effective", None)
        for src in body.get("xmltv_sources") or []:
            if isinstance(src, dict) and "url" in src:
                src["url"] = fix_url(src["url"])

        def mutate(merged):
            configmod.unmask(body, merged)
            for key, value in body.items():
                if key in ("server", "channels_dvr") and isinstance(value, dict):
                    if not isinstance(merged.get(key), dict):
                        merged[key] = {}
                    merged[key].update(value)
                elif key in ("version", "next_channel_id"):
                    continue
                else:
                    merged[key] = value
        self.app.store.update(mutate)
        log.info("settings saved")
        return self.app.store.masked()

    # ------------------------------------------------------------ providers
    def _provider_view(self, provider):
        view = configmod.mask_provider(provider)
        entry = None
        try:
            entry = self.app.catalogs.get(provider["id"])
        except ApiError:
            entry = None
        view["catalog"] = ({"fetched_at": entry["catalog"].get("fetched_at"), "streams": len(entry["by_id"]),
                            "groups": len(entry["group_counts"])} if entry else None)
        view["refresh"] = self.app.refresh_status().get(provider["id"])
        return view

    def h_providers_list(self):
        return {"providers": [self._provider_view(p) for p in self.app.store.get()["providers"]]}

    def h_provider_get(self, provider_id):
        return self._provider_view(find_provider(self.app.store.get(), provider_id))

    def _normalize_provider(self, body, existing=None):
        if not isinstance(body, dict):
            raise bad_request("provider must be a JSON object")
        provider = copy.deepcopy(existing) if existing else configmod.default_provider()
        for key in ("name", "type", "server", "m3u_url", "xmltv_url", "user_agent", "format", "refresh_hours",
                    "auto_add", "groups", "lines"):
            if key in body:
                provider[key] = body[key]
        for key in ("name", "server", "m3u_url", "xmltv_url", "user_agent"):
            if isinstance(provider.get(key), str):
                provider[key] = provider[key].strip()
        if isinstance(provider.get("server"), str):
            provider["server"] = fix_url(provider["server"])
            for k in ("m3u_url", "xmltv_url"):
                if provider.get(k):
                    provider[k] = fix_url(provider[k])
        for line in provider.get("lines") or []:
            if isinstance(line, dict) and isinstance(line.get("server"), str):
                line["server"] = fix_url(line["server"])
        if provider.get("type") == "m3u" and not provider.get("lines"):
            provider["lines"] = [dict(configmod.default_line(), name="Default", max_streams=0)]
        lines = provider.get("lines")
        if not isinstance(lines, list):
            raise bad_request("lines must be a list")
        old_lines = {l["id"]: l for l in (existing or {}).get("lines", [])}
        used = {l.get("id") for l in lines if isinstance(l, dict) and l.get("id")}
        for index, line in enumerate(lines):
            if not isinstance(line, dict):
                raise bad_request("each line must be an object")
            if not line.get("id"):
                line["id"] = configmod.next_id("l", used)
                used.add(line["id"])
            configmod._fill(line, configmod.default_line())
            if not line.get("name"):
                line["name"] = "Line %d" % (index + 1)
            if line.get("password") == configmod.MASK:
                if line["id"] not in old_lines:
                    raise bad_request("line %s: enter the password" % line["id"])
                line["password"] = old_lines[line["id"]]["password"]
            if isinstance(line.get("username"), str):
                line["username"] = line["username"].strip()
        return provider

    def h_providers_create(self):
        body = self._body_object()

        def mutate(cfg):
            provider = self._normalize_provider(body)
            provider["id"] = configmod.next_id("p", {p["id"] for p in cfg["providers"]})
            cfg["providers"].append(provider)
            return provider
        provider = self.app.store.update(mutate)
        log.info("provider %s added", provider["name"])
        wake_scheduler()
        return self._provider_view(provider)

    def h_provider_update(self, provider_id):
        body = self._body_object()

        def mutate(cfg):
            for index, provider in enumerate(cfg["providers"]):
                if provider["id"] == provider_id:
                    updated = self._normalize_provider(body, provider)
                    updated["id"] = provider_id
                    cfg["providers"][index] = updated
                    return updated
            raise not_found("unknown provider %s" % provider_id)
        provider = self.app.store.update(mutate)
        self.app.catalogs.invalidate(provider_id)
        return self._provider_view(provider)

    def h_provider_delete(self, provider_id):
        force = self.query.get("force") in ("1", "true", "yes")

        def mutate(cfg):
            find_provider(cfg, provider_id)
            users = [c for c in cfg["channels"] if any(r["provider"] == provider_id for r in c["streams"])]
            if users and not force:
                raise ApiError(409, "%d channels use streams from this provider; remove them first or delete "
                               "with ?force=1 to detach those streams" % len(users))
            for channel in users:
                channel["streams"] = [r for r in channel["streams"] if r["provider"] != provider_id]
            for channel in cfg["channels"]:
                if channel["guide"].get("xmltv_provider") == provider_id:
                    channel["guide"]["xmltv_provider"] = ""
            cfg["providers"] = [p for p in cfg["providers"] if p["id"] != provider_id]
            return len(users)
        detached = self.app.store.update(mutate)
        self.app.catalogs.invalidate(provider_id)
        log.info("provider %s deleted", provider_id)
        return {"deleted": provider_id, "channels_detached": detached}

    def h_providers_test_unsaved(self):
        body = self._body_object()
        existing = None
        if body.get("id"):
            try:
                existing = find_provider(self.app.store.get(), body["id"])
            except ApiError:
                existing = None
        provider = self._normalize_provider(body, existing)
        return test_provider(provider)

    def h_provider_test(self, provider_id):
        return test_provider(copy.deepcopy(find_provider(self.app.store.get(), provider_id)))

    def h_provider_refresh(self, provider_id):
        body = self._body() or {}
        wait = self.query.get("wait") in ("1", "true") or (isinstance(body, dict) and body.get("wait") is True)
        return self.app.start_refresh(provider_id, wait=wait)

    def h_provider_groups(self, provider_id):
        provider = find_provider(self.app.store.get(), provider_id)
        entry = self.app.catalogs.get(provider_id)
        selected = set(provider["groups"])
        if not entry:
            groups = [{"name": g, "count": 0, "selected": True} for g in provider["groups"]]
            return {"provider": provider_id, "fetched_at": None, "groups": groups}
        names = list(entry["catalog"].get("groups") or [])
        seen = set(names)
        names += sorted(g for g in entry["group_counts"] if g not in seen)
        names += [g for g in provider["groups"] if g not in seen and g not in entry["group_counts"]]
        groups = [{"name": g, "count": entry["group_counts"].get(g, 0), "selected": g in selected} for g in names]
        return {"provider": provider_id, "fetched_at": entry["catalog"].get("fetched_at"), "groups": groups}

    def h_provider_groups_save(self, provider_id):
        body = self._body_object()
        groups = body.get("groups")
        if not isinstance(groups, list) or not all(isinstance(g, str) for g in groups):
            raise bad_request("groups must be a list of group names")

        def mutate(cfg):
            provider = find_provider(cfg, provider_id)
            provider["groups"] = list(dict.fromkeys(groups))
        self.app.store.update(mutate)
        log.info("provider %s: %d groups selected", provider_id, len(groups))
        return self.h_provider_groups(provider_id)

    # ------------------------------------------------------------ catalog
    def h_catalog_streams(self):
        cfg = self.app.store.get()
        q = self.query
        offset = parse_int(q.get("offset"), "offset", 0, 0)
        limit = parse_int(q.get("limit"), "limit", 100, 1, 1000)
        text = (q.get("q") or "").strip().lower()
        group = q.get("group")
        all_groups = q.get("all_groups") in ("1", "true")
        provider_ids = [q["provider"]] if q.get("provider") else [p["id"] for p in cfg["providers"]]
        used = collections.defaultdict(list)
        for channel in cfg["channels"]:
            for ref in channel["streams"]:
                used[(ref["provider"], ref["stream_id"])].append(channel["id"])
        words = text.split()
        matches = []
        for pid in provider_ids:
            provider = find_provider(cfg, pid)
            entry = self.app.catalogs.get(pid)
            if not entry:
                continue
            selected = set(provider["groups"])
            for stream in entry["catalog"].get("streams") or []:
                stream_group = stream.get("group") or ""
                if group is not None and group != "" and stream_group != group:
                    continue
                if not all_groups and stream_group not in selected:
                    continue
                if words:
                    hay = ("%s %s %s" % (stream.get("name", ""), stream.get("tvg_id", ""),
                                          stream.get("stream_id", ""))).lower()
                    if not all(w in hay for w in words):
                        continue
                matches.append((pid, stream))
        page = matches[offset:offset + limit]
        items = [{
            "provider": pid, "stream_id": str(s.get("stream_id")), "name": s.get("name", ""),
            "group": s.get("group", ""), "logo": s.get("logo", ""), "tvg_id": s.get("tvg_id", ""),
            "channels": used.get((pid, str(s.get("stream_id"))), []),
        } for pid, s in page]
        return {"total": len(matches), "offset": offset, "limit": limit, "items": items}

    # ------------------------------------------------------------ channels
    def _channel_view(self, channel, cfg, lineup_map):
        view = copy.deepcopy(channel)
        for ref in view["streams"]:
            entry = None
            try:
                entry = self.app.catalogs.get(ref["provider"])
            except ApiError:
                entry = None
            stream = entry["by_id"].get(ref["stream_id"]) if entry else None
            ref["missing"] = bool(entry) and stream is None
            ref["name"] = stream.get("name", "") if stream else ""
            ref["group"] = stream.get("group", "") if stream else ""
        view["missing_streams"] = sum(1 for r in view["streams"] if r["missing"])
        view["provider_num"] = self._provider_num(channel)
        view["lineups"] = lineup_map.get(channel["id"], [])
        return view

    def _provider_num(self, channel):
        ref = (channel.get("streams") or [None])[0]
        if not ref:
            return ""
        try:
            entry = self.app.catalogs.get(ref["provider"])
        except ApiError:
            return ""
        stream = entry["by_id"].get(ref["stream_id"]) if entry else None
        return str((stream or {}).get("num") or "")

    def _with_provider_numbers(self, cfg):
        cfg = copy.deepcopy(cfg)
        for ch in cfg["channels"]:
            ch["_pnum"] = self._provider_num(ch)
        return cfg

    def _lineup_map(self, cfg):
        lineup_map = collections.defaultdict(list)
        for lineup in cfg["lineups"]:
            for cid in lineup["channels"]:
                lineup_map[cid].append(lineup["id"])
        return lineup_map

    def h_channels_list(self):
        cfg = self.app.store.get()
        lineup_map = self._lineup_map(cfg)
        return {"channels": [self._channel_view(c, cfg, lineup_map) for c in cfg["channels"]],
                "next_id": cfg["next_channel_id"]}

    def h_channel_get(self, channel_id):
        cfg = self.app.store.get()
        return self._channel_view(find_channel(cfg, int(channel_id)), cfg, self._lineup_map(cfg))

    def _apply_lineups(self, cfg, channel_id, lineups):
        if lineups is None:
            return
        if not isinstance(lineups, list):
            raise bad_request("lineups must be a list of lineup ids")
        wanted = set(lineups)
        known = {l["id"] for l in cfg["lineups"]}
        unknown = wanted - known
        if unknown:
            raise bad_request("unknown lineup %s" % ", ".join(sorted(unknown)))
        for lineup in cfg["lineups"]:
            if lineup["id"] in wanted and channel_id not in lineup["channels"]:
                lineup["channels"].append(channel_id)
            elif lineup["id"] not in wanted and channel_id in lineup["channels"]:
                lineup["channels"].remove(channel_id)

    def _default_lineups(self, cfg, body):
        if "lineups" in body:
            return body["lineups"]
        return [cfg["lineups"][0]["id"]] if cfg["lineups"] else []

    def h_channels_create(self):
        body = self._body_object()
        if "streams" in body:
            body["streams"] = stream_refs(body["streams"])

        def mutate(cfg):
            channel = configmod.new_channel(cfg, body)
            if not channel["number"]:
                channel["number"] = next_free_number(cfg)
            cfg["channels"].append(channel)
            self._apply_lineups(cfg, channel["id"], self._default_lineups(cfg, body))
            return channel
        channel = self.app.store.update(mutate)
        cfg = self.app.store.get()
        return self._channel_view(channel, cfg, self._lineup_map(cfg))

    def h_channel_update(self, channel_id):
        channel_id = int(channel_id)
        body = self._body_object()
        if "streams" in body:
            body["streams"] = stream_refs(body["streams"])

        def mutate(cfg):
            channel = find_channel(cfg, channel_id)
            for key in ("number", "name", "group", "logo", "enabled", "streams"):
                if key in body:
                    channel[key] = body[key]
            if "guide" in body:
                if not isinstance(body["guide"], dict):
                    raise bad_request("guide must be an object")
                channel["guide"].update(body["guide"])
            self._apply_lineups(cfg, channel_id, body.get("lineups"))
            return channel
        channel = self.app.store.update(mutate)
        cfg = self.app.store.get()
        return self._channel_view(channel, cfg, self._lineup_map(cfg))

    def h_channel_delete(self, channel_id):
        return self._delete_channels([int(channel_id)])

    def h_channels_bulk_delete(self):
        ids = self._ids(self._body_object())
        return self._delete_channels(ids)

    def _ids(self, body, key="ids", required=True):
        ids = body.get(key)
        if ids is None and not required:
            return None
        if not isinstance(ids, list) or not all(isinstance(i, int) and not isinstance(i, bool) for i in ids):
            raise bad_request("%s must be a list of channel ids" % key)
        return ids

    def _delete_channels(self, ids):
        def mutate(cfg):
            wanted = set(ids)
            known = {c["id"] for c in cfg["channels"]}
            missing = wanted - known
            if missing:
                raise not_found("unknown channel %s" % ", ".join(str(i) for i in sorted(missing)))
            cfg["channels"] = [c for c in cfg["channels"] if c["id"] not in wanted]
            for lineup in cfg["lineups"]:
                lineup["channels"] = [c for c in lineup["channels"] if c not in wanted]
        self.app.store.update(mutate)
        log.info("deleted channels %s", ", ".join(str(i) for i in ids))
        return {"deleted": ids}

    def h_channels_bulk_add(self):
        body = self._body_object()
        refs = stream_refs(body.get("streams"), "streams")
        if not refs:
            raise bad_request("select at least one stream")
        allow_duplicates = body.get("allow_duplicates") is True
        enabled = body.get("enabled", True)
        if not isinstance(enabled, bool):
            raise bad_request("enabled must be true or false")
        start = body.get("start_number")
        group_override = body.get("group")
        guide_mode = body.get("guide_mode", "none")
        if guide_mode not in configmod.GUIDE_MODES:
            raise bad_request("guide_mode must be one of " + ", ".join(configmod.GUIDE_MODES))
        catalogs = {}
        for ref in refs:
            if ref["provider"] not in catalogs:
                catalogs[ref["provider"]] = self.app.catalogs.get(ref["provider"])

        def mutate(cfg):
            used = {(r["provider"], r["stream_id"]) for c in cfg["channels"] for r in c["streams"]}
            lineups = self._default_lineups(cfg, body)
            number = number_value(start) if start not in (None, "") else None
            if start not in (None, "") and number is None:
                raise bad_request("start_number must be a number")
            created, skipped = [], []
            for ref in refs:
                find_provider(cfg, ref["provider"])
                key = (ref["provider"], ref["stream_id"])
                if key in used and not allow_duplicates:
                    skipped.append(ref)
                    continue
                entry = catalogs.get(ref["provider"])
                stream = entry["by_id"].get(ref["stream_id"]) if entry else None
                if stream is None:
                    stream = {"stream_id": ref["stream_id"], "name": body.get("name") or ""}
                fields = channel_fields_from_stream(ref["provider"], stream)
                fields["enabled"] = enabled
                fields["guide"]["mode"] = guide_mode
                if isinstance(group_override, str) and group_override.strip():
                    fields["group"] = group_override.strip()
                channel = configmod.new_channel(cfg, fields)
                if number is None:
                    channel["number"] = next_free_number(cfg)
                else:
                    channel["number"] = format_number(number)
                    number += 1
                cfg["channels"].append(channel)
                self._apply_lineups(cfg, channel["id"], lineups)
                used.add(key)
                created.append(channel["id"])
            return created, skipped
        created, skipped = self.app.store.update(mutate)
        log.info("added %d channels (%d skipped as already present)", len(created), len(skipped))
        return {"created": created, "skipped": skipped}

    def h_channels_bulk_update(self):
        body = self._body_object()
        ids = self._ids(body)
        changes = body.get("set")
        if not isinstance(changes, dict) or not changes:
            raise bad_request("set must be an object with the fields to change")
        allowed = {"enabled", "group", "logo", "guide"}
        extra = set(changes) - allowed
        if extra:
            raise bad_request("bulk update cannot change %s" % ", ".join(sorted(extra)))

        def mutate(cfg):
            for cid in ids:
                channel = find_channel(cfg, cid)
                for key, value in changes.items():
                    if key == "guide":
                        if not isinstance(value, dict):
                            raise bad_request("guide must be an object")
                        channel["guide"].update(value)
                    else:
                        channel[key] = value
        self.app.store.update(mutate)
        return {"updated": ids}

    def h_channels_reorder(self):
        body = self._body_object()
        ids = self._ids(body)
        if len(set(ids)) != len(ids):
            raise bad_request("ids contains duplicates")

        def mutate(cfg):
            index = {c["id"]: i for i, c in enumerate(cfg["channels"])}
            missing = [i for i in ids if i not in index]
            if missing:
                raise not_found("unknown channel %s" % ", ".join(str(i) for i in missing))
            # The listed channels take the positions they already occupy, in the new order.
            positions = sorted(index[i] for i in ids)
            by_id = {c["id"]: c for c in cfg["channels"]}
            channels = list(cfg["channels"])
            for position, cid in zip(positions, ids):
                channels[position] = by_id[cid]
            cfg["channels"] = channels
            for lineup in cfg["lineups"]:
                order = {c["id"]: i for i, c in enumerate(channels)}
                lineup["channels"].sort(key=lambda cid: order.get(cid, 1 << 30))
            return [c["id"] for c in channels]
        return {"order": self.app.store.update(mutate)}

    def h_channels_auto_number(self):
        body = self._body_object()
        start = number_value(body.get("start", 1))
        if start is None or start < 0:
            raise bad_request("start must be a positive number")
        step = number_value(body.get("step", 1))
        if step is None or step <= 0:
            raise bad_request("step must be a positive number")
        ids = self._ids(body, required=False)
        only_enabled = body.get("only_enabled") is True

        def mutate(cfg):
            targets = cfg["channels"]
            if ids is not None:
                wanted = set(ids)
                for cid in wanted:
                    find_channel(cfg, cid)
                targets = [c for c in targets if c["id"] in wanted]
            if only_enabled:
                targets = [c for c in targets if c["enabled"]]
            number = start
            result = {}
            for channel in targets:
                channel["number"] = format_number(number)
                result[channel["id"]] = channel["number"]
                number += step
            return result
        numbers = self.app.store.update(mutate)
        return {"numbers": {str(k): v for k, v in numbers.items()}}

    def h_channel_attach(self, channel_id):
        channel_id = int(channel_id)
        body = self._body_object()
        refs = stream_refs(body.get("streams"))
        mode = body.get("mode", "append")
        if mode not in ("append", "replace"):
            raise bad_request("mode must be append or replace")

        def mutate(cfg):
            channel = find_channel(cfg, channel_id)
            if mode == "replace":
                channel["streams"] = []
            have = {(r["provider"], r["stream_id"]) for r in channel["streams"]}
            for ref in refs:
                if (ref["provider"], ref["stream_id"]) not in have:
                    channel["streams"].append(ref)
                    have.add((ref["provider"], ref["stream_id"]))
            return channel
        channel = self.app.store.update(mutate)
        cfg = self.app.store.get()
        return self._channel_view(channel, cfg, self._lineup_map(cfg))

    # ------------------------------------------------------------ guide
    def _cdvr_url(self):
        url = (self.app.store.get()["channels_dvr"].get("url") or "").strip().rstrip("/")
        if not url:
            raise bad_request("set the Channels DVR URL in Settings first")
        return url

    def h_gracenote_search(self):
        query = (self.query.get("q") or "").strip()
        if not query:
            raise bad_request("q is required")
        gracenote = lazy("app.guide.gracenote")
        cdvr = self._cdvr_url()
        local, wide, problem = [], [], None
        countries = self._countries(None)
        if countries:
            try:
                index = gracenote.lineup_index(cdvr, countries)
                matcher = gracenote.Matcher(index.get("stations") or {})
                if query.isdigit() and query in matcher.by_id:
                    local = [matcher.by_id[query]]
                local += [s for s in matcher.search(query, limit=40) if s not in local]
            except Exception as exc:  # noqa: BLE001 - local index is a speed-up, not required
                log.warning("gracenote: local lineup search failed: %s", exc)
        if self.query.get("local") != "1" and len(query) >= 2:
            try:
                wide = gracenote.search(cdvr, query)
            except Exception as exc:  # noqa: BLE001
                problem = "Channels DVR search failed: %s" % exc
        seen = {s["station_id"] for s in local}
        results = local + [s for s in wide if s["station_id"] not in seen]
        if not results and problem:
            raise ApiError(502, problem)
        return {"results": results[:60]}

    def _countries(self, value):
        """Parse countries for lineup restriction: "USA:10001,CAN:M5V 3L9" or [["USA", "10001"], ...].
        Without an explicit value, the Gracenote regions from Settings are used."""
        if value in (None, "", []):
            regions = (self.app.store.get().get("gracenote") or {}).get("regions") or []
            pairs = [(str(r.get("country", "")).strip().upper(), str(r.get("postal", "")).strip())
                     for r in regions if isinstance(r, dict)]
            return [p for p in pairs if re.match(r"^[A-Z]{3}$", p[0]) and p[1]] or None
        pairs = []
        items = value.split(",") if isinstance(value, str) else value
        if not isinstance(items, list):
            raise bad_request("countries must be like USA:10001,CAN:M5V3L9")
        for item in items:
            if isinstance(item, str):
                country, _, postal = item.strip().partition(":")
            elif isinstance(item, (list, tuple)) and len(item) == 2:
                country, postal = item
            elif isinstance(item, dict):
                country, postal = item.get("country", ""), item.get("postal", "")
            else:
                raise bad_request("countries must be like USA:10001,CAN:M5V3L9")
            country, postal = str(country).strip().upper(), str(postal).strip()
            if not re.match(r"^[A-Z]{3}$", country) or not postal:
                raise bad_request("each country needs a 3-letter code and a postal code, like USA:10001")
            pairs.append((country, postal))
        return pairs or None

    def _suggest_for(self, gracenote, cdvr, channel=None, name=None, countries=None, wide=False):
        name = name or channel["name"]
        tvg_id = channel["guide"].get("xmltv_id", "") if channel else ""
        return call_flexible(gracenote.suggest, name, cdvr_url=cdvr, tvg_id=tvg_id, countries=countries,
                             wide=wide)

    def h_gracenote_suggest(self):
        cfg = self.app.store.get()
        channel = None
        name = (self.query.get("name") or "").strip()
        if self.query.get("channel"):
            channel = find_channel(cfg, parse_int(self.query["channel"], "channel"))
        if not channel and not name:
            raise bad_request("channel or name is required")
        gracenote = lazy("app.guide.gracenote")
        countries = self._countries(self.query.get("countries"))
        wide = self.query.get("wide") in ("1", "true")
        return {"result": self._suggest_for(gracenote, self._cdvr_url(), channel, name, countries, wide)}

    def h_gracenote_bulk_suggest(self):
        body = self._body_object()
        cfg = self.app.store.get()
        ids = self._ids(body, required=False)
        if ids is None:
            ids = [c["id"] for c in cfg["channels"]
                   if c["guide"]["mode"] in ("gracenote", "both", "none") and not c["guide"].get("station_id")]
        channels = [find_channel(cfg, cid) for cid in ids]
        cdvr = self._cdvr_url()
        countries = self._countries(body.get("countries"))
        gracenote = lazy("app.guide.gracenote")
        results = {}
        bulk = getattr(gracenote, "bulk_suggest", None)
        if callable(bulk):
            inputs = [dict(id=c["id"], name=c["name"], tvg_id=c["guide"].get("xmltv_id", "")) for c in channels]
            raw = call_flexible(bulk, inputs, cdvr_url=cdvr, countries=countries)
            if isinstance(raw, dict):
                results = {str(k): v for k, v in raw.items()}
            else:
                results = {str(c["id"]): r for c, r in zip(channels, raw or [])}
        else:
            for channel in channels:
                try:
                    results[str(channel["id"])] = self._suggest_for(gracenote, cdvr, channel, countries=countries)
                except Exception as exc:  # noqa: BLE001 - one bad lookup does not stop the batch
                    log.warning("gracenote suggestion for %s failed: %s", channel["name"], exc)
                    results[str(channel["id"])] = {"error": str(exc)}
        return {"results": results}

    def h_xmltv_ids(self):
        cfg = self.app.store.get()
        pid = self.query.get("provider") or self.query.get("source")
        if not pid:
            raise bad_request("provider is required")
        if not any(x["id"] == pid for x in cfg["providers"] + cfg["xmltv_sources"]):
            raise not_found("unknown provider or XMLTV source %s" % pid)
        text = (self.query.get("q") or "").strip().lower()
        limit = parse_int(self.query.get("limit"), "limit", 200, 1, 5000)
        items = load_xmltv_ids(self.app.store.data_dir, pid)
        if text:
            items = [i for i in items if text in i["id"].lower() or text in i["name"].lower()]
        return {"provider": pid, "total": len(items), "items": items[:limit]}

    # ------------------------------------------------------------ XMLTV sources
    def _source_fields(self, body, source):
        for key in ("name", "url", "refresh_hours"):
            if key in body:
                value = body[key]
                source[key] = value.strip() if isinstance(value, str) else value
        return source

    def h_xmltv_sources_list(self):
        return {"sources": copy.deepcopy(self.app.store.get()["xmltv_sources"])}

    def h_xmltv_sources_create(self):
        body = self._body_object()

        def mutate(cfg):
            taken = {x["id"] for x in cfg["xmltv_sources"]} | {p["id"] for p in cfg["providers"]}
            source = self._source_fields(body, configmod.default_xmltv_source())
            source["id"] = configmod.next_id("x", taken)
            cfg["xmltv_sources"].append(source)
            return source
        source = self.app.store.update(mutate)
        log.info("XMLTV source %s added", source["name"])
        wake_scheduler()
        return source

    def h_xmltv_source_update(self, source_id):
        body = self._body_object()

        def mutate(cfg):
            for source in cfg["xmltv_sources"]:
                if source["id"] == source_id:
                    return self._source_fields(body, source)
            raise not_found("unknown XMLTV source %s" % source_id)
        return self.app.store.update(mutate)

    def h_xmltv_source_delete(self, source_id):
        def mutate(cfg):
            if not any(x["id"] == source_id for x in cfg["xmltv_sources"]):
                raise not_found("unknown XMLTV source %s" % source_id)
            cfg["xmltv_sources"] = [x for x in cfg["xmltv_sources"] if x["id"] != source_id]
            cleared = 0
            for channel in cfg["channels"]:
                if channel["guide"].get("xmltv_provider") == source_id:
                    channel["guide"]["xmltv_provider"] = ""
                    channel["guide"]["xmltv_id"] = ""
                    cleared += 1
            return cleared
        cleared = self.app.store.update(mutate)
        return {"deleted": source_id, "channels_cleared": cleared}

    # ------------------------------------------------------------ one-screen setup
    def h_automatch_start(self):
        """Start Auto-match in the background so leaving the page does not stop it."""
        body = self._body_object()
        ids = self._ids(body)
        app = self.app
        with app.automatch_lock:
            job = app.automatch_job
            if job and job["state"] == "running":
                return self._job_view(job)
            job = {"id": "%x" % int(time.time() * 1000), "state": "running", "done": 0, "total": len(ids) * 2,
                   "phase": "guide", "ids": ids, "started": time.time(), "finished": None, "result": None,
                   "error": None}
            app.automatch_job = job
        log.info("auto-match started for %d channels", len(ids))

        def run():
            try:
                job["result"] = self._automatch_compute(body, ids, job)
                job["state"] = "done"
                log.info("auto-match finished in %.0fs", time.time() - job["started"])
            except Exception as exc:  # noqa: BLE001
                job["state"], job["error"] = "error", (exc.message if isinstance(exc, ApiError) else str(exc))
                log.warning("auto-match failed: %s", job["error"])
            finally:
                job["finished"] = time.time()
                job["done"] = job["total"]
        threading.Thread(target=run, name="coax-automatch", daemon=True).start()
        return self._job_view(job)

    def h_automatch_job(self):
        job = self.app.automatch_job
        if not job:
            return {"state": "none"}
        view = self._job_view(job)
        if job["state"] == "done" and self.query.get("result") == "1":
            view["result"] = job["result"]
        return view

    @staticmethod
    def _job_view(job):
        return {k: job[k] for k in ("id", "state", "done", "total", "phase", "ids", "started", "finished", "error")}

    def h_automatch(self):
        body = self._body_object()
        return self._automatch_compute(body, self._ids(body), None)

    def _automatch_compute(self, body, ids, job):
        cfg = self.app.store.get()
        channels = [find_channel(cfg, cid) for cid in ids]
        out = {str(c["id"]): {"guide": [], "backups": []} for c in channels}

        # Gracenote suggestions (one batch; Channels DVR URL optional so backups still work without it).
        cdvr = (cfg["channels_dvr"].get("url") or "").strip().rstrip("/")
        countries = self._countries(body.get("countries"))
        try:
            gracenote = lazy("app.guide.gracenote")
            inputs = [dict(id=c["id"], name=c["name"], tvg_id=c["guide"].get("xmltv_id", "")) for c in channels]
            tick = (lambda n: job.__setitem__("done", n)) if job else None
            raw = call_flexible(gracenote.bulk_suggest, inputs, cdvr_url=cdvr, countries=countries,
                                progress=tick) or []
            if isinstance(raw, dict):
                pairs = [(str(k), v) for k, v in raw.items()]
            else:
                pairs = [(str(c["id"]), r) for c, r in zip(channels, raw)]
            for cid, result in pairs:
                if cid not in out or not isinstance(result, dict):
                    continue
                out[cid].update(guide=result.get("candidates") or [], guide_status=result.get("status"),
                                guide_reason=result.get("reason", ""), guide_best=result.get("best"))
        except Exception as exc:  # noqa: BLE001 - guide problems never block backup matching
            message = exc.message if isinstance(exc, ApiError) else str(exc)
            log.warning("automatch: gracenote suggestions failed: %s", message)
            for item in out.values():
                item["guide_error"] = message

        # Backup stream suggestions across every provider's catalog.
        try:
            backups = importlib.import_module("app.backups")
            suggest = getattr(backups, "suggest", None)
        except ImportError:
            suggest = None
        if callable(suggest):
            catalogs = {}
            for provider in cfg["providers"]:
                try:
                    entry = self.app.catalogs.get(provider["id"])
                except ApiError:
                    entry = None
                if entry:
                    selected = set(provider.get("groups") or [])
                    catalog = entry["catalog"]
                    if selected:
                        # Only streams from the groups picked for this provider can become backups.
                        catalog = dict(catalog, streams=[s for s in catalog.get("streams") or []
                                                         if (s.get("group") or "") in selected])
                    catalogs[provider["id"]] = catalog
            if job:
                job["phase"], job["done"] = "backups", len(channels)
            for n, channel in enumerate(channels, 1):
                if job:
                    job["done"] = len(channels) + n
                try:
                    out[str(channel["id"])]["backups"] = list(
                        suggest(copy.deepcopy(channel), catalogs) or [])
                except Exception as exc:  # noqa: BLE001 - one channel never stops the batch
                    log.warning("automatch: backup suggestions for %s failed: %s", channel["name"], exc)
                    out[str(channel["id"])]["backups_error"] = str(exc)
        return out

    def h_channels_apply(self):
        body = self._body_object()
        entries = body.get("channels")
        if not isinstance(entries, list) or not entries:
            raise bad_request("channels must be a non-empty list of {id, guide?, streams?}")
        changes = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise bad_request("each entry of channels must be an object")
            cid = entry.get("id")
            if not isinstance(cid, int) or isinstance(cid, bool):
                raise bad_request("each entry of channels needs an integer id")
            guide = entry.get("guide")
            if guide is not None:
                if not isinstance(guide, dict):
                    raise bad_request("channel %s: guide must be an object" % cid)
                # Keep the fields coax uses; ignore any others instead of failing the whole save.
                allowed = {"mode", "station_id", "xmltv_id", "xmltv_provider", "call_sign", "station_name",
                           "confidence", "reviewed"}
                guide = {k: v for k, v in guide.items() if k in allowed}
                entry["guide"] = guide
            streams = stream_refs(entry["streams"], "channel %s streams" % cid) if "streams" in entry else None
            if streams is not None and len({(r["provider"], r["stream_id"]) for r in streams}) != len(streams):
                raise bad_request("channel %s: a stream is listed twice" % cid)
            changes.append((cid, guide, streams))
        if len({c[0] for c in changes}) != len(changes):
            raise bad_request("a channel is listed twice")

        def mutate(cfg):
            for cid, guide, streams in changes:
                channel = find_channel(cfg, cid)
                if guide is not None:
                    channel["guide"].update(guide)
                if streams is not None:
                    channel["streams"] = streams
            return [c[0] for c in changes]
        updated = self.app.store.update(mutate)   # validated and saved in one atomic write
        log.info("applied guide and backup choices to %d channels", len(updated))
        cfg = self.app.store.get()
        lineup_map = self._lineup_map(cfg)
        return {"updated": updated,
                "channels": [self._channel_view(find_channel(cfg, cid), cfg, lineup_map) for cid in updated]}

    # ------------------------------------------------------------ lineups
    def h_lineups_list(self):
        return {"lineups": copy.deepcopy(self.app.store.get()["lineups"])}

    def h_lineups_create(self):
        body = self._body_object()
        name = body.get("name")
        if not isinstance(name, str) or not name.strip():
            raise bad_request("name is required")
        lineup_id = body.get("id") or re.sub(r"[^a-z0-9_-]+", "-", name.strip().lower()).strip("-") or "lineup"

        def mutate(cfg):
            ids = {l["id"] for l in cfg["lineups"]}
            final = lineup_id
            if body.get("id"):
                if final in ids:
                    raise ApiError(409, "lineup %s already exists" % final)
            else:
                n = 2
                while final in ids:
                    final = "%s-%d" % (lineup_id, n)
                    n += 1
            if body.get("all_channels") is True:
                members = [c["id"] for c in cfg["channels"]]
            else:
                members = body.get("channels", [])
            lineup = {"id": final, "name": name.strip(), "guide": body.get("guide") or "per-channel",
                      "sort": body.get("sort") if body.get("sort") in configmod.LINEUP_SORTS else "manual",
                      "start_number": int(body["start_number"]) if str(body.get("start_number") or "").strip().isdigit() else None,
                      "channels": members}
            cfg["lineups"].append(lineup)
            return lineup
        return self.app.store.update(mutate)

    def h_lineup_update(self, lineup_id):
        body = self._body_object()

        def mutate(cfg):
            lineup = find_lineup(cfg, lineup_id)
            if "name" in body:
                if not isinstance(body["name"], str) or not body["name"].strip():
                    raise bad_request("name is required")
                lineup["name"] = body["name"].strip()
            if "channels" in body:
                lineup["channels"] = body["channels"]
            if "guide" in body:
                lineup["guide"] = body["guide"]
            if "sort" in body:
                if body["sort"] not in configmod.LINEUP_SORTS:
                    raise bad_request("sort must be one of " + ", ".join(configmod.LINEUP_SORTS))
                lineup["sort"] = body["sort"]
            if "start_number" in body:
                sn = body["start_number"]
                if sn in (None, ""):
                    lineup["start_number"] = None
                else:
                    try:
                        lineup["start_number"] = int(str(sn).strip())
                    except ValueError:
                        raise bad_request("start number must be a whole number")
            return lineup
        return self.app.store.update(mutate)

    def h_lineup_delete(self, lineup_id):
        def mutate(cfg):
            find_lineup(cfg, lineup_id)
            cfg["lineups"] = [l for l in cfg["lineups"] if l["id"] != lineup_id]
        self.app.store.update(mutate)
        return {"deleted": lineup_id}

    # ------------------------------------------------------------ Channels DVR source per lineup
    def _dvr(self):
        """(module, syncer). The syncer is made on first use when main did not create one (tests)."""
        module = lazy("app.channels_dvr")
        if self.app.dvr is None:
            self.app.dvr = module.Syncer(self.app.store, start=False)
        return module, self.app.dvr

    def _dvr_call(self, func, *args):
        module = lazy("app.channels_dvr")
        try:
            return func(*args)
        except module.DvrError as exc:
            raise ApiError(exc.status, exc.message)

    def h_lineup_dvr_status(self, lineup_id):
        module, syncer = self._dvr()
        cfg = self.app.store.get()
        lineup = find_lineup(cfg, lineup_id)
        out = {"key": module.source_key(lineup_id), "dvr": copy.deepcopy(lineup.get("dvr")),
               "last_sync": syncer.last_result(lineup_id),
               "defaults": self._dvr_call(module.defaults, cfg, lineup_id),
               "dvr_url_set": bool((cfg["channels_dvr"].get("url") or "").strip()),
               "base_url_set": bool((cfg["server"].get("base_url") or "").strip()),
               "in_dvr": False, "source": None, "error": ""}
        if out["dvr_url_set"]:
            out.update(self._dvr_call(module.status, cfg, lineup_id))
        return out

    def h_lineup_dvr_add(self, lineup_id):
        body = self._body_object()
        _module, syncer = self._dvr()
        find_lineup(self.app.store.get(), lineup_id)
        return self._dvr_call(syncer.add, lineup_id, body)

    def h_lineup_dvr_sync(self, lineup_id):
        module, syncer = self._dvr()
        cfg = self.app.store.get()
        find_lineup(cfg, lineup_id)
        self._dvr_call(module.dvr_url, cfg)
        result = syncer.sync_now(lineup_id)
        if result and not result.get("ok"):
            raise ApiError(502, result.get("error") or "Channels DVR sync failed")
        return result

    # ------------------------------------------------------------ config backups
    def h_backups(self):
        snapshots = lazy("app.snapshots")
        return {"settings": snapshots.settings(self.app.store.get()), "backups": snapshots.list_backups(self.app.store),
                "now": time.strftime("%Y-%m-%d %H:%M"), "timezone": time.strftime("%Z")}

    def h_backup_create(self):
        return lazy("app.snapshots").create(self.app.store, "manual")

    def h_backup_restore(self, name):
        try:
            return lazy("app.snapshots").restore(self.app.store, name)
        except FileNotFoundError:
            raise not_found("no backup named %s" % name)
        except (ValueError, configmod.ConfigError) as exc:
            raise bad_request("cannot restore %s: %s" % (name, exc))

    def h_backup_download(self, name):
        try:
            data = lazy("app.snapshots").read(self.app.store, name)
        except (FileNotFoundError, ValueError):
            raise not_found("no backup named %s" % name)
        self._send_bytes(200, data, "application/json", {"Content-Disposition": 'attachment; filename="%s"' % name,
                                                         "Cache-Control": "no-store"})

    def h_backup_delete(self, name):
        try:
            lazy("app.snapshots").delete(self.app.store, name)
        except (FileNotFoundError, ValueError):
            raise not_found("no backup named %s" % name)
        return {"deleted": name}

    # ------------------------------------------------------------ network
    _net = {"at": 0.0, "data": None}
    _net_lock = threading.Lock()

    def h_network(self):
        fresh = self.query.get("refresh") in ("1", "true")
        with Handler._net_lock:
            cached = Handler._net["data"]
            if cached and not fresh and time.time() - Handler._net["at"] < 600:
                return cached
        data = {"ip": None, "country": None, "checked_at": None, "error": None}
        try:
            import requests
            r = requests.get("https://ipinfo.io/json", timeout=8,
                             headers={"User-Agent": "coax", "Accept": "application/json"})
            r.raise_for_status()
            info = r.json()
            data["ip"] = info.get("ip")
            data["country"] = info.get("country")
        except Exception as exc:  # noqa: BLE001 - shown in the UI, never fatal
            data["error"] = "could not look up the public IP (%s)" % type(exc).__name__
        data["checked_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with Handler._net_lock:
            Handler._net.update(at=time.time(), data=data)
        return data

    # ------------------------------------------------------------ status and log
    def h_status(self):
        cfg = self.app.store.get()
        sessions = []
        hub = self.app.hub
        if hub is not None:
            try:
                sessions = hub.status() or []
            except Exception as exc:  # noqa: BLE001
                log.warning("hub status failed: %s", exc)
        return {
            "uptime": round(time.time() - self.app.started, 1),
            "hub_running": hub is not None,
            "sessions": sessions,
            "lines": line_usage(cfg, hub, sessions),
            "refresh": self.app.refresh_status(),
            "channels": {"total": len(cfg["channels"]), "enabled": sum(1 for c in cfg["channels"] if c["enabled"])},
        }

    def h_log(self):
        since = parse_int(self.query.get("since"), "since", 0, 0)
        limit = parse_int(self.query.get("limit"), "limit", 500, 1, 10000)
        return self.app.log_buffer.tail(since, limit)

    def h_log_download(self):
        lines = self.app.log_buffer.tail(0, 0)["lines"]
        text = "".join("%s %-7s %s: %s\n" % (l["ts"], l["level"], l["name"], l["msg"]) for l in lines)
        name = "coax-log-%s.txt" % time.strftime("%Y%m%d-%H%M%S")
        self._send_bytes(200, text.encode("utf-8"), "text/plain; charset=utf-8",
                         {"Content-Disposition": 'attachment; filename="%s"' % name, "Cache-Control": "no-store"})


# ---------------------------------------------------------------- helpers outside the handler

def next_free_number(cfg):
    numbers = [number_value(c["number"]) for c in cfg["channels"] if c.get("number")]
    numbers = [n for n in numbers if n is not None]
    return str(int(max(numbers)) + 1) if numbers else "1"


def fallback_stream_urls(cfg, channel):
    """Stream URLs built with each provider's first line, used when no hub session exists."""
    providers = {p["id"]: p for p in cfg["providers"]}
    try:
        build = getattr(lazy("app.providers"), "stream_url", None) or lazy("app.providers.catalog").stream_url
    except (ApiError, AttributeError):
        return []
    urls = []
    for ref in channel["streams"]:
        provider = providers.get(ref["provider"])
        if not provider or not provider["lines"]:
            continue
        try:
            urls.append(build(provider, provider["lines"][0], ref["stream_id"]))
        except Exception as exc:  # noqa: BLE001
            log.warning("could not build URL for %s/%s: %s", ref["provider"], ref["stream_id"], exc)
    return urls


def line_usage(cfg, hub, sessions):
    """Every line with used/max slots and which channels hold them.

    Shape: [{"provider", "provider_name", "line", "line_name", "used", "max", "channels": [{"id", "name"}]}].
    """
    held = collections.defaultdict(list)
    from_hub = False
    if hub is not None and callable(getattr(hub, "line_status", None)):
        try:
            for item in hub.line_status() or []:
                key = (item.get("provider_id"), item.get("line_id"))
                held[key] = [c if isinstance(c, dict) else {"id": c, "name": None}
                             for c in item.get("channels") or []]
            from_hub = True
        except Exception as exc:  # noqa: BLE001
            log.warning("hub line status failed: %s", exc)
    if not from_hub:
        for session in sessions or []:
            if isinstance(session, dict):
                key = (session.get("provider_id") or session.get("provider"),
                       session.get("line_id") or session.get("line"))
                held[key].append({"id": session.get("channel_id"),
                                  "name": session.get("channel_name") or session.get("channel")})
    out = []
    for provider in cfg["providers"]:
        for line in provider["lines"]:
            holders = held.get((provider["id"], line["id"]), [])
            out.append({"provider": provider["id"], "provider_name": provider["name"], "line": line["id"],
                        "line_name": line["name"], "max": line["max_streams"], "used": len(holders),
                        "channels": holders})
    return out


def wake_scheduler():
    try:
        scheduler = importlib.import_module("app.providers.scheduler")
        if callable(getattr(scheduler, "wake", None)):
            scheduler.wake()
    except Exception:  # noqa: BLE001 - the scheduler is optional
        pass


def load_xmltv_ids(data_dir, provider_id):
    """Normalize the cached XMLTV channel list to [{"id", "name"}]."""
    raw = None
    try:
        xmltv = importlib.import_module("app.guide.xmltv")
        for name in ("load_ids", "channel_ids", "ids"):
            func = getattr(xmltv, name, None)
            if callable(func):
                raw = func(provider_id)
                break
    except Exception as exc:  # noqa: BLE001 - fall back to the cache file
        log.debug("xmltv id helper unavailable: %s", exc)
    if raw is None:
        path = os.path.join(data_dir, "cache", "xmltv-ids-%s.json" % re.sub(r"[^A-Za-z0-9_-]", "_", provider_id))
        try:
            with open(path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, ValueError):
            raw = []
    if isinstance(raw, dict):
        raw = raw.get("channels", raw.get("ids", raw))
    items = []
    if isinstance(raw, dict):
        for key, value in raw.items():
            name = value if isinstance(value, str) else (value or {}).get("name", "") if isinstance(value, dict) \
                else ", ".join(value) if isinstance(value, list) else ""
            items.append({"id": str(key), "name": name})
    else:
        for value in raw or []:
            if isinstance(value, dict) and value.get("id") is not None:
                name = value.get("name") or value.get("display_name") or value.get("names") or ""
                if isinstance(name, list):
                    name = name[0] if name else ""
                item = {"id": str(value["id"]), "name": str(name)}
                if value.get("icon"):
                    item["icon"] = value["icon"]
                items.append(item)
            elif isinstance(value, (list, tuple)) and value:
                items.append({"id": str(value[0]), "name": str(value[1]) if len(value) > 1 else ""})
            elif isinstance(value, str):
                items.append({"id": value, "name": ""})
    return items


def test_provider(provider):
    """Check that a provider answers. Uses the providers module when it offers a test, else a direct probe."""
    for module_name, func_name in (("app.providers", "test_connection"),
                                   ("app.providers.%s" % provider.get("type"), "test_connection"),
                                   ("app.providers.xtream", "account_info")):
        if func_name == "account_info" and provider.get("type") != "xtream":
            continue
        try:
            module = importlib.import_module(module_name)
        except Exception:  # noqa: BLE001
            continue
        func = getattr(module, func_name, None)
        if not callable(func):
            continue
        try:
            result = func(provider)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": str(exc)}
        if isinstance(result, dict):
            result = dict(result)
            result.setdefault("ok", True)
            result.setdefault("message", result.get("error") or ("connected" if result["ok"] else "failed"))
            return result
        return {"ok": bool(result), "message": "connected" if result else "no answer", "info": result}
    return probe_provider(provider)


def probe_provider(provider, timeout=15):
    headers = {"User-Agent": __import__("app").USER_AGENT}
    try:
        if provider.get("type") == "xtream":
            if not provider.get("lines"):
                return {"ok": False, "message": "add a line with a username and password"}
            line = provider["lines"][0]
            url = "%s/player_api.php?%s" % (provider["server"].rstrip("/"), urllib.parse.urlencode(
                {"username": line["username"], "password": line["password"]}))
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as response:
                data = json.loads(response.read(2 * 1024 * 1024).decode("utf-8", "replace") or "{}")
            info = data.get("user_info") if isinstance(data, dict) else None
            if not isinstance(info, dict) or str(info.get("auth", 1)) == "0":
                return {"ok": False, "message": "the provider rejected the login"}
            return {"ok": True, "message": "connected (%s)" % info.get("status", "ok"),
                    "info": {k: info.get(k) for k in ("status", "exp_date", "max_connections", "active_cons",
                                                      "is_trial")}}
        with urllib.request.urlopen(urllib.request.Request(provider["m3u_url"], headers=headers),
                                    timeout=timeout) as response:
            head = response.read(65536).decode("utf-8", "replace")
        if "#EXTM3U" in head or "#EXTINF" in head:
            return {"ok": True, "message": "playlist found"}
        return {"ok": False, "message": "the URL did not return an M3U playlist"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": redact(str(exc), provider)}


def redact(text, provider):
    for line in provider.get("lines") or []:
        if line.get("password"):
            text = text.replace(line["password"], configmod.MASK)
    return text


# ---------------------------------------------------------------- server

class CoaxHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64

    def __init__(self, address, app):
        self.app = app
        super().__init__(address, Handler)

    def server_bind(self):
        # HTTPServer.server_bind calls socket.getfqdn(), a reverse DNS lookup that can stall for many seconds
        # on a LAN without reverse DNS. The name is unused, so skip it.
        import socketserver
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = port

    def handle_error(self, request, client_address):
        import sys
        exc = sys.exc_info()[1]
        if isinstance(exc, DISCONNECT_ERRORS):
            return
        log.exception("unhandled error from %s", client_address[0] if client_address else "?")


def make_server(app, host="", port=7780):
    return CoaxHTTPServer((host, port), app)
