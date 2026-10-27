"""M3U playlist download and parsing.

The download is streamed and parsed line by line, so a 50,000 entry playlist never sits in
memory as one big string. Gzip is handled both as Content-Encoding and as a .gz file body.
"""

import codecs
import hashlib
import logging
import re
import time
import zlib
from urllib.parse import urlsplit

import requests
import urllib3

from .net import ProviderError, get, iter_body, redact, secrets_of

log = logging.getLogger("coax.providers")

UNCATEGORIZED = "Uncategorized"
CHUNK_SIZE = 64 * 1024
# Overall wall-clock limit for one playlist download, on top of the per-read timeout, so a
# server trickling one byte at a time cannot stall the scheduler.
DOWNLOAD_DEADLINE = 300
# Longest accepted line search pattern; the stdlib re module has no timeout.
MAX_PATTERN = 500
_NEWLINE = re.compile(r"\r\n|\r|\n")

# key="value", key='value' or key=value (unquoted, up to whitespace or comma).
_ATTR = re.compile(r"""([A-Za-z0-9_][A-Za-z0-9_.:-]*)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s,"']+))""")


def stream_id_for(url):
    """Legacy id (before stable ids): first 16 hex chars of sha1(url). Kept for cache lookups."""
    return hashlib.sha1(url.strip().encode("utf-8")).hexdigest()[:16]


legacy_stream_id = stream_id_for

# Xtream style live paths: /live/<user>/<pass>/<id>[.ext] or /<user>/<pass>/<id>[.ext].
_XTREAM_PATH = re.compile(r"^/(?:live/)?[^/]+/[^/]+/(\d+)(?:\.(ts|m3u8))?/?$", re.IGNORECASE)
_MEDIA_EXT = re.compile(r"\.(?:m3u8?|ts|mp4|mkv|mpd|aac|mp3|flv|php)$", re.IGNORECASE)
_TOKENISH = re.compile(r"^[A-Za-z0-9_\-=.%~+]+$")


def xtream_id(url):
    """The numeric stream id of an Xtream style URL, or ""."""
    match = _XTREAM_PATH.match(urlsplit(url.strip()).path or "")
    return match.group(1) if match else ""


def _credential_like(segment):
    """A path part that looks like a token or password: long, mixed letters and digits."""
    if len(segment) < 12 or not _TOKENISH.match(segment) or _MEDIA_EXT.search(segment):
        return False
    return any(c.isdigit() for c in segment) and any(c.isalpha() for c in segment)


def normalized_url(url):
    """The URL without userinfo, query, fragment and credential-looking path parts."""
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    if parts.port:
        host = "%s:%d" % (host, parts.port)
    segments = [seg for seg in (parts.path or "").split("/") if seg]
    kept = [seg for seg in segments if not _credential_like(seg)]
    return "%s://%s/%s" % (parts.scheme.lower(), host, "/".join(kept))


def _digest(text):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def candidate_ids(entry):
    """Stream id candidates for an entry, most stable first.

    1. the Xtream numeric id from the URL path (survives credential and token rotation);
    2. a hash of tvg-id and name;
    3. a hash of the URL without query string and credential-looking path parts.
    The caller takes the first one not already used in the playlist.
    """
    url = entry["url"]
    out = []
    numeric = xtream_id(url)
    if numeric:
        out.append(numeric)
    name = entry.get("name") or ""
    if name == url:
        name = ""
    if entry.get("tvg_id") or name:
        out.append(_digest("tvg:%s\n%s" % (entry.get("tvg_id") or "", name)))
    out.append(_digest("url:" + normalized_url(url)))
    return out


def stream_format_for_url(url):
    """"hls" for a .m3u8 path, "ts" for a .ts path or an Xtream live path, else ""."""
    path = (urlsplit(url.strip()).path or "").lower().rstrip("/")
    if path.endswith(".m3u8"):
        return "hls"
    if path.endswith(".ts"):
        return "ts"
    if xtream_id(url):
        return "ts"
    return ""


