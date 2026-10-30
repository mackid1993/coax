"""Xtream Codes client: account info, live categories and live streams, and the VOD lists (movies, series).

Providers are inconsistent: numbers arrive as strings, fields are null, lists come back as
empty objects, and errors are sometimes HTML pages with status 200. Everything here is
normalized to plain strings before it reaches the catalog.
"""

import json
import logging
import time
from datetime import datetime, timezone
from urllib.parse import quote, urlencode

import requests
import urllib3

from .net import ProviderError, get, read_body, redact, secrets_of

log = logging.getLogger("coax.providers")

UNCATEGORIZED = "Uncategorized"
# Overall wall-clock limit per player_api.php call, on top of the per-read timeout.
API_DEADLINE = 120
EXPIRY_DEADLINE = 10   # the plan expiry read at a catalog refresh: a slow answer must not hold the refresh (the
                       # last read can run past it by up to its own timeout, so about 20 s at worst)


def base_url(server):
    """Normalize a server address: add a scheme, drop trailing slashes and a pasted player_api.php."""
    server = (server or "").strip()
    if not server:
        raise ProviderError("Xtream server address is empty")
    if "://" not in server:
        server = "http://" + server
    server = server.split("?", 1)[0].rstrip("/")
    for suffix in ("/player_api.php", "/get.php"):
        if server.endswith(suffix):
            server = server[: -len(suffix)].rstrip("/")
    return server


# Set by main to the hub: provider id -> ids of its lines that are streaming now. Each line allows one connection,
# so catalog and guide calls use a line nobody is watching when there is one.
BUSY_LINES = [None]


def first_line(provider):
    """The line whose credentials catalog and guide API calls use: an idle enabled line (the last one, as the hub
    tunes from the first), else the first enabled line."""
    lines = [line for line in provider.get("lines") or []
             if isinstance(line, dict) and line.get("enabled", True) is not False]
    if not lines:
        raise ProviderError("provider %s has no lines configured" % provider.get("id", "?"))
    busy_of = BUSY_LINES[0]
    if busy_of is not None:
        try:
            busy = busy_of(provider.get("id"))
        except Exception:  # noqa: BLE001 - never stop a catalog refresh over this
            busy = set()
        idle = [line for line in lines if line.get("id") not in busy]
        if idle:
            return idle[-1]
    return lines[0]


def text(value):
    """Return a clean string for any JSON scalar; None becomes an empty string."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def id_text(value):
    """Normalize ids so 123, "123", 123.0 and " 123 " all become "123"."""
    value = text(value)
    if value.endswith(".0") and value[:-2].isdigit():
        value = value[:-2]
    return value


def to_int(value, default=0):
    try:
        return int(float(text(value)))
    except (TypeError, ValueError):
        return default


def _api(provider, action=None, limit=None, **extra):
    """Call player_api.php with the first line's credentials and return parsed JSON."""
    line = first_line(provider)
    params = {"username": line.get("username") or "", "password": line.get("password") or ""}
    if action:
        params["action"] = action
    params.update(extra)
    label = "Xtream %s" % (action or "account info")
    deadline = time.monotonic() + (API_DEADLINE if limit is None else limit)
    resp = get(base_url(line_server(provider, line)) + "/player_api.php", provider, params=params, label=label,
               stream=True, deadline=deadline)
    try:
        raw = read_body(resp, deadline, label)
    except (requests.RequestException, urllib3.exceptions.HTTPError, OSError) as exc:
        raise ProviderError("%s failed while reading: %s" % (label, redact(exc, secrets_of(provider))))
    finally:
        resp.close()
    body = raw.decode("utf-8-sig", errors="replace").strip()
    if not body:
        # Some panels answer an empty body for an empty list or a bad login.
        return None
    if body.startswith("<"):
        snippet = " ".join(body[:200].split())
        raise ProviderError("%s returned an HTML page instead of JSON (%s)"
                            % (label, redact(snippet, secrets_of(provider))))
    try:
        return json.loads(body)
    except ValueError as exc:
        raise ProviderError("%s returned invalid JSON: %s" % (label, exc))


def _check_auth(data, label):
    """Raise when the panel answered with a user_info block that says the login failed."""
    if isinstance(data, dict) and isinstance(data.get("user_info"), dict):
        if to_int(data["user_info"].get("auth"), 1) == 0:
            raise ProviderError("%s: login rejected by the provider" % label)


def _as_list(data, label):
    """Panels return [] , {} , null, or a dict keyed by index for lists. Accept all of them."""
    _check_auth(data, label)
    if data is None:
        return []
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        if not data:
            return []
        values = list(data.values())
        if all(isinstance(item, dict) for item in values):
            return values
    raise ProviderError("%s returned an unexpected response" % label)


