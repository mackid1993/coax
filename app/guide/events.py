"""Guide entries for event channels, read from the stream's own name and matched to Channels DVR's guide.

Providers carry live sports and other events on numbered channels that exist for one event at a time. The
channel is called "NHL 01: New Jersey Devils vs. Vegas Golden Knights @ 30 Oct 06:00 PM ET" today and
"NHL 01: NO EVENT" or another game tomorrow. These channels have no Gracenote station and rarely a useful XMLTV
entry, so Channels DVR shows them blank and nothing on them can be recorded.

For a channel with no guide of its own, coax reads the name its main stream has in the provider's catalog right
now. When that name is an event, the lineup's XMLTV gets an airing for it; when it says nothing is on, a "No event
scheduled" block. Any other name is left alone, so a normal channel never changes.

A game is then looked up in Channels DVR's own Gracenote guide (GET /dvr/guide/airings/<series id>, the upcoming
airings of one series on the user's sources; the series ids in SERIES were checked against Channels DVR). When the
same two teams play there at the time the name gives, the airing takes Gracenote's title, matchup, description,
picture, start and end. That also places a name with a time but no day ("NFL | 02 - 1pm Ravens at Bills"): the
day comes from Gracenote, never from a guess. The teams Gracenote names there give the Gracenote team ids of
games it does not list (an out-of-market game only the provider carries). A streaming game no TV guide lists
(ESPN+, Peacock, ...) is looked up on ESPN's public scoreboards, and team logos come from ESPN's team lists.

The approach and the Channels DVR details come from epg3r by Jon Maddox (https://github.com/maddox/epg3r),
written in Go and ported here in coax's own terms:
  * The episode-num value starts with "episode/". Channels DVR uses such a value as the airing's identity
    whatever its system attribute says; any other episode-num is ignored and the sub-title decides. The id is
    the same on every channel carrying the event, so Channels DVR records it once.
  * The identity is the day (read in one zone, coax's) and the sides of a matchup in sorted order, never the
    minute: "A vs B" and "B at A" are one game, and a provider moving 7:00 to 7:05 is not a new one.
  * Airings are marked live and new, with the "Sports event" and "Sports" categories, so Channels DVR files
    them as sports and passes that record only new airings see them; team ids go in <team-id system="tms">.
  * A time with no day is not evidence of a day: providers keep a game's name on a channel long after it
    ended, and reading it as today puts last night's game on tonight. Such a name gets an airing only when the
    Gracenote guide has that game at that time of day.
  * A "start: ... stop: ..." window states no zone (epg3r found one provider's five hours off the others' for
    the same games) and its stop is the provider's block length, not the game's end, so it is never read. coax
    goes one step further: no time that names no zone ("19:30", "2026-10-30 13:00:00") is read as a date,
    because a guessed zone puts the airing, and its recording, hours off.
  * A block with nothing on still gets an entry: an empty guide cell looks broken rather than empty.
  * Game lengths per league (NFL 3 h 30 min, NBA 3 h, ...) and airing titles from epg3r's league table, for
    games Gracenote does not list (the only data taken from epg3r; its team roster is not used).
"""

import datetime
import difflib
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

# The Gracenote series that holds each league's games, as Channels DVR lists them (/dvr/guide/airings/<id> answers
# "NFL Football", "NHL Hockey", ...). Public Gracenote ids, checked against Channels DVR's guide.
SERIES = {
    "NFL": "191277", "MLB": "191273", "NBA": "191276", "NHL": "191278", "WNBA": "191289", "MLS": "191274",
    "NCAAF": "191261", "NCAAB": "191260", "NCAAM": "191260", "NCAAMB": "191260", "NCAAW": "191292",
    "NCAAWB": "191292",
}
MATCH_HOURS = 2          # a dated name matches a Gracenote airing that starts this close to its time
CLOCK_MINUTES = 30       # a time with no day matches an airing within this of it, in a US zone
AHEAD_DAYS = 8           # how far ahead a time with no day looks for its game
US_ZONES = ("America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles")

