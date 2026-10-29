"""Lineup output for Channels DVR: the M3U playlist and the cached XMLTV file path."""

import re
import unicodedata

from app.guide import xmltv

STATION_ID_MODES = ("gracenote", "both")


def _attr(value):
    """Make a value safe inside a double-quoted M3U attribute.

    M3U has no escape syntax, so double quotes become single quotes and line breaks
    become spaces. Anything else is passed through unchanged.
    """
    text = "" if value is None else str(value)
    text = re.sub(r"[\r\n\t]+", " ", text).replace('"', "'")
    return text.strip()


def _title(value):
    """Channel title after the comma: one line, no surrounding whitespace."""
    text = re.sub(r"[\r\n\t]+", " ", "" if value is None else str(value))
    return text.replace('"', "'").strip()


def _num_key(value):
    try:
        return (0, float(str(value).strip()))
    except (TypeError, ValueError):
        return (1, 0.0)


def _name_key(value):
    """A to Z the way the Channels page sorts (localeCompare, numeric, base sensitivity): accents and case ignored,
    numbers by value (PPV 2 before PPV 10), spaces before punctuation before digits before letters (ESPN before
    ESPN2)."""
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).casefold()
    key = []
    for part in re.findall(r"\d+|\D", text):
        if part.isdigit():
            key.append((2, int(part), ""))
        elif part.isspace():
            key.append((0, 0, " "))
        elif part.isalpha():
            key.append((3, 0, part))
        else:
            key.append((1, 0, part))
    return key


def ordered_ids(lineup, channels):
    """Channel ids of a lineup in its chosen order: manual (as arranged in coax, whatever order they were
    added to the lineup in), provider (the provider's own channel number, from _pnum), or alpha (by name).
    channels is {id: channel} in coax's order."""
    ids = list(lineup.get("channels") or [])
    mode = lineup.get("sort") or "manual"
    name = lambda cid: _name_key((channels.get(str(cid)) or {}).get("name"))  # noqa: E731
    if mode == "alpha":
        ids.sort(key=name)
    elif mode == "provider":
        ids.sort(key=lambda cid: (_num_key((channels.get(str(cid)) or {}).get("_pnum")), name(cid)))
    else:
        position = {cid: i for i, cid in enumerate(channels)}
        ids.sort(key=lambda cid: position.get(str(cid), 1 << 30))
    return ids


def find_lineup(config, lineup_id):
    for lineup in config.get("lineups") or []:
        if str(lineup.get("id")) == str(lineup_id):
            return lineup
    return None


def m3u(config, lineup_id):
    """M3U text for one lineup. Raises KeyError if the lineup does not exist.

    One entry per enabled channel, in lineup order:
      #EXTINF:-1 channel-id="coax-<id>" tvg-id="coax-<id>" tvg-name=".." tvg-logo=".."
        group-title=".." channel-number=".." tvc-guide-stationid="..",<name>
      {base_url}/stream/{uuid}.ts
    tvc-guide-stationid follows the lineup's guide: always (when the channel has a station id) for
    gracenote and both, never for xmltv, and for per-channel only when the channel's mode is gracenote or both.
    """
    lineup = find_lineup(config, lineup_id)
    if lineup is None:
        raise KeyError(f"unknown lineup: {lineup_id}")
    base_url = ((config.get("server") or {}).get("base_url") or "").strip().rstrip("/")
    channels = {}
    for ch in config.get("channels") or []:
        channels.setdefault(str(ch.get("id")), ch)

    lineup_guide = lineup.get("guide") or "per-channel"
    lines = ["#EXTM3U"]
    seen = set()
    start = lineup.get("start_number")
    start = start if isinstance(start, int) and not isinstance(start, bool) else None
    position = 0
    for cid in ordered_ids(lineup, channels):
        ch = channels.get(str(cid))
        if ch is None or str(cid) in seen or not ch.get("enabled", True) or not ch.get("uuid"):
            continue
        seen.add(str(cid))
        position += 1
        coax_id = f"coax-{ch['id']}"
        name = ch.get("name") or ""
        attrs = [
            ("channel-id", coax_id),
            ("tvg-id", coax_id),
            ("tvg-name", name),
            ("tvg-logo", ch.get("logo") or ""),
            ("group-title", ch.get("group") or ""),
            # A lineup with a start number numbers its channels in its own order from there.
            ("channel-number", str(start + position - 1) if start is not None else (ch.get("number") or "")),
        ]
        g = ch.get("guide") or {}
        station_id = _attr(g.get("station_id"))
        if lineup_guide in STATION_ID_MODES:
            wants_station = True
        elif lineup_guide == "xmltv":
            wants_station = False
        else:
            wants_station = g.get("mode") in STATION_ID_MODES
        if wants_station and station_id:
            attrs.append(("tvc-guide-stationid", station_id))
        attr_text = " ".join(f'{key}="{_attr(value)}"' for key, value in attrs)
        lines.append(f"#EXTINF:-1 {attr_text},{_title(name)}")
        lines.append(f"{base_url}/stream/{ch['uuid']}.ts")
    return "\n".join(lines) + "\n"


def xmltv_path(lineup_id):
    """Path of the cached XMLTV file for a lineup: /data/cache/xmltv-<lineup_id>.xml.

    The file may not exist yet (no refresh has run); the server should check and can
    serve xmltv.empty_document() instead. Raises ValueError for an unsafe lineup id.
    """
    return xmltv.guide_path(lineup_id)
