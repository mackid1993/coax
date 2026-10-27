"""Lineup output for Channels DVR: the M3U playlist and the cached XMLTV file path."""

import re

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
    return re.sub(r"[\r\n\t]+", " ", "" if value is None else str(value)).strip()


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
    tvc-guide-stationid appears only for guide mode gracenote or both with a station id.
    """
    lineup = find_lineup(config, lineup_id)
    if lineup is None:
        raise KeyError(f"unknown lineup: {lineup_id}")
    base_url = ((config.get("server") or {}).get("base_url") or "").strip().rstrip("/")
    channels = {}
    for ch in config.get("channels") or []:
        channels.setdefault(str(ch.get("id")), ch)

    lines = ["#EXTM3U"]
    seen = set()
    for cid in lineup.get("channels") or []:
        ch = channels.get(str(cid))
        if ch is None or str(cid) in seen or not ch.get("enabled", True) or not ch.get("uuid"):
            continue
        seen.add(str(cid))
        coax_id = f"coax-{ch['id']}"
        name = ch.get("name") or ""
        attrs = [
            ("channel-id", coax_id),
            ("tvg-id", coax_id),
            ("tvg-name", name),
            ("tvg-logo", ch.get("logo") or ""),
            ("group-title", ch.get("group") or ""),
            ("channel-number", ch.get("number") or ""),
        ]
        g = ch.get("guide") or {}
        station_id = _attr(g.get("station_id"))
        if g.get("mode") in STATION_ID_MODES and station_id:
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