# ESPN's public team lists, for team logos: https://site.api.espn.com/apis/site/v2/sports/<path>/teams
ESPN_PATHS = {
    "NFL": "football/nfl", "MLB": "baseball/mlb", "NBA": "basketball/nba", "NHL": "hockey/nhl",
    "WNBA": "basketball/wnba", "MLS": "soccer/usa.1", "NCAAF": "football/college-football",
    "NCAAB": "basketball/mens-college-basketball", "NCAAM": "basketball/mens-college-basketball",
    "NCAAMB": "basketball/mens-college-basketball", "NCAAW": "basketball/womens-college-basketball",
    "NCAAWB": "basketball/womens-college-basketball",
}
ESPN_TEAMS = "https://site.api.espn.com/apis/site/v2/sports/%s/teams?limit=1000"
ESPN_LEAGUE_LOGO = "https://a.espncdn.com/i/teamlogos/leagues/500/%s.png"   # nfl, mlb, nba, nhl, wnba, mls
ESPN_DAYS = 7            # team lists change a few times a year; kept on disk this long
# ESPN's public scoreboards list games by league whoever streams them (ESPN+, SECN+, Peacock, ...): asked, one day at
# a time, only for a game in a slot of no known league that Channels DVR's guide does not have.
ESPN_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/%s/scoreboard?dates=%s&limit=500"
ESPN_BOARDS = (
    "football/college-football", "basketball/mens-college-basketball", "basketball/womens-college-basketball",
    "volleyball/womens-college-volleyball", "soccer/usa.ncaa.m.1", "soccer/usa.ncaa.w.1", "hockey/mens-college-hockey",
    "hockey/womens-college-hockey", "field-hockey/womens-college-field-hockey", "baseball/college-baseball",
    "baseball/college-softball", "lacrosse/mens-college-lacrosse", "lacrosse/womens-college-lacrosse",
    "football/nfl", "basketball/nba", "basketball/wnba", "hockey/nhl", "baseball/mlb", "soccer/usa.1",
    "soccer/usa.nwsl", "soccer/usa.usl.1", "soccer/eng.1", "soccer/eng.2", "soccer/esp.1", "soccer/ger.1",
    "soccer/ger.2", "soccer/ita.1", "soccer/fra.1", "soccer/mex.1", "soccer/aus.1", "soccer/uefa.champions",
    "soccer/uefa.europa", "soccer/uefa.europa.conf", "mma/ufc",
)
SPORTS = {"mma": "Mixed martial arts", "field-hockey": "Field hockey"}

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
                  r"(?P<num>\d{1,3}|4k)\s*\)?\s*(?::|\||\s-(?=\s|$))\s*(?P<rest>.*)$", re.I)
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
LANGUAGE = (r"espa[nñ]ol|spanish|french|fran[cç]ais|portugu[eê]se?|portugu[eê]s|deutsch|german|italiano|italian|"
            r"arabic")
DISTINCT = re.compile(r"\b(?:w|m|women|men|women's|men's|womens|mens|game\s*\d+|%s)\b" % LANGUAGE, re.I)
_LANGUAGE = re.compile(r"\b(?:%s)\b" % LANGUAGE, re.I)
# A time of day with no date: "1pm Ravens at Bills", "SNF 8:20pm Eagles at Commanders", "Phillies vs Marlins 7:05 PM".
_CLOCK = r"(?P<h>\d{1,2})(?::(?P<mi>\d{2}))?\s*(?P<ap>[ap])\.?m\.?(?:\s*\(?(?P<tz>%s)\)?)?" % _ZONE
_NIGHT = r"(?:(?:snf|mnf|tnf|sunday night|monday night|thursday night)\s+)?"
CLOCK_FIRST = re.compile(r"^%s%s\s+%s(?P<body>.+)$" % (_NIGHT, _CLOCK, _NIGHT), re.I)
CLOCK_LAST = re.compile(r"^(?P<body>.+?)\s*[@|,-]?\s*%s\s*$" % _CLOCK, re.I)
_ALT_CAST = re.compile(r"\b(?:alt(?:ernate)?\s*cast|simulcast|megacast|manningcast|prime vision)\b", re.I)
_LEAD_DATE = re.compile(r"^(?:%s\s+)?\d{1,2}/\d{1,2}\s+-\s+" % _WDAY, re.I)
_SIDES = re.compile(r"\s+(?:vs\.?|v\.?|versus|x|at|@)\s+", re.I)


