"""Catalog cache: download a provider's live streams and keep the last good copy on disk.

Cache file: {data_dir}/cache/catalog-<provider_id>.json
  {"fetched_at": iso, "groups": [...],
   "streams": [{"stream_id", "name", "group", "logo", "tvg_id", "url"?, "format"?}]}
"""

import datetime
import json
import logging
import os
import re
import tempfile
import threading

from . import m3u, xtream
from .net import ProviderError

log = logging.getLogger("coax.providers")

# Default data directory. app.config.DATA_DIR wins when it exists, unless a test overrides this.
DEFAULT_DATA_DIR = "/data"
DATA_DIR = DEFAULT_DATA_DIR

_SAFE_ID = re.compile(r"[^A-Za-z0-9_.-]")

# One lock per provider so a scheduled refresh and a "refresh now" never overlap.
_locks = {}
_locks_guard = threading.Lock()

# Parsed cache files keyed by provider id: ((path, mtime_ns, size), catalog, index by stream_id).
_memo = {}
_memo_guard = threading.Lock()


def data_dir():
    if DATA_DIR != DEFAULT_DATA_DIR:
        return DATA_DIR
    try:
        from app import config  # owned by web; may not exist yet
    except ImportError:
        return DATA_DIR
    return getattr(config, "DATA_DIR", None) or DATA_DIR


def cache_path(provider_id):
    safe = _SAFE_ID.sub("_", str(provider_id)) or "_"
    return os.path.join(data_dir(), "cache", "catalog-%s.json" % safe)


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()


def parse_iso(value):
    """Parse fetched_at; returns an aware datetime or None."""
    try:
        parsed = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed


def atomic_write_json(path, data):
    """Write JSON to a temp file in the same directory, fsync, then rename over the target."""
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:
        dir_fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


def _lock_for(provider_id):
    with _locks_guard:
        return _locks.setdefault(str(provider_id), threading.Lock())


def fetch(provider):
    """Download the catalog without touching the cache."""
    kind = (provider.get("type") or "xtream").lower()
    if kind == "xtream":
        return xtream.fetch_catalog(provider)
    if kind == "m3u":
        return m3u.fetch_catalog(provider)
    raise ProviderError("unknown provider type %r" % kind)


def refresh(provider):
    """Download, cache atomically, and return the catalog plus a change report.

    Returned dict is the cached catalog plus:
      "added" / "removed": stream ids new or gone versus the previous cache (all groups),
      "added_selected" / "removed_selected": the same limited to the provider's selected groups,
      "updated": count of kept streams whose name, logo, group, tvg_id or url changed.
    On failure this raises and the old cache file is left untouched.
    """
    provider_id = provider.get("id")
    if not provider_id:
        raise ProviderError("provider has no id")
    with _lock_for(provider_id):
        fresh = fetch(provider)
        previous = load(provider_id)
        if not fresh.get("streams") and (previous or {}).get("streams"):
            # A panel hiccup can answer an empty list; never trade a good catalog for it.
            raise ProviderError("provider returned no streams; keeping the previous list")
        catalog = {"fetched_at": now_iso(), "groups": fresh["groups"], "streams": fresh["streams"]}
        atomic_write_json(cache_path(provider_id), catalog)

    report = diff(previous, catalog, provider.get("groups") or [])
    log.info("provider %s refreshed: %d streams in %d groups, %d added, %d removed, %d updated",
             provider_id, len(catalog["streams"]), len(catalog["groups"]),
             len(report["added"]), len(report["removed"]), report["updated"])
    result = dict(catalog)
    result.update(report)
    return result


