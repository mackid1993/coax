"""Guide entries for event channels, read from the stream's own name.

Providers carry live sports and other events on numbered channels that exist for one event at a time. The
channel is called "NHL 01: New Jersey Devils vs. Vegas Golden Knights @ 30 Oct 06:00 PM ET" today and
"NHL 01: NO EVENT" or another game tomorrow. These channels have no Gracenote station and rarely a useful XMLTV
entry, so Channels DVR shows them blank and nothing on them can be recorded.

For a channel with no guide of its own, coax reads the name its main stream has in the provider's catalog right
now. When that name is an event with a date and a time, the lineup's XMLTV gets an airing for it; when it says
nothing is on, a "No event scheduled" block. Any other name is left alone, so a normal channel never changes.

The approach and the Channels DVR details come from epg3r by Jon Maddox (https://github.com/maddox/epg3r),
written in Go and ported here in coax's own terms:
  * The episode-num value starts with "episode/". Channels DVR uses such a value as the airing's identity
    whatever its system attribute says; any other episode-num is ignored and the sub-title decides. The id is
    the same on every channel carrying the event, so Channels DVR records it once.
  * The identity is the day (read in one zone, coax's) and the sides of a matchup in sorted order, never the
    minute: "A vs B" and "B at A" are one game, and a provider moving 7:00 to 7:05 is not a new one.
  * Airings are marked live and new, with the "Sports event" and "Sports" categories, so Channels DVR files
    them as sports and passes that record only new airings see them.
  * A time with no day is not evidence of a day: providers keep a game's name on a channel long after it
    ended, and reading it as today puts last night's game on tonight. Such a name gets no airing.
  * A "start: ... stop: ..." window states no zone (epg3r found one provider's five hours off the others' for
    the same games) and its stop is the provider's block length, not the game's end, so it is never read. coax
    goes one step further: no time that names no zone ("19:30", "2026-10-30 13:00:00") is read, because a
    guessed zone puts the airing, and its recording, hours off.
  * A block with nothing on still gets an entry: an empty guide cell looks broken rather than empty.
  * Game lengths per league (NFL 3 h 30 min, NBA 3 h, ...) for the airing's end, and the airing titles, from
    epg3r's league table (the only data taken from it; its team roster and Gracenote ids are not used).
"""

import datetime
import hashlib
import logging
import re
import unicodedata
import xml.etree.ElementTree as ET
import zoneinfo

log = logging.getLogger("coax.guide.events")

GENERIC_HOURS = 3.0          # an event of no known league
FILLER_HOURS = 24            # a block with nothing on covers this much from the hour it was built in
FAR_DAYS = 30                # a date further than this from today is a sentinel ("2098 12 31"), not an event

# League words in slot labels, with what Gracenote calls their airings, the genre, and how long a game runs.
LEAGUES = {
    "NFL": ("NFL Football", "Football", 3.5),
    "MLB": ("MLB Baseball", "Baseball", 3.5),
    "NBA": ("NBA Basketball", "Basketball", 3.0),
    "NHL": ("NHL Hockey", "Hockey", 3.0),
    "WNBA": ("WNBA Basketball", "Basketball", 2.5),
    "MLS": ("MLS Soccer", "Soccer", 2.0),
    "NCAAF": ("College Football", "Football", 3.75),
    "NCAAB": ("College Basketball", "Basketball", 2.5),
    "NCAAM": ("College Basketball", "Basketball", 2.5),
    "NCAAMB": ("College Basketball", "Basketball", 2.5),
    "NCAAW": ("College Women's Basketball", "Basketball", 2.5),
    "NCAAWB": ("College Women's Basketball", "Basketball", 2.5),
}
CATEGORIES = ("Sports event", "Sports")

# Zone abbreviations as providers write them. "EST" all year means New York, not a fixed offset: providers never
# switch to "EDT".
ZONES = {
    "ET": "America/New_York", "EST": "America/New_York", "EDT": "America/New_York",
    "CT": "America/Chicago", "CST": "America/Chicago", "CDT": "America/Chicago",
    "MT": "America/Denver", "MST": "America/Denver", "MDT": "America/Denver",
    "PT": "America/Los_Angeles", "PST": "America/Los_Angeles", "PDT": "America/Los_Angeles",
    "GMT": "UTC", "UTC": "UTC", "UK": "Europe/London", "BST": "Europe/London",
    "CET": "Europe/Berlin", "CEST": "Europe/Berlin",
}
MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
                                      "nov", "dec"), 1)}