def _clean(name):
    text = "".join(ch for ch in unicodedata.normalize("NFKC", name or "") if ch not in "\u200b\u2069\ufeff")
    text = re.sub(r"[\u2010-\u2015]", "-", text)
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


def _league(label):
    """The league is the whole label, after a country ("USA NBA"); "MLS NEXT Pro" is not MLS."""
    words = label.upper().split()
    words = words[1:] if len(words) == 2 and words[0] in ("US", "USA", "CA", "CAN", "UK") else words
    return words[0] if len(words) == 1 and words[0] in LEAGUES else ""


def _peel_notes(body, notes):
    while True:
        p = _TRAIL_PAREN.search(body)
        if not p or p.start() == 0:
            return body
        notes.insert(0, p.group(1).strip())
        body = body[:p.start()].strip()


def _identity(league, label, day, body, notes):
    """One id per event: the league (or label), the day in coax's zone, the sides in sorted order (or the name),
    and the notes that make it a different event."""
    sides = sorted(_key(side) for side in _SIDES.split(body)) if _SIDES.search(body) else [_key(body)]
    other = [_key(n) for n in notes if DISTINCT.search(n)]
    key = "|".join([league or _key(label), day.isoformat()] + sides + other)
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def _sides(body):
    parts = _SIDES.split(body, maxsplit=1)
    return [part.strip() for part in parts] if len(parts) == 2 and all(p.strip() for p in parts) else []


