"""Gracenote station lookup and matching through a Channels DVR server.

Channels DVR exposes Gracenote (TMS) data:
  GET {cdvr}/tms/stations/{query}             search stations by name or call sign
  GET {cdvr}/tms/lineups/{country}/{postal}   real TV lineups for a place
  GET {cdvr}/dvr/guide/stations/{lineup_id}   stations carried by one lineup

The matching rules are ported from reference/m3u_editor_stationid_matcher.py and
reference/dispatcharr_stationid_matcher.py and keep their strictness:
  * A candidate is only "matched" (safe for one-click apply) when it is exact and
    unique within the user's lineups: an explicit call sign such as "(WABC)" or
    "KTLA-DT", or a channel name equal to the Gracenote name or call sign after
    removing feed words like HD/East/Channel.
  * Stations found Gracenote-wide (outside the lineups for the chosen countries)
    are offered as suggestions but never marked "matched".
  * Everything else is "review" or "unmatched".

suggest() returns:
  {"status": "matched" | "review" | "unmatched",
   "reason": "...",
   "best": candidate | None,          set only when status is "matched"
   "candidates": [candidate, ...]}    ranked, best first
where a candidate is:
  {"station_id", "call_sign", "name", "type", "affiliate", "logo", "lineups": [...],
   "countries": [...], "outside": bool, "confidence": 0..1, "reason": "..."}
"""

import concurrent.futures
import difflib
import functools
import json
import logging
import os
import re
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

from app import guide

log = logging.getLogger("coax.guide.gracenote")

TIMEOUT = 20
INDEX_MAX_AGE = 7 * 24 * 3600   # rebuild cached lineup indexes weekly
GOOD_LOCAL = 0.8                # below this, also look Gracenote-wide
SUGGEST_THRESHOLD = 0.45

MATCHED, REVIEW, UNMATCHED, ERROR = "matched", "review", "unmatched", "error"


class GracenoteError(RuntimeError):
    """Channels DVR answered with an error or unusable data."""


class GracenoteUnreachable(GracenoteError):
    """The Channels DVR server could not be reached at all."""


# ----------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------

def _get_json(url, timeout=TIMEOUT):
    """GET a JSON document. Tests replace this function."""
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:200] if e.fp else ""
        raise GracenoteError(f"HTTP {e.code} from Channels DVR: {detail}".strip()) from None
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", e)
        raise GracenoteUnreachable(f"Channels DVR not reachable: {reason}") from None
    try:
        return json.loads(body) if body.strip() else None
    except ValueError:
        raise GracenoteError("Channels DVR returned something that is not JSON") from None


def _base(cdvr_url):
    return (cdvr_url or "").strip().rstrip("/")


def clean_query(term):
    """Gracenote answers HTTP 503 to "&" and to non-ASCII characters."""
    term = (term or "").replace("&", " and ")
    term = unicodedata.normalize("NFKD", term).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", term).strip()


def _station(raw, outside=False):
    """Normalize one station record from Channels DVR."""
    image = raw.get("preferredImage") or {}
    return {
        "station_id": str(raw.get("stationId") or ""),
        "call_sign": raw.get("callSign") or "",
        "name": raw.get("name") or "",
        "type": raw.get("type") or "",
        "affiliate": raw.get("affiliateCallSign") or "",
        "langs": [l for l in (raw.get("bcastLangs") or []) if isinstance(l, str)],
        "logo": (image.get("uri") or "") if isinstance(image, dict) else "",
        "lineups": [],
        "countries": [],
        "outside": outside,
    }


def search(cdvr_url, query):
    """Gracenote-wide station search. Returns normalized station dicts.

    Gracenote returns only about 10 stations per search, and a quoted phrase misses names that differ
    in punctuation ("Hip-Hop" vs "Hip Hop"), so the plain words, the quoted phrase and the run-together
    form ("GustoTV") are each searched and merged, plain words first."""
    term = clean_query(query)
    base = _base(cdvr_url)
    if not term or not base:
        return []
    forms = [term, f'"{term}"']
    compact = re.sub(r"[^A-Za-z0-9]", "", term)
    if " " in term and len(compact) >= 4 and len(term.split()) <= 3:
        forms.append(compact)
    seen, out, failed = set(), [], None
    for form in forms:
        try:
            result = _get_json(f"{base}/tms/stations/{urllib.parse.quote(form, safe='')}")
        except GracenoteUnreachable:
            raise
        except GracenoteError as e:      # one form failing does not lose the others
            failed = e
            continue
        for r in result if isinstance(result, list) else []:
            if isinstance(r, dict) and r.get("stationId") and str(r["stationId"]) not in seen:
                seen.add(str(r["stationId"]))
                out.append(_station(r, outside=True))
    if not out and failed is not None:
        raise failed
    return out