_MON = (r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?"
        r"|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b\.?")
_WDAY = (r"(?:mon(?:day)?|tue(?:s(?:day)?)?|wed(?:nesday)?|thu(?:r(?:s(?:day)?)?)?|fri(?:day)?|sat(?:urday)?"
         r"|sun(?:day)?)\b\.?,?")
_ZONE = r"(?:%s)" % "|".join(sorted(ZONES, key=len, reverse=True))

# The slot label: an optional country ("USA | "), a label of up to four words ("NHL", "ESPN+", "DAZN US"), the
# slot number, then ":" or "|" or " - ". "NFL  | 02 - 1pm Ravens at Bills" and "NHL 01: ..." both read.
# "US (Peacock 002) | ..." puts the label in parentheses.
_LABEL = r"(?P<label>[A-Za-z][\w+&'.]*(?:\s+[A-Za-z][\w+&'.]*){0,3}?)"
SLOT = re.compile(r"^(?:[A-Z]{2,4}\s*[|:]\s*|[A-Z]{2,4}\s+(?=\())?\(?" + _LABEL + r"\s*\|?\s*"
                  r"(?P<num>\d{1,3})\s*\)?\s*(?::|\||\s-(?=\s|$))\s*(?P<rest>.*)$")
# The label last: "NEXT | TEMPLE VS. EAST CAROLINA | Fri 30 Oct 12:00 EDT (US) | 8K EXCLUSIVE | US: ESPN+ PPV 13".
TRAIL_SLOT = re.compile(r"^(?:[A-Z]{2,4}\s*:\s*)?" + _LABEL + r"\s+(?P<num>\d{1,3})$")
_STATUS = re.compile(r"^(?:live|next|end|ended|upcoming|soon|replay)$", re.I)
_QUALITY = re.compile(r"^(?:(?:\d+k|[fu]?hd|sd|raw|hevc|vip|exclusive|all|multi|\d{3,4}p)\s*)+$", re.I)
PLACEHOLDER = re.compile(r"^(?:no (?:scheduled )?(?:events?|games?|match(?:es)?|streams?)(?: scheduled| today)?|"
                         r"offline|off ?air|not scheduled|no schedule|tba|tbd|to be announced|coming soon|"
                         r"stand ?by|(?:channel |event |stream )?will be updated.*)\.?$", re.I)
SENTINEL = re.compile(r"^\(?\s*20[6-9]\d\b[^()]*\)?$")   # "(2098 12 31 08:00:17)": nothing scheduled
WINDOW = re.compile(r"\bstart:\s*\d{4}[ -]\d{2}[ -]\d{2}\b", re.I)
_DATE = (r"(?:(?P<d1>\d{1,2})(?:st|nd|rd|th)?\s+(?P<m1>%(mon)s)|(?P<m2>%(mon)s)\s+(?P<d2>\d{1,2})(?:st|nd|rd|th)?"
         r"|(?P<y3>\d{4})[ -](?P<m3>\d{2})[ -](?P<d3>\d{2})|(?P<m4>\d{1,2})[./-](?P<d4>\d{1,2})"
         r"(?:[./-](?P<y4>\d{2,4}))?)(?:,?\s+(?P<y>\d{4}))?") % {"mon": _MON}
_TIME = (r"(?:(?P<h>\d{1,2})(?::(?P<mi>\d{2}))?\s*(?P<ap>[ap])\.?m\.?|(?P<h24>\d{1,2}):(?P<mi24>\d{2})(?::\d{2})?)"
         r"(?:\s*\(?(?P<tz>%s)\)?)?") % _ZONE
# A date and a time at the end of the name, in the arrangements providers use: "@ 30 Oct 06:00 PM ET",
# "@ Oct 30 4:55AM ET", "(09/13) 1:00PM ET", "| 09/03 07:00PM", "(Sunday 09.08 1:00 PM ET)".
SCHEDULE = re.compile(r"^(?P<body>.*?)\s*(?:[@|,-]\s*)?\(?(?:%(wd)s\s+)?%(date)s\)?\s*(?:%(wd)s\s+)?[,@|-]?\s*"
                      r"\(?%(time)s\)?\s*$" % {"wd": _WDAY, "date": _DATE, "time": _TIME}, re.I)