def parse(name, zone, now):
    """What a stream's name says is on.

    Returns None for a name that is not an event channel's, {"kind": "idle", ...} for one that says nothing is
    on, {"kind": "event", "label", "slot", "event", "sides", "notes", "league", "start", "key"} for an event with
    a date, a time and a zone, or {"kind": "undated", ..., "clock": (hour, minute), "tz"} for a matchup with a time
    of day and no date (it gets an airing only when Channels DVR's guide places it). Any other slot name returns
    None: it gets no airing.
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
    base = {"label": label, "slot": slot, "league": _league(label)}
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
            break
        notes.insert(0, p.group(1).strip())
        rest = rest[:p.start()].strip()
        s = SCHEDULE.match(rest)
    if not s:
        return _undated(base, rest, notes)
    start = _when(s, now)
    if start is None:
        return None
    body = _LEAD_DATE.sub("", _peel_notes(s.group("body").strip(" -|:@"), notes)).strip()
    if not body:
        return None
    return dict(base, kind="event", event=body, sides=_sides(body), notes=notes, start=start,
                key=_identity(base["league"], label, start.astimezone(zone).date(), body, notes))


def _undated(base, rest, notes):
    m = CLOCK_FIRST.match(rest) or CLOCK_LAST.match(rest)
    if not m or int(m.group("h")) > 12:
        return None
    body = _peel_notes(m.group("body").strip(" -|:@"), notes)
    sides = _sides(body)
    if not sides:
        return None
    clock = (int(m.group("h")) % 12 + (12 if m.group("ap").lower() == "p" else 0), int(m.group("mi") or 0))
    key = hashlib.sha1("|".join([base["league"] or _key(base["label"]), "%02d:%02d" % clock]
                                + sorted(_key(x) for x in sides)).encode("utf-8")).hexdigest()[:16]
    return dict(base, kind="undated", event=body, sides=sides, notes=notes, clock=clock,
                tz=(m.group("tz") or "").upper(), key=key)


# ----------------------------------------------------------------------
# Channels DVR's Gracenote guide
# ----------------------------------------------------------------------

_STOP = {"the", "fc", "sc", "cf", "afc", "club", "university", "of"}


def _tokens(text):
    return set(_key(text).split()) - _STOP


FUZZY = 0.9     # how alike two words of 5 letters or more must be to count as one misspelled ("Clevland")


def _word_in(word, words, loose):
    """A word is in a name when it is one of its words, the initials of words next to each other ("NY" in "New
    York Jets", "KC"), or, loose, the same word misspelled."""
    if word in words:
        return True
    if 2 <= len(word) <= 3 and word.isalpha() and word in "".join(w[0] for w in words):
        return True
    return loose and len(word) >= 5 and any(
        len(w) >= 5 and difflib.SequenceMatcher(None, word, w).ratio() >= FUZZY for w in words)


def _same_team(ours, theirs, level=1):
    """Level 0: the same words. Level 1: every word of one name is in the other ("Devils" is "New Jersey Devils",
    "NY Jets" is "New York Jets"). Level 2: a word may also be misspelled ("Clevland"). Callers try the levels in
    turn (_hits), so "Kansas State" is Kansas State and not Kansas, and "Kansas" never becomes "Arkansas"."""
    a, b = _key(ours).split(), _key(theirs).split()
    a, b = [w for w in a if w not in _STOP], [w for w in b if w not in _STOP]
    if not a or not b:
        return False
    if level == 0:
        return set(a) == set(b)
    loose = level == 2
    return all(_word_in(w, b, loose) for w in a) or all(_word_in(w, a, loose) for w in b)


def _same_game(sides, teams, level=1):
    if len(sides) != 2 or len(teams) != 2:
        return False
    (a, b), (x, y) = sides, [t[0] for t in teams]
    return ((_same_team(a, x, level) and _same_team(b, y, level))
            or (_same_team(a, y, level) and _same_team(b, x, level)))


def _hits(match):
    """match(level) for the closest level that finds anything: the same name, then a shorter one, then a
    misspelled one."""
    return match(0) or match(1) or match(2)


class Guide:
    """Upcoming league games in Channels DVR's own Gracenote guide, read once per guide rebuild, and only when a
    lineup has an event channel to match (most users never cause a request). Team logos for the games it does not
    list come from ESPN's public team lists (Espn)."""

    def __init__(self, config, get_json=None, now=None, espn=None):
        self.base = ((config.get("channels_dvr") or {}).get("url") or "").strip().rstrip("/")
        self.get_json = get_json
        self.now = now or datetime.datetime.now(datetime.timezone.utc)
        self.espn = espn or Espn()
        self._games = None
        self._known = None

    def games(self):
        if self._games is None:
            self._games = self._fetch()
        return self._games

    def _fetch(self):
        if not self.base:
            return []
        from app.guide import gracenote
        get_json = self.get_json or gracenote._get_json
        games = []
        for series in sorted(set(SERIES.values())):
            try:
                data = get_json(f"{self.base}/dvr/guide/airings/{series}") or {}
            except Exception as exc:  # noqa: BLE001 - names alone still give a guide
                log.warning("Channels DVR guide not read (%s); event channels use their names only", exc)
                break
            for airing in data.get("airings") or []:
                game = _game(series, airing)
                if game:
                    games.append(game)
        return games

    def match(self, p):
        """The Gracenote game a parsed name is, or None. Precise rather than eager: the same teams at the time the
        name gives (a time with no day: at that time of day, in the coming days), one program only, the live one
        when there is a replay too. A name that says it is another broadcast (a language) keeps its own listing."""
        if len(p.get("sides") or []) != 2 or any(_LANGUAGE.search(t) for t in [p["event"]] + p["notes"]):
            return None
        series = SERIES.get(p["league"])
        hits = _hits(lambda level: [g for g in self.games()
                                    if not (series and g["series"] != series) and not g["alt"]
                                    and _same_game(p["sides"], g["teams"], level) and self._at_time(p, g["start"])])
        # One program can air several times (live, then reruns) and on several channels: keep its live airing, else
        # the one nearest the time the name gives.
        target = p.get("start") or self.now
        hits.sort(key=lambda g: (not g["live"], abs((g["start"] - target).total_seconds())))
        programs = {}
        for g in hits:
            programs.setdefault(g["program"], g)
        games = list(programs.values())
        live = [g for g in games if g["live"]]
        if live:
            return live[0] if len(live) == 1 else None    # two live games of these teams: not sure which
        if len(games) == 1 or (p["kind"] == "event" and games):
            return games[0]
        return None

    def _at_time(self, p, start):
        if p["kind"] == "event":
            return abs((start - p["start"]).total_seconds()) <= MATCH_HOURS * 3600
        now = self.now
        if not now - datetime.timedelta(hours=4) <= start <= now + datetime.timedelta(days=AHEAD_DAYS):
            return False
        zones = [ZONES[p["tz"]]] if p.get("tz") else US_ZONES
        want = p["clock"][0] * 60 + p["clock"][1]
        for zone in zones:
            local = start.astimezone(zoneinfo.ZoneInfo(zone))
            diff = abs(local.hour * 60 + local.minute - want)
            if min(diff, 1440 - diff) <= CLOCK_MINUTES:
                return True
        return False

    def team_ids(self, p):
        """Gracenote team ids for both sides of a game Gracenote does not list, from the teams its listed games of
        that league name; only when each side is exactly one team."""
        series = SERIES.get(p["league"])
        if not series or len(p.get("sides") or []) != 2:
            return []
        teams = self.teams(series)
        ids = []
        for side in p["sides"]:
            hits = _hits(lambda level: {tid for name, tid in teams if _same_team(side, name, level)})
            if len(hits) != 1:
                return []
            ids.append(hits.pop())
        return ids if ids[0] != ids[1] else []


    def teams(self, series):
        """(name, Gracenote team id) for a league: every team Gracenote has named in this guide's games, remembered on
        disk (cache/gracenote-teams.json) so a team stays known after its last game here has left the guide."""
        if self._known is None:
            self._known = self._remember()
        return {(name, tid) for name, tid in self._known.get(series, {}).items()}

    def _remember(self):
        import json
        import os
        from app import guide
        path = os.path.join(guide.cache_dir(), "gracenote-teams.json")
        try:
            with open(path, encoding="utf-8") as f:
                known = json.load(f)
            known = {k: dict(v) for k, v in known.items() if isinstance(v, dict)}
        except (OSError, ValueError, TypeError, AttributeError):
            known = {}
        changed = False
        for game in self.games():
            for name, tid in game["teams"]:
                if name and tid and known.setdefault(game["series"], {}).get(name) != tid:
                    known[game["series"]][name] = tid
                    changed = True
        if changed:
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path + ".tmp", "w", encoding="utf-8") as f:
                    json.dump(known, f, ensure_ascii=False, sort_keys=True)
                os.replace(path + ".tmp", path)
            except OSError as exc:
                log.warning("could not keep the Gracenote team list: %s", exc)
        return known