# ----------------------------------------------------------------------
# Lineup index (country / postal code restriction)
# ----------------------------------------------------------------------

def build_index(cdvr_url, country, postal):
    """All stations carried in the real lineups for one country and postal code."""
    base = _base(cdvr_url)
    try:
        lineups = _get_json(f"{base}/tms/lineups/{country}/{urllib.parse.quote(postal)}")
    except GracenoteUnreachable:
        raise
    except GracenoteError as e:
        if "1005" in str(e) or "invalid_lineup" in str(e):
            raise GracenoteError(f"Gracenote does not recognize postal code '{postal}' for "
                                 f"{country}. UK uses the outward code only, e.g. SW1A.") from None
        raise
    if not isinstance(lineups, list) or not lineups:
        raise GracenoteError(f"Gracenote returned no lineups for {country} {postal}. "
                             "Check the postal code (UK uses the outward code only, e.g. SW1A).")
    stations = {}
    for lineup in lineups:
        lineup_id = lineup.get("lineupId")
        if not lineup_id:
            continue
        label = f"{lineup.get('name') or lineup_id} {lineup.get('location') or ''}".strip()
        try:
            carried = _get_json(f"{base}/dvr/guide/stations/{urllib.parse.quote(lineup_id)}") or []
        except GracenoteUnreachable:
            raise   # server went away; stop instead of timing out on every lineup
        except GracenoteError as e:
            log.warning("Skipping lineup %s: %s", lineup_id, e)
            continue
        for raw in carried:
            if not isinstance(raw, dict) or not raw.get("stationId"):
                continue
            st = stations.setdefault(str(raw["stationId"]), _station(raw))
            if label not in st["lineups"]:
                st["lineups"].append(label)

    # Cable and satellite lineups carry no station names; look them up by call sign.
    calls = sorted({s["call_sign"] for s in stations.values() if s["call_sign"] and not s["name"]})

    def lookup(call):
        try:
            return search(base, call)
        except GracenoteUnreachable:
            raise
        except GracenoteError:
            return []

    with concurrent.futures.ThreadPoolExecutor(8) as pool:
        for results in pool.map(lookup, calls):
            for found in results:
                st = stations.get(found["station_id"])
                if st and not st["name"]:
                    st["name"] = found["name"]
                    st["type"] = st["type"] or found["type"]
                    st["logo"] = st["logo"] or found["logo"]

    # Subchannels carry diginets (MOVIES!, Catchy Comedy, Story) under the local call sign; the network
    # itself is only named by the affiliate call sign. Add those networks as stations of these lineups.
    nets = {}
    for st in stations.values():
        aff = (st.get("affiliate") or "").upper()
        if aff and aff != st["call_sign"].upper():
            nets.setdefault(aff, set()).update(st["lineups"])

    def network(aff):
        try:
            return aff, [r for r in search(base, aff) if r["call_sign"].upper() == aff]
        except GracenoteUnreachable:
            raise
        except GracenoteError:
            return aff, []

    with concurrent.futures.ThreadPoolExecutor(8) as pool:
        for aff, found in pool.map(network, sorted(nets)):
            for net in found:
                if net["type"] != "Digital Broadcast Network" or net["station_id"] in stations:
                    continue
                net = dict(net, outside=False, lineups=sorted(nets[aff]))
                stations[net["station_id"]] = net

    # An unknown postal code silently returns only nationwide satellite lineups.
    local = any((lu.get("type") or "").upper() in ("OTA", "CABLE", "VMVPD")
                and not (lu.get("lineupId") or "").endswith("-AFN-X")
                for lu in lineups)
    for st in stations.values():
        st["countries"] = [country]
    return {"country": country, "postal": postal, "built": time.time(),
            "lineups": len(lineups), "stations": stations, "local": local}