def parse_attributes(text):
    attrs = {}
    for match in _ATTR.finditer(text):
        key = match.group(1).lower()
        value = next((g for g in match.groups()[1:] if g is not None), "")
        attrs.setdefault(key, value.strip())
    return attrs


def split_extinf(body):
    """Split the part after "#EXTINF:" into (attribute text, display name).

    The name starts after the first comma that is not inside a quoted attribute value,
    so both tvg-name="A, B" and names like "News, Weather" survive.
    """
    quote = None
    for index, char in enumerate(body):
        if quote:
            if char == quote:
                quote = None
        elif char in "\"'":
            # Only treat it as a quote when it opens an attribute value (key="...).
            if index > 0 and body[index - 1] == "=":
                quote = char
        elif char == ",":
            return body[:index], body[index + 1:].strip()
    return body, ""


def parse_lines(lines):
    """Parse an iterable of text lines. Returns {"header": {...}, "entries": [...]}.

    Each entry: {"name", "url", "tvg_id", "tvg_name", "logo", "group", "attrs"}.
    """
    header = {}
    entries = []
    pending = None
    extgrp = ""
    for raw in lines:
        line = raw.strip().lstrip("\ufeff")
        if not line:
            continue
        upper = line[:8].upper()
        if upper.startswith("#EXTM3U"):
            header.update(parse_attributes(line[7:]))
            continue
        if upper.startswith("#EXTINF"):
            body = line.split(":", 1)[1] if ":" in line else ""
            attr_text, name = split_extinf(body)
            pending = {"attrs": parse_attributes(attr_text), "name": name}
            extgrp = ""
            continue
        if upper.startswith("#EXTGRP"):
            extgrp = line.split(":", 1)[1].strip() if ":" in line else ""
            continue
        if line.startswith("#"):
            # #EXTVLCOPT, #KODIPROP and comments carry nothing the catalog needs.
            continue
        # A URL line. Lines without a preceding #EXTINF are still valid streams.
        url = line
        info = pending or {"attrs": {}, "name": ""}
        attrs = info["attrs"]
        tvg_name = attrs.get("tvg-name", "")
        entries.append({
            "name": info["name"] or tvg_name or url,
            "url": url,
            "tvg_id": attrs.get("tvg-id", ""),
            "tvg_name": tvg_name,
            "logo": attrs.get("tvg-logo", "") or attrs.get("logo", ""),
            "group": attrs.get("group-title", "") or extgrp or UNCATEGORIZED,
            "attrs": attrs,
        })
        pending = None
        extgrp = ""
    return {"header": header, "entries": entries}


def parse_text(text):
    return parse_lines(_NEWLINE.split(text.lstrip("\ufeff")))


def iter_text_lines(chunks):
    """Turn an iterable of byte chunks into text lines.

    Undoes gzip when the body starts with the gzip magic (a .gz file served as-is), drops a
    UTF-8 BOM, and splits on CRLF, LF or lone CR. Memory stays bounded by the chunk size.
    """
    decoder = codecs.getincrementaldecoder("utf-8-sig")(errors="replace")
    inflater = None
    started = False
    pending = ""
    head = b""
    for chunk in chunks:
        if not chunk:
            continue
        if not started:
            # Sniff the gzip magic from the first two bytes, even across tiny chunks.
            head += chunk
            if len(head) < 2:
                continue
            chunk, head, started = head, b"", True
            if chunk[:2] == b"\x1f\x8b":
                inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
        if inflater is not None:
            chunk = inflater.decompress(chunk)
        pending += decoder.decode(chunk)
        parts = _NEWLINE.split(pending)
        # A CRLF split across chunks yields one extra empty line, which the parser skips.
        pending = parts.pop()
        for part in parts:
            yield part
    if head:
        pending += decoder.decode(head)
    if inflater is not None:
        pending += decoder.decode(inflater.flush())
    pending += decoder.decode(b"", final=True)
    for part in _NEWLINE.split(pending):
        yield part


def parse_bytes(data):
    return parse_lines(iter_text_lines([data]))