class Espn:
    """ESPN's public team list per league (site.api.espn.com), for a game's team logo. Read only when a game needs a
    logo, kept on disk for ESPN_DAYS (cache/espn-teams-<league>.json), the old copy used when ESPN does not answer."""

    def __init__(self, get_json=None):
        self.get_json = get_json
        self._teams = {}
        self._days = {}
        self._down = False

    def game(self, p):
        """ESPN's listing of a game no Gracenote guide has (most streaming and pay-per-view games), or None: the same
        two teams within MATCH_HOURS of the name's time, on exactly one scoreboard."""
        if p["kind"] != "event" or len(p.get("sides") or []) != 2 or any(
                _LANGUAGE.search(t) for t in [p["event"]] + p["notes"]):
            return None
        day = p["start"].astimezone(zoneinfo.ZoneInfo("America/New_York")).strftime("%Y%m%d")
        near = [g for g in self._day(day)
                if abs((g["start"] - p["start"]).total_seconds()) <= MATCH_HOURS * 3600]
        hits = _hits(lambda level: {g["id"]: g for g in near if _same_game(p["sides"], g["teams"], level)})
        return next(iter(hits.values())) if len(hits) == 1 else None

    def _day(self, day):
        if day in self._days or self._down:
            return self._days.get(day, [])
        get_json = self.get_json or _espn_json
        games = []
        for path in ESPN_BOARDS:
            try:
                data = get_json(ESPN_SCOREBOARD % (path, day)) or {}
            except Exception as exc:  # noqa: BLE001 - names alone still give a guide
                log.warning("ESPN scoreboards not read (%s); streaming events use their names only", exc)
                self._down = True
                break
            league = ((data.get("leagues") or [{}])[0].get("name")) or ""
            sport = path.split("/")[0]
            for event in data.get("events") or []:
                comp = (event.get("competitions") or [{}])[0]
                teams = [c.get("team") or {} for c in comp.get("competitors") or []]
                home = next((c.get("team") or {} for c in comp.get("competitors") or []
                             if c.get("homeAway") == "home"), {})
                try:
                    start = datetime.datetime.fromisoformat(event["date"].replace("Z", "+00:00"))
                except (KeyError, TypeError, ValueError):
                    continue
                venue = comp.get("venue") or {}
                place = ", ".join(x for x in [venue.get("fullName"), (venue.get("address") or {}).get("city"),
                                              (venue.get("address") or {}).get("state")] if x)
                games.append({"id": str(event.get("id")), "league": league, "name": event.get("name") or "",
                              "start": start, "teams": [(t.get("displayName") or "", "") for t in teams],
                              "logo": home.get("logo") or "", "place": place,
                              "genre": SPORTS.get(sport, sport.replace("-", " ").capitalize())})
        self._days[day] = games
        return games

    def logo(self, league, p):
        """The home team's logo for a game (the side after "at" or "@", else the first), else the league's."""
        sides = p.get("sides") or []
        if league in ESPN_PATHS and len(sides) == 2:
            home = sides[1] if re.search(r"\s(?:at|@)\s", p["event"], re.I) else sides[0]
            for side in (home, sides[0] if home is sides[1] else sides[1]):
                hits = _hits(lambda level: {t["logo"] for t in self.teams(league)
                                            if t["logo"] and self._is(side, t, level)})
                if len(hits) == 1:
                    return hits.pop()
        short = {"NFL", "MLB", "NBA", "NHL", "WNBA", "MLS"}
        return ESPN_LEAGUE_LOGO % league.lower() if league in short else ""

    @staticmethod
    def _is(side, team, level=1):
        if side.strip().upper() == team["abbreviation"] and len(side.strip()) >= 2:
            return True
        return _same_team(side, team["name"], level)

    def teams(self, league):
        path = ESPN_PATHS[league]
        if path not in self._teams:
            self._teams[path] = self._load(path)
        return self._teams[path]

    def _load(self, path):
        import json
        import os
        from app import guide
        cache = os.path.join(guide.cache_dir(), "espn-teams-%s.json" % path.replace("/", "-"))
        try:
            with open(cache, encoding="utf-8") as f:
                stored = json.load(f)
        except (OSError, ValueError):
            stored = None
        fresh = stored and _time_ok(stored.get("fetched_at"), ESPN_DAYS)
        if fresh:
            return stored["teams"]
        try:
            get_json = self.get_json or _espn_json
            data = get_json(ESPN_TEAMS % path) or {}
            teams = []
            for league in ((data.get("sports") or [{}])[0].get("leagues") or []):
                for item in league.get("teams") or []:
                    t = item.get("team") or {}
                    logo = next((lg.get("href") for lg in t.get("logos") or []
                                 if "default" in (lg.get("rel") or [])), "") or ""
                    if t.get("displayName"):
                        teams.append({"name": t["displayName"], "abbreviation": (t.get("abbreviation") or "").upper(),
                                      "logo": logo})
            if not teams:
                raise ValueError("no teams in the answer")
        except Exception as exc:  # noqa: BLE001 - a game without a logo is still a game
            log.warning("ESPN team list %s not read (%s)%s", path, exc, "; using the last copy" if stored else "")
            return (stored or {}).get("teams") or []
        try:
            os.makedirs(os.path.dirname(cache), exist_ok=True)
            tmp = cache + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"fetched_at": _time_now(), "teams": teams}, f)
            os.replace(tmp, cache)
        except OSError as exc:
            log.warning("could not keep the ESPN team list %s: %s", path, exc)
        return teams