def diff(previous, current, selected_groups=()):
    """Compare two catalogs. A missing previous catalog counts every stream as added."""
    old = {s["stream_id"]: s for s in (previous or {}).get("streams") or []}
    new = {s["stream_id"]: s for s in current.get("streams") or []}
    selected = set(selected_groups or ())
    # Streams whose id changed but whose URL did not (for example the switch from URL-hash ids
    # to stable M3U ids) are the same stream, not one removal plus one addition.
    new_by_url = {s["url"]: sid for sid, s in new.items() if s.get("url")}
    renamed = {}
    for sid, s in old.items():
        if sid not in new and s.get("url") in new_by_url and new_by_url[s["url"]] not in old:
            renamed[sid] = new_by_url[s["url"]]
    renamed_to = set(renamed.values())
    added = [sid for sid in new if sid not in old and sid not in renamed_to]
    removed = [sid for sid in old if sid not in new and sid not in renamed]
    fields = ("name", "logo", "group", "tvg_id", "url")
    updated = sum(1 for sid, s in new.items()
                  if sid in old and any(old[sid].get(f) != s.get(f) for f in fields))
    return {
        "added": added,
        "removed": removed,
        "added_selected": [sid for sid in added if new[sid].get("group") in selected],
        "removed_selected": [sid for sid in removed if old[sid].get("group") in selected],
        "updated": updated,
        "renamed": renamed,  # old stream id -> new stream id, for rewriting channel refs
    }


def load(provider_id):
    """Return the cached catalog, or None when missing or unreadable.

    The dict is shared between callers (parsed once per file change); treat it as read-only.
    """
    entry = _load_memo(provider_id)
    return entry[1] if entry else None


def _load_memo(provider_id):
    """Read the cache file, reusing the parsed copy while the file is unchanged."""
    path = cache_path(provider_id)
    try:
        stat = os.stat(path)
    except OSError:
        return None
    key = str(provider_id)
    with _memo_guard:
        entry = _memo.get(key)
        if entry and entry[0] == (path, stat.st_mtime_ns, stat.st_size):
            return entry
    try:
        with open(path, "r", encoding="utf-8") as handle:
            catalog = json.load(handle)
    except (OSError, ValueError) as exc:
        log.warning("catalog cache for provider %s unreadable: %s", provider_id, exc)
        return None
    if not isinstance(catalog, dict) or not isinstance(catalog.get("streams"), list):
        log.warning("catalog cache for provider %s has an unexpected shape", provider_id)
        return None
    catalog.setdefault("groups", [])
    index = {s.get("stream_id"): s for s in catalog["streams"] if isinstance(s, dict)}
    # Channel refs saved before stable M3U ids used sha1(url); keep them resolvable.
    for stream in catalog["streams"]:
        if isinstance(stream, dict) and stream.get("url"):
            index.setdefault(m3u.legacy_stream_id(stream["url"]), stream)
    entry = ((path, stat.st_mtime_ns, stat.st_size), catalog, index)
    with _memo_guard:
        _memo[key] = entry
    return entry


def find_stream(provider_id, stream_id):
    """Return the cached stream dict or None."""
    entry = _load_memo(provider_id)
    if not entry:
        return None
    return entry[2].get(str(stream_id))


def age_hours(catalog):
    """Hours since the catalog was fetched, or None when unknown."""
    fetched = parse_iso((catalog or {}).get("fetched_at"))
    if not fetched:
        return None
    delta = datetime.datetime.now(datetime.timezone.utc) - fetched
    return delta.total_seconds() / 3600.0


def group_counts(catalog):
    """[{"name": group, "count": n}] in catalog group order, for the group picker."""
    counts = {}
    for stream in (catalog or {}).get("streams") or []:
        counts[stream.get("group")] = counts.get(stream.get("group"), 0) + 1
    names = list((catalog or {}).get("groups") or [])
    names += [g for g in counts if g not in names]
    return [{"name": g, "count": counts.get(g, 0)} for g in names]


def selected_streams(provider, catalog=None):
    """Streams in the provider's selected groups (what the Channels tab offers)."""
    catalog = catalog if catalog is not None else load(provider.get("id"))
    selected = set(provider.get("groups") or [])
    return [s for s in (catalog or {}).get("streams") or [] if s.get("group") in selected]