_TRAIL_PAREN = re.compile(r"\s*\(([^()]*)\)\s*$")
# Trailing notes that make it another event rather than another feed of the same one: a language (a Spanish
# broadcast is not the English one), a women's or men's game, game 2 of a doubleheader. Other notes ("Panthers
# Broadcast", "In-Arena", "FOX") name a feed, and every feed shares one listing.
DISTINCT = re.compile(r"\b(?:w|m|women|men|women's|men's|womens|mens|game\s*\d+|espa[nñ]ol|spanish|french|"
                      r"fran[cç]ais|portugu[eê]se?|portugu[eê]s|deutsch|german|italiano|italian|arabic)\b", re.I)
_LEAD_DATE = re.compile(r"^(?:%s\s+)?\d{1,2}/\d{1,2}\s+-\s+" % _WDAY, re.I)
_SIDES = re.compile(r"\s+(?:vs\.?|v\.?|versus|x|at|@)\s+", re.I)


def _clean(name):
    text = "".join(ch for ch in unicodedata.normalize("NFKC", name or "") if ch not in "​⁩﻿")
    text = re.sub(r"[‐-―]", "-", text)
    return re.sub(r"\s+", " ", text).strip()


def _key(text):
    """Lower case letters and digits only, for comparing names."""
    text = unicodedata.normalize("NFKD", text or "")
    return re.sub(r"[^a-z0-9]+", " ", "".join(c for c in text if not unicodedata.combining(c)).lower()).strip()


def _year(month, day, today):
    """The year that puts month/day nearest today (a January game listed in December is next year's)."""
    best = None
    for year in (today.year - 1, today.year, today.year + 1):
        try:
            when = datetime.date(year, month, day)
        except ValueError:
            continue
        if best is None or abs((when - today).days) < abs((best - today).days):
            best = when
    return best


def _when(m, now):
    """The kickoff a SCHEDULE match names, as an aware datetime, or None when it states no zone or is not a real
    date."""
    g = m.groupdict()
    if not g.get("tz"):
        return None   # the numbers alone do not say which clock they are on; a guessed zone misses the game
    tz = zoneinfo.ZoneInfo(ZONES[g["tz"].upper()])
    today = now.astimezone(tz).date()
    try:
        if g["m1"] or g["m2"]:
            month, day = MONTHS[(g["m1"] or g["m2"]).lower()[:3]], int(g["d1"] or g["d2"])
            year = g["y"]
        elif g["y3"]:
            month, day, year = int(g["m3"]), int(g["d3"]), g["y3"]
        else:
            month, day = int(g["m4"]), int(g["d4"])
            if month > 12 >= day:     # a day-first feed
                month, day = day, month
            year = g["y4"] or g["y"]
        if year:
            year = int(year) + (2000 if int(year) < 100 else 0)
            date = datetime.date(year, month, day)
        else:
            date = _year(month, day, today)
        if g["h"] is not None:
            if int(g["h"]) > 12:
                return None
            hour, minute = int(g["h"]) % 12 + (12 if g["ap"].lower() == "p" else 0), int(g["mi"] or 0)
        else:
            hour, minute = int(g["h24"]), int(g["mi24"])
        when = datetime.datetime.combine(date, datetime.time(hour, minute), tz)
    except (ValueError, TypeError, KeyError):
        return None
    if date is None or abs((date - today).days) > FAR_DAYS:
        return None
    return when