def _time_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _time_ok(stamp, days):
    try:
        when = datetime.datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return False
    return datetime.datetime.now(datetime.timezone.utc) - when < datetime.timedelta(days=days)


def _espn_json(url, timeout=20):
    import json
    import urllib.request
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


def _game(series, a):
    """One Gracenote airing from /dvr/guide/airings, or None when it is not usable (coax's own XMLTV comes back
    as source "xmltv:coax" and is never matched)."""
    if a.get("Source") != "tms" or not a.get("ProgramID") or not isinstance(a.get("Time"), int):
        return None
    program = ((a.get("Raw") or {}).get("program") or {})
    teams = [(t.get("name") or "", str(t.get("teamBrandId") or "")) for t in program.get("teams") or []]
    if len(teams) != 2 and a.get("EventTitle"):
        teams = [(side, "") for side in _sides(a["EventTitle"])]
    start = datetime.datetime.fromtimestamp(a["Time"], datetime.timezone.utc)
    tags = a.get("Tags") or []
    return {"series": series, "program": a["ProgramID"], "title": a.get("Title") or "",
            "event": a.get("EventTitle") or "", "summary": a.get("FullSummary") or a.get("Summary") or "",
            "image": a.get("Image") or "", "start": start,
            "stop": start + datetime.timedelta(seconds=int(a.get("Duration") or 0) or 3 * 3600),
            "genres": list(a.get("Genres") or []), "categories": list(a.get("Categories") or []),
            "live": "Live" in tags, "new": "New" in tags, "teams": teams,
            "alt": bool(_ALT_CAST.search(a.get("EventTitle") or ""))}


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
    programs: covered is the set of channel ids that got some (None: unknown, count it as none). A lineup
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


