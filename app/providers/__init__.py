"""Providers: Xtream Codes and M3U catalogs, the on-disk cache, and the refresh scheduler.

Public surface (see SPEC.md "providers"):
  catalog.refresh(provider) / catalog.load(provider_id)
  stream_url(provider, line, stream_id)
  xmltv_url(provider)
  scheduler.start(get_config)
"""

from . import catalog, m3u, scheduler, xtream
from .net import ProviderError

__all__ = ["catalog", "m3u", "scheduler", "xtream", "ProviderError",
           "stream_url", "xmltv_url", "test_connection", "stream_format", "usable_lines",
           "is_enabled"]


def _kind(provider):
    return (provider.get("type") or "xtream").lower()


def is_enabled(item):
    """Providers and lines are enabled unless "enabled" is explicitly false."""
    return isinstance(item, dict) and item.get("enabled", True) is not False


def usable_lines(provider):
    """The provider's enabled lines, in configured order."""
    return [line for line in (provider or {}).get("lines") or [] if is_enabled(line)]


def stream_format(provider, stream):
    """Engine format for one stream: the stream's own "format" ("hls"/"ts") when the catalog
    detected one, else the provider's "format".

    `stream` may be a catalog stream dict or a stream id (looked up in the cache).
    """
    provider = provider or {}
    fmt = ""
    if isinstance(stream, dict):
        fmt = stream.get("format") or ""
        if not fmt and stream.get("url"):
            fmt = m3u.stream_format_for_url(stream["url"])
    elif stream is not None:
        cached = catalog.find_stream(provider.get("id"), str(stream))
        if cached:
            fmt = cached.get("format") or (m3u.stream_format_for_url(cached["url"]) if cached.get("url") else "")
    return fmt or (provider.get("format") or "")


def stream_url(provider, line, stream_id):
    """Build the playable URL for one stream on one line.

    Xtream: {server}/live/{username}/{password}/{stream_id}.ts with the line's credentials.
    M3U: the cached URL, with the line's optional regex search/replace applied.
    Raises ProviderError when an M3U stream is not in the cache, or when the line's search
    pattern does not match the URL (so the caller skips that line).
    """
    if _kind(provider) == "m3u":
        stream = catalog.find_stream(provider.get("id"), stream_id)
        if not stream or not stream.get("url"):
            raise ProviderError("stream %s not found in the catalog of provider %s"
                                % (stream_id, provider.get("id")))
        return m3u.apply_line_rewrite(stream["url"], line)
    return xtream.stream_url(provider, line, stream_id)


def xmltv_url(provider):
    """The provider's XMLTV URL: xmltv_url if set, else Xtream xmltv.php, else empty."""
    explicit = (provider.get("xmltv_url") or "").strip()
    if explicit:
        return explicit
    if _kind(provider) == "xtream" and provider.get("lines"):
        return xtream.xmltv_url(provider)
    return ""


def test_connection(provider):
    """Check a provider for the "test connection" button. Returns a dict; never raises.

    Xtream: account info (status, expiry, connections). M3U: downloads and counts streams.
    """
    try:
        if _kind(provider) == "m3u":
            parsed = m3u.download(provider)
            return {"ok": True, "streams": len(parsed["entries"])}
        info = xtream.account_info(provider)
        info["ok"] = info["status"].lower() == "active"
        if not info["ok"]:
            info["error"] = "account status is %s" % info["status"]
        return info
    except ProviderError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        from .net import redact, secrets_of
        return {"ok": False, "error": redact("unexpected error: %s" % exc, secrets_of(provider))}