def parse(name, zone, now):
    """What a stream's name says is on.

    Returns None for a name that is not an event channel's, {"kind": "idle", ...} for one that says nothing is
    on, or {"kind": "event", "label", "slot", "event", "notes", "league", "start", "key"} for an event with a
    date and a time. A slot name with an event but no usable date and time returns None: it gets no airing.
    """
    name = _clean(name)
    m = SLOT.match(name)
    if m:
        label, slot, rest = m.group("label").strip(), m.group("num"), m.group("rest").strip(" -|:")
    else:
        segs = [seg.strip() for seg in name.split("|")]
        m = TRAIL_SLOT.match(segs[-1]) if len(segs) >= 3 else None
        if not m:
            return None
        segs = segs[1:-1] if _STATUS.match(segs[0]) else segs[:-1]
        label, slot = m.group("label").strip(), m.group("num")
        rest = " | ".join(seg for seg in segs if seg and not _QUALITY.match(seg))
    base = {"label": label, "slot": slot}
    if not rest or PLACEHOLDER.match(rest) or SENTINEL.match(rest):
        return dict(base, kind="idle")
    if WINDOW.search(rest):
        return None
    notes = []
    s = SCHEDULE.match(rest)
    while not s and len(notes) < 3:
        # "(09.08 1:00 PM ET) (FOX)": a note after the schedule.
        p = _TRAIL_PAREN.search(rest)
        if not p or p.start() == 0:
            return None
        notes.insert(0, p.group(1).strip())
        rest = rest[:p.start()].strip()
        s = SCHEDULE.match(rest)
    if not s:
        return None
    start = _when(s, now)
    if start is None:
        return None
    body = s.group("body").strip(" -|:@")
    while True:
        p = _TRAIL_PAREN.search(body)
        if not p or p.start() == 0:
            break
        notes.insert(0, p.group(1).strip())
        body = body[:p.start()].strip()
    body = _LEAD_DATE.sub("", body).strip()
    if not body:
        return None
    # The league is the whole label, after a country ("USA NBA"); "MLS NEXT Pro" is not MLS.
    words = label.upper().split()
    words = words[1:] if len(words) == 2 and words[0] in ("US", "USA", "CA", "CAN", "UK") else words
    league = words[0] if len(words) == 1 and words[0] in LEAGUES else ""
    # One identity per event: the league (or label), the day in coax's zone, the sides in sorted order (or the
    # name), and the notes that make it a different event.
    sides = sorted(_key(side) for side in _SIDES.split(body)) if _SIDES.search(body) else [_key(body)]
    other = [_key(n) for n in notes if DISTINCT.search(n)]
    key = "|".join([league or _key(label), start.astimezone(zone).date().isoformat()] + sides + other)
    return dict(base, kind="event", event=body, notes=notes, league=league, start=start,
                key=hashlib.sha1(key.encode("utf-8")).hexdigest()[:16])


# ----------------------------------------------------------------------
# Which channels, and their XMLTV
# ----------------------------------------------------------------------

def guide_source(lineup_guide, g):
    """Where a channel's guide comes from in a lineup with this guide setting: "gracenote" (a station), "xmltv"
    (an XMLTV id the lineup sends, a new channel's provider tvg-id included) or "" (none)."""
    station, xmltv_id = (g.get("station_id") or "").strip(), (g.get("xmltv_id") or "").strip()
    if lineup_guide == "xmltv":
        return "xmltv" if xmltv_id else ""
    if lineup_guide in ("gracenote", "both"):
        if station:
            return "gracenote"
        return "xmltv" if lineup_guide == "both" and xmltv_id else ""
    mode = g.get("mode")
    if mode == "gracenote" and station:
        return "gracenote"
    return "xmltv" if mode == "xmltv" and xmltv_id else ""


def _zone(config):
    from app import tz
    try:
        return zoneinfo.ZoneInfo(tz.effective(config)[0])
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        return datetime.timezone.utc


def _stream_name(ref):
    try:
        from app.providers import catalog
        stream = catalog.find_stream(ref.get("provider"), ref.get("stream_id"))
    except Exception:  # noqa: BLE001 - a guide without event entries is better than no guide
        log.exception("could not read the catalog for an event channel")
        return ""
    return (stream or {}).get("name") or ""


def lineup_events(config, lineup_id, now=None, covered=None):
    """[(channel, parsed)] for the lineup's channels with no guide whose main stream's name is an event or says
    nothing is on. A channel whose guide is an XMLTV id counts as having none when its provider's XMLTV gave it no
    programmes: covered is the set of channel ids that got some (None: unknown, count it as none). A lineup
    switched off, or one that takes Gracenote only (no XMLTV reaches Channels DVR), has none."""
    from app.guide import output
    lineup = output.find_lineup(config, lineup_id)
    if lineup is None or lineup.get("enabled", True) is False or lineup.get("guide") == "gracenote":
        return []
    now = now or datetime.datetime.now(datetime.timezone.utc)
    zone = _zone(config)
    off = output.off_providers(config)
    channels = {str(ch.get("id")): ch for ch in config.get("channels") or []}
    out, seen = [], set()
    for cid in lineup.get("channels") or []:
        ch = channels.get(str(cid))
        if ch is None or str(cid) in seen or not output.listed(ch, off) or not ch.get("uuid"):
            continue
        seen.add(str(cid))
        source = guide_source(lineup.get("guide") or "per-channel", ch.get("guide") or {})
        if source == "gracenote" or (source == "xmltv" and covered is not None and ch.get("id") in covered):
            continue
        ref = next((r for r in ch.get("streams") or []
                    if not r.get("disabled") and str(r.get("provider")) not in off), None)
        parsed = parse(_stream_name(ref), zone, now) if ref else None
        if parsed:
            out.append((ch, parsed))
    return out