def _program(cid, start, stop, title, desc="", logo=""):
    elem = ET.Element("programme", {"start": _stamp(start), "stop": _stamp(stop), "channel": f"coax-{cid}"})
    _text(elem, "title", title, lang="en")
    if desc:
        _text(elem, "desc", desc, lang="en")
    if logo:
        _text(elem, "icon", None, src=logo)
    return elem


def entries(config, lineup_id, now=None, covered=None, listed=(), guide=None):
    """XMLTV channel and program elements for the lineup's event channels: (channels, programs).
    covered as in lineup_events; listed holds channel ids that already have a channel element; guide is the
    rebuild's Guide (one for every lineup, so Channels DVR is asked once)."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    hour = now.astimezone(datetime.timezone.utc).replace(minute=0, second=0, microsecond=0)
    until = hour + datetime.timedelta(hours=FILLER_HOURS)
    zone = _zone(config)
    guide = guide or Guide(config, now=now)
    channels, programs = [], []
    for ch, p in lineup_events(config, lineup_id, now, covered):
        airing = None if p["kind"] == "idle" else _airing(p, guide.match(p), guide, zone)
        if p["kind"] == "undated" and airing is None:
            continue    # a time with no day that Gracenote does not place: no airing, as before
        cid, logo = ch["id"], ch.get("logo") or ""
        if cid not in listed:
            elem = ET.Element("channel", {"id": f"coax-{cid}"})
            _text(elem, "display-name", ch.get("name") or "")
            if logo:
                _text(elem, "icon", None, src=logo)
            channels.append(elem)
        nothing = "No event scheduled"
        if airing is None:
            programs.append(_program(cid, hour, until, nothing, logo=logo))
            continue
        start, stop = airing["start"], airing["stop"]
        if start > hour:
            programs.append(_program(cid, hour, start, "Up next: " + airing["name"],
                                     "Starts " + start.astimezone(zone).strftime("%a %b %-d, %-I:%M %p"), logo))
        elem = _program(cid, start, stop, airing["title"], logo=airing["image"] or logo)
        if airing["title"] != airing["name"]:
            _text(elem, "sub-title", airing["name"], lang="en")
        _text(elem, "desc", airing["desc"], lang="en")
        for cat in airing["categories"]:
            _text(elem, "category", cat, lang="en")
        _text(elem, "episode-num", "episode/" + airing["key"], system="coax")
        _text(elem, "date", start.astimezone(zone).strftime("%Y%m%d"))
        if airing["live"]:
            _text(elem, "live", None)
        if airing["new"]:
            _text(elem, "new", None)
        for tid in airing["teams"]:
            _text(elem, "team-id", tid, system="tms")
        programs.append(_order(elem))
        if stop < until:
            programs.append(_program(cid, max(stop, hour), until, nothing, logo=logo))
    return channels, programs


def _airing(p, game, guide, zone):
    """What the airing says: Gracenote's own listing when the guide has the game, else what the name gives."""
    notes = "".join(" (%s)" % n for n in p["notes"])
    if game is not None:
        # Gracenote's title, matchup, description, picture and times. No team ids here: the user's guide already
        # lists this game on a real channel, and a team pass would record it twice if Channels DVR does not take
        # the two for one program.
        name = game["event"] or p["event"]
        key = p["key"] if p["kind"] == "event" else _identity(p["league"], p["label"],
                                                              game["start"].astimezone(zone).date(), p["event"],
                                                              p["notes"])
        cats = list(dict.fromkeys(game["genres"] + game["categories"] + list(CATEGORIES)))
        return {"title": game["title"] or name, "name": name, "desc": (game["summary"] or name) + notes,
                "image": game["image"] or guide.espn.logo(p["league"], p), "start": game["start"],
                "stop": game["stop"], "categories": cats,
                "live": game["live"], "new": game["new"], "teams": [], "key": key}
    if p["kind"] != "event":
        return None
    espn = guide.espn.game(p) if not p["league"] else None
    if espn is not None:
        # A game only a streaming service carries, as ESPN's scoreboard lists it.
        desc = (espn["name"] + (". At " + espn["place"] if espn["place"] else "")) + notes
        return {"title": espn["league"] or espn["name"], "name": espn["name"], "desc": desc, "image": espn["logo"],
                "start": espn["start"], "stop": espn["start"] + datetime.timedelta(hours=GENERIC_HOURS),
                "categories": [espn["genre"]] + list(CATEGORIES), "live": True, "new": True, "teams": [],
                "key": p["key"]}
    title, genre, hours = LEAGUES.get(p["league"], (p["event"], "", GENERIC_HOURS))
    # A game only the provider carries: its team ids let a team pass record it.
    return {"title": title, "name": p["event"], "desc": p["event"] + notes,
            "image": guide.espn.logo(p["league"], p), "start": p["start"],
            "stop": p["start"] + datetime.timedelta(hours=hours),
            "categories": ([genre] if genre else []) + list(CATEGORIES), "live": True, "new": True,
            "teams": guide.team_ids(p), "key": p["key"]}


# Child order of a program element in the XMLTV DTD, which strict readers check.
_ORDER = ("title", "sub-title", "desc", "date", "category", "icon", "episode-num", "new", "live", "team-id")


def _order(elem):
    kids = sorted(list(elem), key=lambda k: _ORDER.index(k.tag) if k.tag in _ORDER else len(_ORDER))
    for kid in list(elem):
        elem.remove(kid)
    elem.extend(kids)
    return elem