def _write_json_atomic(path, data):
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_index(cdvr_url, country, postal, cache_dir=None, rebuild=False):
    """Cached build_index(), stored as gracenote-index-<country>_<postal>.json."""
    country = (country or "").strip().upper()
    postal = (postal or "").strip()
    safe = re.sub(r"[^A-Za-z0-9]", "", f"{country}_{postal}")
    path = os.path.join(cache_dir or guide.cache_dir(), f"gracenote-index-{safe}.json")
    if not rebuild:
        try:
            with open(path, encoding="utf-8") as f:
                index = json.load(f)
            if time.time() - index.get("built", 0) < INDEX_MAX_AGE and "stations" in index:
                return index
        except (OSError, ValueError):
            pass
    index = build_index(cdvr_url, country, postal)
    try:
        _write_json_atomic(path, index)
    except OSError as e:
        log.warning("Could not cache the Gracenote lineup index: %s", e)
    return index


def lineup_index(cdvr_url, countries, cache_dir=None, rebuild=False):
    """Merged index for several (country, postal) pairs. Failing countries are reported, not fatal.

    Returns {"stations": {station_id: station}, "lineups": n, "problems": [str]}.
    """
    merged, problems, total = {}, [], 0
    for country, postal in countries or []:
        try:
            index = load_index(cdvr_url, country, postal, cache_dir, rebuild)
        except GracenoteUnreachable:
            raise
        except GracenoteError as e:
            log.warning("No lineups for %s %s: %s", country, postal, e)
            problems.append(f"{country} {postal}: {e}")
            continue
        total += index.get("lineups", 0)
        if not index.get("local", True):
            problems.append(f"{country} {postal}: only nationwide satellite lineups were found; "
                            "the postal code may be wrong.")
        for sid, st in index["stations"].items():
            have = merged.get(sid)
            if have is None:
                merged[sid] = dict(st, lineups=list(st["lineups"]),
                                   countries=list(st.get("countries") or [country]))
                continue
            have["lineups"] += [x for x in st["lineups"] if x not in have["lineups"]]
            for c in st.get("countries") or [country]:
                if c not in have["countries"]:
                    have["countries"].append(c)
            for key in ("name", "type", "call_sign", "logo"):
                have[key] = have[key] or st.get(key, "")
    if countries and not merged and problems:
        raise GracenoteError("Gracenote returned no lineups for any selected country: "
                             + "; ".join(problems))
    return {"stations": merged, "lineups": total, "problems": problems}


# ----------------------------------------------------------------------
# Name normalization and similarity (ported unchanged in behavior)
# ----------------------------------------------------------------------

# Words that describe a feed rather than the station.
DROP_WORDS = {
    "hd", "fhd", "uhd", "sd", "4k", "hevc", "h265", "raw", "backup", "alt",
    "the", "channel", "network", "tv", "east", "feed", "ip",
}
PROVIDER_PREFIX = re.compile(r"^\s*(?:\[[A-Za-z]{2,4}\]|[A-Za-z]{2,4}\s*[:|])\s*")
LONG_PREFIX = re.compile(r"^\s*[A-Z][A-Z0-9 ]{0,14}\|\s*")   # "NFL TEAMS|", "US|"
# US/Canada broadcast call signs, only when they look deliberate:
# "(WABC)", "KTLA-DT", or the whole name ("US| WNYC").
EXPLICIT_CALLSIGN = re.compile(
    r"\(([KWC][A-Z]{2,3})(?:-(?:DT\d*|TV|CD|LD))?\)"
    r"|\b([KWC][A-Z]{2,3})-(?:DT\d*|TV|CD|LD)\b"
    r"|^\s*(?:[A-Z]{2,4}\s*[:|]\s*)?([KW][A-Z]{2,3})\s*$"
)
# Curated equivalences, applied to both channel names and Gracenote names.
ALIASES = [
    (re.compile(r"\bnat ?geo\b"), "national geographic"),
    (re.compile(r"\bsho\b"), "showtime"),
    (re.compile(r"\bid\b"), "investigation discovery"),
    (re.compile(r"\blmn\b"), "lifetime movie network"),
    (re.compile(r"\bhln\b"), "headline news"),
    (re.compile(r"\bgsn\b"), "game show network"),
    (re.compile(r"\bnyc\b"), "new york"),
]
GENERIC = {"news", "sports", "sport", "movies", "movie", "plus", "pacific", "central", "live",
           "music", "kids", "family", "national", "television", "america", "american"}