def account_info(provider, limit=None):
    """Fetch account info for "test connection". Raises ProviderError on a rejected login."""
    data = _api(provider, limit=limit)
    if not isinstance(data, dict) or not isinstance(data.get("user_info"), dict):
        raise ProviderError("Xtream account info: login rejected or unexpected response")
    _check_auth(data, "Xtream account info")
    user = data["user_info"]
    server = data.get("server_info") if isinstance(data.get("server_info"), dict) else {}
    return {
        "status": text(user.get("status")) or "Unknown",
        "exp_date": text(user.get("exp_date")),
        "is_trial": text(user.get("is_trial")) == "1",
        "max_connections": to_int(user.get("max_connections")),
        "active_connections": to_int(user.get("active_cons")),
        "created_at": text(user.get("created_at")),
        "timezone": text(server.get("timezone")),
        "server_url": text(server.get("url")),
    }


def expiry(value):
    """A plan's exp_date as Unix seconds, or None when the panel reports none (null, 0, unlimited) or a value
    that cannot be a date: a sentinel such as -1 or 1, milliseconds are converted, a date string is read as UTC."""
    value = text(value)
    try:
        n = float(value)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return int(datetime.strptime(value, fmt).replace(tzinfo=timezone.utc).timestamp())
            except ValueError:
                pass
        return None
    if n != n or n in (float("inf"), float("-inf")):
        return None
    if n > 1e11:
        n /= 1000                        # milliseconds
    return int(n) if 1e9 <= n < 1e11 else None   # 2001 to 5138: anything else is not a real date


def get_live_categories(provider):
    """Return [{"category_id": str, "category_name": str}] in provider order."""
    out = []
    for item in _as_list(_api(provider, "get_live_categories"), "Xtream get_live_categories"):
        cid = id_text(item.get("category_id"))
        name = text(item.get("category_name")) or ("Category %s" % cid if cid else UNCATEGORIZED)
        out.append({"category_id": cid, "category_name": name})
    return out


def get_live_streams(provider):
    """Return the raw live stream dicts with normalized string fields."""
    out = []
    for item in _as_list(_api(provider, "get_live_streams"), "Xtream get_live_streams"):
        stream_type = text(item.get("stream_type")).lower()
        if stream_type in ("movie", "series"):
            continue
        sid = id_text(item.get("stream_id"))
        if not sid:
            continue
        cid = id_text(item.get("category_id"))
        if not cid and isinstance(item.get("category_ids"), list) and item["category_ids"]:
            cid = id_text(item["category_ids"][0])
        out.append({
            "stream_id": sid,
            "name": text(item.get("name")),
            "stream_icon": text(item.get("stream_icon")),
            "epg_channel_id": text(item.get("epg_channel_id")),
            "category_id": cid,
            "num": text(item.get("num")),
        })
    return out


def fetch_catalog(provider):
    """Download categories and live streams and return {"groups": [...], "streams": [...]}."""
    categories = get_live_categories(provider)
    streams = get_live_streams(provider)
    # Empty categories beside streams that name categories is usually a hiccup; catalog.refresh decides (it keeps a
    # good catalog with named groups instead of filing everything under Uncategorized).
    categories_missing = not categories and any(item["category_id"] for item in streams)
    names = {}
    groups = []
    for cat in categories:
        names.setdefault(cat["category_id"], cat["category_name"])
        if cat["category_name"] not in groups:
            groups.append(cat["category_name"])

    seen = set()
    result = []
    for item in streams:
        if item["stream_id"] in seen:
            continue
        seen.add(item["stream_id"])
        group = names.get(item["category_id"]) or UNCATEGORIZED
        if group not in groups:
            groups.append(group)
        result.append({
            "stream_id": item["stream_id"],
            "name": item["name"] or "Stream %s" % item["stream_id"],
            "group": group,
            "logo": item["stream_icon"],
            "tvg_id": item["epg_channel_id"],
            "num": item.get("num") or "",
        })
    out = {"groups": groups, "streams": result, "categories_missing": categories_missing}
    try:   # the plan's expiry for the provider card (None: the panel reports none); a failed read leaves the key out
        out["expires"] = expiry(account_info(provider, limit=EXPIRY_DEADLINE).get("exp_date"))
    except Exception:  # noqa: BLE001
        log.info("provider %s: plan expiry not read this refresh; keeping the last one", provider.get("id"))
    return out


def line_server(provider, line):
    """The line's own server address if it has one, else the provider's."""
    return (line.get("server") or "").strip() or provider.get("server")