def download(provider):
    """Stream-download and parse the provider's M3U. Raises ProviderError on failure."""
    url = (provider.get("m3u_url") or "").strip()
    if not url:
        raise ProviderError("M3U URL is empty")
    deadline = time.monotonic() + DOWNLOAD_DEADLINE
    resp = get(url, provider, stream=True, label="M3U download", deadline=deadline)
    try:
        # iter_body undoes Content-Encoding: gzip; iter_text_lines handles a raw .gz body.
        lines = iter_text_lines(iter_body(resp, deadline, "M3U download", CHUNK_SIZE))
        first = next((line for line in lines if line.strip()), "")
        if first.lstrip().lower().startswith(("<!doctype", "<html")):
            raise ProviderError("M3U download returned an HTML page instead of a playlist")
        parsed = parse_lines(_chain(first, lines))
    except (requests.RequestException, urllib3.exceptions.HTTPError, OSError, zlib.error) as exc:
        raise ProviderError("M3U download failed while reading: %s" % redact(exc, secrets_of(provider)))
    finally:
        resp.close()
    if not parsed["entries"]:
        raise ProviderError("M3U playlist contains no streams")
    return parsed


def _chain(first, rest):
    yield first
    for line in rest:
        yield line


def fetch_catalog(provider):
    """Download the playlist and return {"groups": [...], "streams": [...]}.

    Duplicate URLs are dropped. Stream ids come from candidate_ids(): the first candidate
    not yet taken in this playlist, so ids stay the same across refreshes even when tokens
    or credentials in the URLs rotate.
    """
    parsed = download(provider)
    groups = []
    seen_groups = set()
    seen_urls = set()
    seen_ids = set()
    streams = []
    for entry in parsed["entries"]:
        url = entry["url"].strip()
        if url in seen_urls:
            continue
        seen_urls.add(url)
        candidates = candidate_ids(entry)
        sid = next((c for c in candidates if c not in seen_ids), None)
        if sid is None:
            # Same normalized URL and name as an earlier entry: fall back to the full URL.
            sid = _digest("raw:" + url)
            if sid in seen_ids:
                continue
        seen_ids.add(sid)
        if entry["group"] not in seen_groups:
            seen_groups.add(entry["group"])
            groups.append(entry["group"])
        streams.append({
            "stream_id": sid,
            "name": entry["name"],
            "group": entry["group"],
            "logo": entry["logo"],
            "tvg_id": entry["tvg_id"],
            "url": entry["url"],
            "format": stream_format_for_url(entry["url"]),
        })
    return {"groups": groups, "streams": streams}


def _python_replacement(replace):
    """Translate a JavaScript (Dispatcharr) replacement to Python re syntax.

    $1 / $<name> / $& / $$ are converted; Python \\1 and \\g<name> pass through unchanged.
    """
    def convert(match):
        token = match.group(0)
        if token == "$$":
            return "$"
        if token == "$&":
            return "\\g<0>"
        if token.startswith("$<"):
            return "\\g<%s>" % match.group(2)
        return "\\g<%s>" % match.group(1)
    return re.sub(r"\$\$|\$&|\$(\d+)|\$<([A-Za-z_][A-Za-z0-9_]*)>", convert, replace or "")


def apply_line_rewrite(url, line):
    """Apply the line's optional regex search/replace. Empty search means unchanged.

    Raises ProviderError when the pattern is invalid, too long, or does not match the URL,
    so that line is skipped instead of silently streaming on another line's credentials.
    """
    line = line or {}
    search = line.get("search") or ""
    if not search:
        return url
    label = line.get("id", "?")
    if len(search) > MAX_PATTERN:
        raise ProviderError("line %s: search pattern is longer than %d characters" % (label, MAX_PATTERN))
    try:
        result, count = re.subn(search, _python_replacement(line.get("replace")), url)
    except (re.error, IndexError) as exc:
        raise ProviderError("line %s: invalid search/replace: %s" % (label, exc))
    if not count:
        raise ProviderError("line %s: search pattern does not match the stream URL" % label)
    return result