JUNK = re.compile(r"\b(?:test|dummy|catalog|vod|on demand)\b", re.I)


@functools.lru_cache(maxsize=20000)
def tokens(text):
    """Order-independent normalized word set for a channel or station name."""
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    text = PROVIDER_PREFIX.sub("", LONG_PREFIX.sub("", text)).lower()
    text = text.replace("&", " and ").replace("+", " plus ").replace("'", "")
    text = re.sub(r"(?<=[a-z0-9])-(?=[a-z0-9])", "", text)        # c-span -> cspan
    for pattern, replacement in ALIASES:
        text = pattern.sub(replacement, text)
    words = []
    for t in re.sub(r"[^a-z0-9]+", " ", text).split():
        if t.isdigit() and len(t) <= 2 and words and words[-1].isalpha() \
                and words[-1] not in DROP_WORDS:
            words[-1] += t                                       # qvc 2 -> qvc2
        else:
            words.append(t)
    out = []
    for t in words:
        t = "pacific" if t == "west" else t                     # West feeds are Pacific feeds
        if t not in DROP_WORDS and t not in out:
            out.append(t)
    return frozenset(out)


@functools.lru_cache(maxsize=200000)
def _token_similarity(a, b):
    if a == b:
        return 1.0
    if min(len(a), len(b)) >= 3 and (a.startswith(b) or b.startswith(a)):
        return 0.85
    if min(len(a), len(b)) >= 4 and a[0] == b[0] and abs(len(a) - len(b)) <= 3:
        ratio = difflib.SequenceMatcher(None, a, b).ratio()
        return ratio if ratio >= 0.8 else 0.0
    return 0.0


def similarity(a, b, call=""):
    """0..1, ignores word order. `call` (a call sign) may stand in for any word of `a`."""
    if not a or not (b or call):
        return 0.0
    b_plus = set(b) | ({call} if call else set())
    score_a = sum(max(_token_similarity(x, y) for y in b_plus) for x in a)
    if not b:
        return score_a / len(a) * 0.9
    score_b = sum(max(_token_similarity(y, x) for x in a) for y in b)
    return (score_a + score_b) / (len(a) + len(b))


def bare_call(station):
    """Call sign without a trailing HD, lowercase."""
    return re.sub(r"hd$", "", (station.get("call_sign") or "").lower())


def search_phrase(name):
    """Human search phrase for a channel name: no provider prefix, tags or HD/RAW noise."""
    t = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()
    t = PROVIDER_PREFIX.sub("", LONG_PREFIX.sub("", t))
    t = re.sub(r"\([^)]*\)", " ", t)
    t = re.sub(r"\b(?:HD|FHD|UHD|SD|4K|RAW|HEVC|EAST)\b", " ", t, flags=re.I)
    return re.sub(r"\s+", " ", t).strip(" -:|")


def _compact_variants(text):
    t = re.sub(r"[^a-z0-9]", "", search_phrase(text).lower().replace("&", "and"))
    return {t, t[3:] if t.startswith("the") and len(t) > 6 else t}


US_LANGS = {"en", "en-US"}


def language_weight(station):
    """1 for US English (or unknown), 0.85 for other English feeds (UK, Australia, Canada), 0.6 otherwise.

    Only a tiebreak between equally good name matches, after stations in your own lineups: it never lowers a
    match's confidence or drops a station, so an exact-name Telemundo or a UK user's own feeds stay exact."""
    langs = station.get("langs") or []
    if not langs or all(l in US_LANGS for l in langs):
        return 1.0
    if any(l in US_LANGS for l in langs):
        return 0.9    # US English plus another variant, e.g. a UK streaming feed
    if any(l.lower().startswith("en") for l in langs):
        return 0.85
    return 0.6


def foreign_label(station):
    langs = [l for l in station.get("langs") or [] if l not in US_LANGS]
    if not langs:
        return ""
    return {"en-GB": "UK feed", "en-AU": "Australian feed", "en-CA": "Canadian feed",
            "fr-CA": "French Canadian feed"}.get(langs[0], "%s-language feed" % langs[0])


def _brand(channel_name):
    """The channel's leading brand word ("spectrum" in "Spectrum News 1 Hudson Valley"), if distinctive."""
    for w in re.findall(r"[a-z0-9]+", search_phrase(channel_name).lower().replace("&", " ")):
        if w in DROP_WORDS:
            continue
        return w if len(w) >= 3 and w.isalpha() and w not in GENERIC else ""
    return ""


