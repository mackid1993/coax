"""M3U playlist download and parsing.

The download is streamed and parsed line by line, so a 50,000 entry playlist never sits in
memory as one big string. Gzip is handled both as Content-Encoding and as a .gz file body.
"""

import codecs
import hashlib
import logging
import re
import zlib

import requests

from .net import ProviderError, get, redact, secrets_of

log = logging.getLogger("coax.providers")

UNCATEGORIZED = "Uncategorized"
CHUNK_SIZE = 64 * 1024
_NEWLINE = re.compile(r"\r\n|\r|\n")

# key="value", key='value' or key=value (unquoted, up to whitespace or comma).
_ATTR = re.compile(r"""([A-Za-z0-9_][A-Za-z0-9_.:-]*)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s,"']+))""")


def stream_id_for(url):
    """Stable id for an M3U entry: first 16 hex chars of sha1(url)."""
    return hashlib.sha1(url.strip().encode("utf-8")).hexdigest()[:16]


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
    resp = get(url, provider, stream=True, label="M3U download")
    try:
        # iter_content undoes Content-Encoding: gzip; iter_text_lines handles a raw .gz body.
        lines = iter_text_lines(resp.iter_content(chunk_size=CHUNK_SIZE))
        first = next((line for line in lines if line.strip()), "")
        if first.lstrip().lower().startswith(("<!doctype", "<html")):
            raise ProviderError("M3U download returned an HTML page instead of a playlist")
        parsed = parse_lines(_chain(first, lines))
    except (requests.RequestException, OSError, zlib.error) as exc:
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
    """Download the playlist and return {"groups": [...], "streams": [...]}."""
    parsed = download(provider)
    groups = []
    seen_groups = set()
    seen_ids = set()
    streams = []
    for entry in parsed["entries"]:
        sid = stream_id_for(entry["url"])
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
        })
    return {"groups": groups, "streams": streams}


def _python_replacement(replace):
    """Accept Dispatcharr style $1 backreferences as well as Python \\1."""
    return re.sub(r"\$(\d+)", r"\\g<\1>", replace or "")


def apply_line_rewrite(url, line):
    """Apply the line's optional regex search/replace. Empty search means unchanged."""
    search = (line or {}).get("search") or ""
    if not search:
        return url
    try:
        return re.sub(search, _python_replacement(line.get("replace")), url)
    except (re.error, IndexError) as exc:
        log.warning("line %s: invalid search/replace (%s); using the URL unchanged",
                    line.get("id", "?"), exc)
        return url