def fingerprint(config, lineup_id):
    """What the lineup's event entries say, without the filler around them: the DVR sync downloads the guide
    again when it changes (a catalog refresh gave a slot a new event)."""
    return [(ch.get("id"), p["kind"], p.get("key"), p["start"].isoformat() if p.get("start") else "")
            for ch, p in lineup_events(config, lineup_id)]


def _stamp(when):
    return when.astimezone(datetime.timezone.utc).strftime("%Y%m%d%H%M%S +0000")


def _text(parent, tag, text, **attrs):
    elem = ET.SubElement(parent, tag, attrs)
    if text is not None:
        elem.text = text
    return elem


def _programme(cid, start, stop, title, desc="", logo=""):
    elem = ET.Element("programme", {"start": _stamp(start), "stop": _stamp(stop), "channel": f"coax-{cid}"})
    _text(elem, "title", title, lang="en")
    if desc:
        _text(elem, "desc", desc, lang="en")
    if logo:
        _text(elem, "icon", None, src=logo)
    return elem


def entries(config, lineup_id, now=None, covered=None, listed=()):
    """XMLTV <channel> and <programme> elements for the lineup's event channels: (channels, programmes).
    covered as in lineup_events; listed holds channel ids that already have a <channel> element."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    hour = now.astimezone(datetime.timezone.utc).replace(minute=0, second=0, microsecond=0)
    until = hour + datetime.timedelta(hours=FILLER_HOURS)
    zone = _zone(config)
    channels, programmes = [], []
    for ch, p in lineup_events(config, lineup_id, now, covered):
        cid, logo = ch["id"], ch.get("logo") or ""
        if cid not in listed:
            elem = ET.Element("channel", {"id": f"coax-{cid}"})
            _text(elem, "display-name", ch.get("name") or "")
            if logo:
                _text(elem, "icon", None, src=logo)
            channels.append(elem)
        nothing = "No event scheduled"
        if p["kind"] == "idle":
            programmes.append(_programme(cid, hour, until, nothing, logo=logo))
            continue
        title, genre, hours = LEAGUES.get(p["league"], (p["event"], "", GENERIC_HOURS))
        start = p["start"]
        stop = start + datetime.timedelta(hours=hours)
        if start > hour:
            programmes.append(_programme(cid, hour, start, "Up next: " + p["event"],
                                         "Starts " + start.astimezone(zone).strftime("%a %b %-d, %-I:%M %p"),
                                         logo))
        airing = _programme(cid, start, stop, title, logo=logo)
        if title != p["event"]:
            _text(airing, "sub-title", p["event"], lang="en")
        desc = p["event"] + "".join(" (%s)" % n for n in p["notes"])
        _text(airing, "desc", desc, lang="en")
        for cat in ([genre] if genre else []) + list(CATEGORIES):
            _text(airing, "category", cat, lang="en")
        _text(airing, "episode-num", "episode/" + p["key"], system="coax")
        _text(airing, "date", start.astimezone(zone).strftime("%Y%m%d"))
        _text(airing, "live", None)
        _text(airing, "new", None)
        programmes.append(_order(airing))
        if stop < until:
            programmes.append(_programme(cid, max(stop, hour), until, nothing, logo=logo))
    return channels, programmes


# Child order of <programme> in the XMLTV DTD, which strict readers check.
_ORDER = ("title", "sub-title", "desc", "date", "category", "icon", "episode-num", "new", "live")


def _order(elem):
    kids = sorted(list(elem), key=lambda k: _ORDER.index(k.tag) if k.tag in _ORDER else len(_ORDER))
    for kid in list(elem):
        elem.remove(kid)
    elem.extend(kids)
    return elem