@functools.lru_cache(maxsize=200000)
def name_score(channel_name, station_name, call=""):
    score = _name_score(channel_name, station_name, call)
    # A station missing the channel's brand is a different channel: Spectrum News Hudson Valley is not
    # News 12 Hudson Valley, NBC New York News is not SportsNet New York.
    brand = _brand(channel_name)
    if brand and score < 0.95:
        have = set(tokens(station_name)) | ({call} if call else set())
        compact = re.sub(r"[^a-z0-9]", "", (station_name or "").lower())
        if not any(_token_similarity(brand, t) >= 0.8 for t in have) and brand not in compact:
            score *= 0.6
    return score


def _name_score(channel_name, station_name, call=""):
    """Order-independent similarity, plus run-together names (SHOXBET = SHO x BET)."""
    score = similarity(tokens(channel_name), tokens(station_name), call)
    a, b = _compact_variants(channel_name), _compact_variants(station_name)
    best = max((difflib.SequenceMatcher(None, x, y).ratio()
                for x in a for y in b if min(len(x), len(y)) >= 5), default=0.0)
    if best >= 0.88:
        score = max(score, best * 0.95)
    return score


def wants_hd(text):
    return bool(re.search(r"\b(?:HD|FHD|UHD|4K)\b", text or "", re.I))


def is_hd_station(station):
    return bool(re.search(r"\bHD\b", station["name"])) \
        or station["call_sign"].upper().endswith("HD")


def explicit_callsign(text):
    match = EXPLICIT_CALLSIGN.search((text or "").upper())
    return next((g for g in match.groups() if g), None) if match else None


# ----------------------------------------------------------------------
# Matcher over the user's lineup index
# ----------------------------------------------------------------------

class Matcher:
    def __init__(self, stations):
        """`stations`: {station_id: station} from lineup_index(), or a list."""
        if isinstance(stations, dict):
            self.by_id = stations
            self.stations = list(stations.values())
        else:
            self.stations = list(stations or [])
            self.by_id = {s["station_id"]: s for s in self.stations}
        self.by_call, self.by_name, self.by_bare_call = {}, {}, {}
        for st in self.stations:
            call = st["call_sign"].upper()
            self.by_call.setdefault(call, []).append(st)
            bare = re.sub(r"HD$", "", call.lower())
            if len(bare) >= 3:
                self.by_bare_call.setdefault(bare, []).append(st)
            key = tokens(st["name"])
            if key:
                self.by_name.setdefault(key, []).append(st)

    @staticmethod
    def _rank(cands):
        unique = {s["station_id"]: s for s in cands}
        return sorted(unique.values(), key=lambda s: (-len(s["lineups"]), s["name"]))

    @staticmethod
    def _resolve(cands, channel_name):
        """Return the single right station, or None if it is ambiguous."""
        cands = list({s["station_id"]: s for s in cands}.values())
        if len(cands) == 1:
            return cands[0]
        # Same network in SD and HD: take the one matching the channel's HD-ness.
        preferred = [s for s in cands if is_hd_station(s) == wants_hd(channel_name)]
        if len(preferred) == 1:
            return preferred[0]
        return None

    def match(self, name, tvg_id=""):
        """(status, best station or None, candidate stations, reason)."""
        call = explicit_callsign(name) or explicit_callsign(tvg_id)
        if call:
            exact = self.by_call.get(call, [])
            if len(exact) == 1:
                return MATCHED, exact[0], [exact[0]], f"call sign {call}"
        key = tokens(name)
        cands = list(self.by_name.get(key, [])) if key else []
        if key and len(key) == 1:
            token = next(iter(key))
            by_call = self.by_bare_call.get(token, [])
            # "CNN" -> Cable News Network is safe; "Retro" -> RetroPlex
            # (call sign RETRO) is not, so that one goes to review.
            cands += [st for st in by_call if self._call_sign_is_name(st, key)]
            if not cands and by_call:
                return REVIEW, None, self._rank(by_call), "call sign matches but the name differs"
        if not cands and call:
            cands = [s for s in self.stations if s["call_sign"].upper().startswith(call)]
        if cands:
            best = self._resolve(cands, name)
            if best:
                return MATCHED, best, self._rank(cands), "exact name"
            return REVIEW, None, self._rank(cands), "several stations share this name"
        loose = self.suggest(name)
        if loose:
            return REVIEW, None, loose, "similar names only"
        return UNMATCHED, None, [], "nothing similar in your lineups"

    @staticmethod
    def _call_sign_is_name(st, key):
        """True when a call sign really is the station's name or acronym."""
        if not st["name"] or tokens(st["name"]) == key:
            return True
        words = re.findall(r"[A-Za-z0-9]+", st["name"].replace("&", " "))
        initials = "".join(w[0] for w in words).lower()
        call = re.sub(r"hd$", "", st["call_sign"].lower())
        # Acronyms: CNN = Cable News Network, HGTV starts with H-G-T.
        return initials == call or (len(initials) >= 3 and call.startswith(initials))

    def suggest(self, text, limit=15, threshold=SUGGEST_THRESHOLD):
        """Ranked loose suggestions for a channel name, ignoring word order."""
        if not tokens(text):
            return []
        scored = []
        for st in self.stations:
            score = name_score(text, st["name"], bare_call(st))
            if score >= threshold:
                scored.append((score, st))
        scored.sort(key=lambda x: (-x[0], -len(x[1]["lineups"]), x[1]["name"]))
        return [st for _, st in scored[:limit]]

    def search(self, text, limit=40):
        """Stations whose name or call sign contains every word of text."""
        want = tokens(text)
        if not want:
            return []
        hits = []
        for st in self.stations:
            hay = " ".join(tokens(st["name"])) + " " + st["call_sign"].lower()
            if all(w in hay for w in want):
                hits.append(st)
        return self._rank(hits)[:limit]


