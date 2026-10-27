"""Xtream Codes client: account info, live categories and live streams.

Providers are inconsistent: numbers arrive as strings, fields are null, lists come back as
empty objects, and errors are sometimes HTML pages with status 200. Everything here is
normalized to plain strings before it reaches the catalog.
"""

import json
import logging
from urllib.parse import quote, urlencode

from .net import ProviderError, get, redact, secrets_of

log = logging.getLogger("coax.providers")

UNCATEGORIZED = "Uncategorized"


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


def first_line(provider):
    lines = provider.get("lines") or []
    if not lines:
        raise ProviderError("provider %s has no lines configured" % provider.get("id", "?"))
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


def _api(provider, action=None):
    """Call player_api.php with the first line's credentials and return parsed JSON."""
    line = first_line(provider)
    params = {"username": line.get("username") or "", "password": line.get("password") or ""}
    if action:
        params["action"] = action
    label = "Xtream %s" % (action or "account info")
    resp = get(base_url(provider.get("server")) + "/player_api.php", provider, params=params, label=label)
    body = resp.content.decode("utf-8-sig", errors="replace").strip()
    resp.close()
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


def account_info(provider):
    """Fetch account info for "test connection". Raises ProviderError on a rejected login."""
    data = _api(provider)
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
        })
    return out


def fetch_catalog(provider):
    """Download categories and live streams and return {"groups": [...], "streams": [...]}."""
    categories = get_live_categories(provider)
    streams = get_live_streams(provider)
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
        })
    return {"groups": groups, "streams": result}


def stream_url(provider, line, stream_id):
    """{server}/live/{username}/{password}/{stream_id}.ts for the given line."""
    safe = "@:+!$&'()*,;=-._~"
    return "%s/live/%s/%s/%s.ts" % (
        base_url(provider.get("server")),
        quote(line.get("username") or "", safe=safe),
        quote(line.get("password") or "", safe=safe),
        quote(id_text(stream_id), safe=safe),
    )


def xmltv_url(provider):
    line = first_line(provider)
    query = urlencode({"username": line.get("username") or "", "password": line.get("password") or ""})
    return "%s/xmltv.php?%s" % (base_url(provider.get("server")), query)