def stream_url(provider, line, stream_id):
    """{server}/live/{username}/{password}/{stream_id}.ts for the given line."""
    safe = "@:+!$&'()*,;=-._~"
    return "%s/live/%s/%s/%s.ts" % (
        base_url(line_server(provider, line)),
        quote(line.get("username") or "", safe=safe),
        quote(line.get("password") or "", safe=safe),
        quote(id_text(stream_id), safe=safe),
    )


def xmltv_url(provider):
    line = first_line(provider)
    query = urlencode({"username": line.get("username") or "", "password": line.get("password") or ""})
    return "%s/xmltv.php?%s" % (base_url(line_server(provider, line)), query)


# -- video on demand -------------------------------------------------------------------------------------------------

# The movie and series lists of a large panel are tens of megabytes (one had 214,000 movies).
VOD_DEADLINE = 600


def _categories(action):
    def get(provider):
        out = []
        for item in _as_list(_api(provider, action), "Xtream " + action):
            cid = id_text(item.get("category_id"))
            out.append({"category_id": cid,
                        "category_name": text(item.get("category_name")) or ("Category %s" % cid if cid else UNCATEGORIZED)})
        return out
    return get


get_vod_categories = _categories("get_vod_categories")
get_series_categories = _categories("get_series_categories")


def _category_ids(item):
    """Every category an item is in: category_id, plus category_ids on panels that list several."""
    ids = []
    for value in [item.get("category_id")] + (item.get("category_ids") if isinstance(item.get("category_ids"), list) else []):
        cid = id_text(value)
        if cid and cid not in ids:
            ids.append(cid)
    return ids


def _year(item):
    for key in ("year", "releaseDate", "release_date", "releasedate"):
        value = text(item.get(key))
        if len(value) >= 4 and value[:4].isdigit() and value[:4] != "0000":
            return value[:4]
    return ""


def get_vod_streams(provider):
    """Every movie: {"id", "name", "ext", "categories", "year", "tmdb", "logo", "added"}."""
    out = []
    for item in _as_list(_api(provider, "get_vod_streams", VOD_DEADLINE), "Xtream get_vod_streams"):
        sid = id_text(item.get("stream_id"))
        if not sid:
            continue
        out.append({"id": sid, "name": text(item.get("name")), "ext": text(item.get("container_extension")) or "mp4",
                    "categories": _category_ids(item), "year": _year(item),
                    "tmdb": text(item.get("tmdb_id") or item.get("tmdb")), "logo": text(item.get("stream_icon")),
                    "added": to_int(item.get("added"))})
    return out


def get_series(provider):
    """Every series: {"id", "name", "categories", "year", "tmdb", "logo", "modified"}."""
    out = []
    for item in _as_list(_api(provider, "get_series", VOD_DEADLINE), "Xtream get_series"):
        sid = id_text(item.get("series_id"))
        if not sid:
            continue
        out.append({"id": sid, "name": text(item.get("name")), "categories": _category_ids(item),
                    "year": _year(item), "tmdb": text(item.get("tmdb_id") or item.get("tmdb")),
                    "logo": text(item.get("cover")), "modified": to_int(item.get("last_modified"))})
    return out


def get_series_episodes(provider, series_id):
    """One series' episodes: [{"id", "season", "episode", "title", "ext"}]. Panels answer the episodes as a dict
    keyed by season, a list of seasons, or not at all for a series without any."""
    data = _api(provider, "get_series_info", series_id=id_text(series_id))
    _check_auth(data, "Xtream get_series_info")
    episodes = data.get("episodes") if isinstance(data, dict) else None
    if isinstance(episodes, dict):
        groups = list(episodes.items())
    elif isinstance(episodes, list):
        groups = [(None, group) for group in episodes]
    else:
        groups = []
    out = []
    for season_key, group in groups:
        for ep in group if isinstance(group, list) else []:
            if not isinstance(ep, dict) or not id_text(ep.get("id")):
                continue
            season = to_int(ep.get("season"), to_int(season_key, 0))
            out.append({"id": id_text(ep.get("id")), "season": season, "episode": to_int(ep.get("episode_num")),
                        "title": text(ep.get("title")), "ext": text(ep.get("container_extension")) or "mp4"})
    return out


def vod_url(provider, line, kind, item_id, ext):
    """{server}/movie/{username}/{password}/{id}.{ext} for a movie, /series/... for an episode."""
    safe = "@:+!$&'()*,;=-._~"
    return "%s/%s/%s/%s/%s.%s" % (
        base_url(line_server(provider, line)), "series" if kind == "episode" else "movie",
        quote(line.get("username") or "", safe=safe), quote(line.get("password") or "", safe=safe),
        quote(id_text(item_id), safe=safe), quote(ext or "mp4", safe=safe))