def global_candidates(cdvr_url, name, matcher, limit=8, search_fn=None):
    """Gracenote-wide candidates for a channel, outside the user's lineups (never auto-picked)."""
    search_fn = search_fn or search
    want = tokens(name)
    phrase = search_phrase(name)
    queries = [phrase] if phrase else []
    distinct = sorted((t for t in want if len(t) >= 4 and t not in GENERIC and not t.isdigit()),
                      key=len, reverse=True)
    found = {}
    for query in queries + distinct[:2]:
        if query in queries[1:] or (found and query != phrase
                                    and max(v[0] for v in found.values()) >= 0.8):
            break
        try:
            results = search_fn(cdvr_url, query)
        except GracenoteUnreachable:
            raise
        except GracenoteError as e:
            log.debug("Gracenote search %r failed: %s", query, e)
            continue
        for result in results:
            sid = result["station_id"]
            if not sid or result["type"] == "VODProvider" or JUNK.search(result["name"]):
                continue
            st = matcher.by_id.get(sid) or result
            score = name_score(name, st["name"], bare_call(st))
            shares = bool(set(distinct) & tokens(st["name"]))
            # "CNN" -> Cable News Network (call sign CNN): the call sign is the name's acronym.
            acronym = len(want) == 1 and bare_call(st) in want \
                and Matcher._call_sign_is_name(st, want)
            if score >= SUGGEST_THRESHOLD or shares or acronym:
                previous = found.get(sid)
                if not previous or score > previous[0]:
                    found[sid] = (score, st)
    ranked = sorted(found.values(), key=lambda x: (-round(x[0], 2), -language_weight(x[1]), x[1]["name"]))
    return [st for _, st in ranked[:limit]]


def merge_candidates(name, *lists, limit=15):
    """One ranked list: best similarity first, stations in your lineups before outside ones, then US English."""
    seen = {}
    for stations in lists:
        for st in stations or []:
            seen.setdefault(st["station_id"], st)
    ranked = sorted(seen.values(), key=lambda st: (
        -round(name_score(name, st["name"], bare_call(st)), 2),
        bool(st.get("outside")), -language_weight(st), -len(st.get("lineups", [])), st["name"]))
    return ranked[:limit]


# ----------------------------------------------------------------------
# Public suggestion API
# ----------------------------------------------------------------------

def _candidate(channel_name, st, call, best_id):
    score = name_score(channel_name, st["name"], bare_call(st))
    station_call = st["call_sign"].upper()
    if call and call in (station_call, re.sub(r"HD$", "", station_call)):
        reason, score = f"call sign {call}", max(score, 0.95 if st.get("outside") else 1.0)
    elif tokens(channel_name) and tokens(channel_name) == tokens(st["name"]):
        reason, score = "exact name", max(score, 0.95)
    elif tokens(channel_name) == frozenset([bare_call(st)]) and bare_call(st):
        reason = "call sign matches the name"
        score = max(score, 0.9 if Matcher._call_sign_is_name(st, tokens(channel_name)) else 0.6)
    else:
        reason = "similar name"
    if st["station_id"] == best_id:
        score = 1.0
    if foreign_label(st):
        reason += ", " + foreign_label(st)
    if st.get("outside"):
        reason += ", outside your lineups"
    out = dict(st, lineups=list(st.get("lineups") or []), countries=list(st.get("countries") or []))
    out["outside"] = bool(st.get("outside"))
    out["confidence"] = round(min(score, 1.0), 2)
    out["reason"] = reason
    return out


def suggest(channel_name, cdvr_url="", tvg_id="", index=None, countries=None,
            limit=10, wide=False, cache_dir=None, search_fn=None):
    """Ranked Gracenote candidates for one channel.

    index:     result of lineup_index(); restricts "matched" to the user's lineups.
    countries: [(country, postal), ...] used to build or load the index when
               `index` is not given.
    wide:      also search Gracenote-wide even when local matches are good.
    Without an index or countries, only Gracenote-wide results are possible and the
    status is at best "review".
    """
    if index is None and countries:
        index = lineup_index(cdvr_url, countries, cache_dir=cache_dir)
    matcher = index if isinstance(index, Matcher) else Matcher((index or {}).get("stations") or {})
    return _suggest_with(matcher, channel_name, cdvr_url, tvg_id, limit, wide, search_fn)


def _suggest_with(matcher, channel_name, cdvr_url, tvg_id, limit, wide, search_fn):
    channel_name = channel_name or ""
    call = explicit_callsign(channel_name) or explicit_callsign(tvg_id)
    if matcher.stations:
        status, best, cands, reason = matcher.match(channel_name, tvg_id)
    else:
        status, best, cands, reason = UNMATCHED, None, [], "no lineups selected"

    local = merge_candidates(channel_name, cands, matcher.search(channel_name),
                             matcher.suggest(channel_name), limit=30)
    top = name_score(channel_name, local[0]["name"], bare_call(local[0])) if local else 0.0
    if cdvr_url and (wide or status != MATCHED and top < GOOD_LOCAL):
        outside = global_candidates(cdvr_url, channel_name, matcher, limit=25, search_fn=search_fn)
        local = merge_candidates(channel_name, local, outside, limit=30)
        if status == UNMATCHED and outside:
            status, reason = REVIEW, "only found outside your lineups"

    # The matched station always leads the list.
    if best is not None:
        local = [best] + [s for s in local if s["station_id"] != best["station_id"]]
    best_id = best["station_id"] if best is not None else None
    candidates = [_candidate(channel_name, st, call, best_id) for st in local]
    # Order by the confidence shown to the user (stable, so ties keep the lineup ranking).
    candidates.sort(key=lambda c: -c["confidence"])
    candidates = candidates[:limit]
    if status == UNMATCHED and candidates:
        status, reason = REVIEW, "similar names only"
    if not candidates and status != UNMATCHED:
        status, reason = UNMATCHED, "nothing similar found"
    return {
        "status": status,
        "reason": reason,
        "best": candidates[0] if best is not None and candidates else None,
        "candidates": candidates,
    }


def bulk_suggest(channels, cdvr_url="", index=None, countries=None, limit=5,
                 cache_dir=None, search_fn=None, progress=None):
    """Suggestions for many channels at once.

    channels: config channel dicts (uses "id", "name", optional "tvg_id").
    Returns [{"channel_id": id, "name": name, **suggest result}, ...] in input order.
    Gracenote-wide searches are cached per query, and if Channels DVR becomes
    unreachable, the rest continue with lineup matching only.
    """
    if index is None and countries:
        index = lineup_index(cdvr_url, countries, cache_dir=cache_dir)
    matcher = Matcher((index or {}).get("stations") or {})
    base_search = search_fn or search
    query_cache = {}

    def cached_search(url, query):
        key = query.lower()
        if key not in query_cache:
            query_cache[key] = base_search(url, query)
        return query_cache[key]

    results, url = [], cdvr_url
    for channel in channels or []:
        name = channel.get("name") or ""
        try:
            result = _suggest_with(matcher, name, url, channel.get("tvg_id") or "", limit,
                                   False, cached_search)
        except GracenoteUnreachable as e:
            log.warning("Channels DVR unreachable during bulk suggest: %s", e)
            url = ""   # stop trying the server; lineup matching still works
            result = _suggest_with(matcher, name, url, channel.get("tvg_id") or "", limit,
                                   False, cached_search)
        except Exception as e:   # one bad channel never stops the batch
            log.exception("Suggest failed for channel %r", name)
            result = {"status": ERROR, "reason": str(e), "best": None, "candidates": []}
        results.append(dict(result, channel_id=channel.get("id"), name=name))
        if progress:
            try:
                progress(len(results))
            except Exception:  # noqa: BLE001
                pass
    return results


# ----------------------------------------------------------------------
# Deep search (opt-in fallback)
# ----------------------------------------------------------------------

# Spelling variants Gracenote names use; each side is tried for the other.
SWAPS = [("new york", "nyc"), ("+", " plus"), (" plus", "+"), ("&", " and "), (" and ", " & "),
         ("television", "tv"), (" tv", ""), ("network", ""), ("channel", ""), ("east", ""), ("hd", "")]
DEEP_MAX_QUERIES = 24


def deep_queries(text):
    """Every reasonable way Gracenote might spell a channel, best first, at most DEEP_MAX_QUERIES.

    Gracenote search only returns stations whose names contain all the query words (about 10 at most, in a
    fixed order), so a missing or extra word hides the station. This tries the name, its spelling variants,
    the run-together form, word pairs and single distinctive words."""
    base = clean_query(search_phrase(text)) or clean_query(text)
    out = []

    def add(q):
        q = re.sub(r"\s+", " ", q).strip(" -:|")
        if len(q) >= 2 and q.lower() not in (x.lower() for x in out):
            out.append(q)

    add(base)
    low = base.lower()
    split = re.sub(r"(?<=[a-z])(?=\d)|(?<=\d)(?=[a-z])", " ", low)      # News12 -> News 12
    for form in (low, split):
        add(form)
        for a, b in SWAPS:                                                 # NYC for New York, + for Plus ...
            if a in form:
                add(re.sub(re.escape(a), b, form))
    add(re.sub(r"[^a-z0-9]", "", low))                                     # GustoTV
    # Multi-word places count as one word, so "new york" is never split into "new" and "york".
    words = [w.replace("_", " ") for w in re.findall(r"[a-z0-9+_]+", re.sub(r"\bnew york\b", "new_york", split))
             if w not in DROP_WORDS]

    def useful(ws):
        return any(len(w) >= 4 and w not in GENERIC and not w.isdigit() for w in ws)

    if len(words) > 1:
        for i in range(len(words)):                                        # drop one word at a time
            rest = words[:i] + words[i + 1:]
            if useful(rest):
                add(" ".join(rest))
    for w in sorted(words, key=len, reverse=True):                         # single distinctive words
        if useful([w]) and " " not in w:
            add(w)
    return out[:DEEP_MAX_QUERIES]


def deep_search(cdvr_url, text, limit=40, search_fn=None, workers=6):
    """Fuzzy fallback for a channel that normal matching cannot find: run every deep_queries() form, merge,
    and rank by name similarity times language (US English first). Slower: up to DEEP_MAX_QUERIES requests."""
    search_fn = search_fn or search
    queries = deep_queries(text)

    def one(q):
        try:
            return search_fn(cdvr_url, q)
        except GracenoteUnreachable:
            raise
        except GracenoteError:
            return []

    found = {}
    with concurrent.futures.ThreadPoolExecutor(workers) as pool:
        for results in pool.map(one, queries):
            for st in results:
                if st["station_id"] and st["type"] != "VODProvider" and not JUNK.search(st["name"]):
                    found.setdefault(st["station_id"], st)
    scored = []
    for st in found.values():
        score = name_score(text, st["name"], bare_call(st))
        if score >= 0.3:
            scored.append((score, st))
    scored.sort(key=lambda x: (-round(x[0], 2), -language_weight(x[1]), x[1]["name"]))
    out = []
    for score, st in scored[:limit]:
        reason = "deep search"
        if foreign_label(st):
            reason += ", " + foreign_label(st)
        out.append(dict(st, confidence=round(min(score, 1.0), 2), reason=reason))
    return out
