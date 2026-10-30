"""Tests for app.guide.events: event channel names read into guide airings (no network)."""

import datetime
import json
import os
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zoneinfo
from unittest import mock

from app import guide
from app.guide import events, xmltv
from app.providers import catalog

NY = zoneinfo.ZoneInfo("America/New_York")
ESPN_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "guide", "espn-teams.json")


def fake_espn(fail=False):
    """ESPN's /teams answers for NHL and NFL, trimmed from the real ones."""
    with open(ESPN_FIXTURE, encoding="utf-8") as f:
        data = json.load(f)
    calls = []

    def get_json(url):
        calls.append(url)
        if fail:
            raise OSError("ESPN not reachable")
        path = url.split("/sports/", 1)[1].rsplit("/teams", 1)[0]
        return data.get(path, {"sports": [{"leagues": [{"teams": []}]}]})
    return get_json, calls


def setUpModule():
    # No test reaches ESPN or writes to /data: a fake answers and the cache goes to a temporary folder.
    global _TMP, _PATCHES
    _TMP = tempfile.TemporaryDirectory()
    _PATCHES = [mock.patch.object(events, "_espn_json", fake_espn()[0]),
                mock.patch.object(guide, "CACHE_DIR", _TMP.name),
                mock.patch.object(events, "clock", lambda: NOW.astimezone(datetime.timezone.utc))]
    for patcher in _PATCHES:
        patcher.start()


def tearDownModule():
    for patcher in _PATCHES:
        patcher.stop()
    _TMP.cleanup()
NOW = datetime.datetime(2026, 10, 30, 12, 0, tzinfo=NY)


def parse(name, now=NOW):
    return events.parse(name, NY, now)


class ParseTests(unittest.TestCase):
    """Names as providers write them: shapes seen on a real Xtream panel, and the formats epg3r's source comments
    describe (the team names are made up)."""

    def test_dated_event(self):
        p = parse("NHL 01: New Jersey Devils vs. Vegas Golden Knights @ 30 Oct 06:00 PM ET")
        self.assertEqual((p["kind"], p["label"], p["slot"], p["league"]), ("event", "NHL", "01", "NHL"))
        self.assertEqual(p["event"], "New Jersey Devils vs. Vegas Golden Knights")
        self.assertEqual(p["start"], datetime.datetime(2026, 10, 30, 18, 0, tzinfo=NY))

    def test_month_first_and_label_with_symbols(self):
        p = parse("ESPN+ 01: Brisbane Roar FC vs. Auckland FC @ Oct 30 4:55AM ET")
        self.assertEqual((p["label"], p["league"], p["event"]), ("ESPN+", "", "Brisbane Roar FC vs. Auckland FC"))
        self.assertEqual(p["start"], datetime.datetime(2026, 10, 30, 4, 55, tzinfo=NY))
        p = parse("DAZN US 01: Pitch 1A | Day 6 @ 30 Oct 01:00 AM ET")
        self.assertEqual((p["label"], p["event"]), ("DAZN US", "Pitch 1A | Day 6"))

    def test_zone_in_the_name_wins(self):
        p = parse("MLS 01: Inter Miami vs. LAFC @ Nov 1, 2026 5:00 PM PT")
        self.assertEqual(p["start"], datetime.datetime(2026, 11, 1, 20, 0, tzinfo=NY))

    def test_a_time_without_a_zone_is_not_read(self):
        for name in ("NBA 02: Knicks vs Nets 30/10 19:30", "US (Peacock 002) | Breeders` Cup: Day 1 (2026-10-30 13:00:00)",
                     "USA | NFL 04: Bills @ Texans | 10/31 07:00PM"):
            self.assertIsNone(parse(name), name)
        self.assertEqual(parse("NBA 02: Knicks vs Nets 30/10 19:30 ET")["start"],
                         datetime.datetime(2026, 10, 30, 19, 30, tzinfo=NY))

    def test_label_in_parentheses_and_label_last(self):
        p = parse("US (Peacock 002) | Breeders` Cup Keeneland: Day 1 (2026-10-30 13:00:00 ET)")
        self.assertEqual((p["label"], p["slot"], p["event"]), ("Peacock", "002", "Breeders` Cup Keeneland: Day 1"))
        p = parse("NEXT | TEMPLE VS. EAST CAROLINA | Fri 30 Oct 12:00 EDT (US) | 8K EXCLUSIVE | US: ESPN+ PPV 13")
        self.assertEqual((p["label"], p["slot"], p["event"]), ("ESPN+ PPV", "13", "TEMPLE VS. EAST CAROLINA"))
        self.assertEqual(p["start"], datetime.datetime(2026, 10, 30, 12, 0, tzinfo=NY))
        p = parse("End | Havre vs. PSG | all | 30-10-2026 | 18:40 (GMT) | 8K EXCLUSIVE | ES: SOCCER PPV 12")
        self.assertEqual((p["event"], p["start"]), ("Havre vs. PSG", datetime.datetime(2026, 10, 30, 14, 40, tzinfo=NY)))
        self.assertIsNone(parse("Live | Jack James vs. Alfred Stoddart | all | 8K EXCLUSIVE | ES: SOCCER PPV 3"))
        self.assertEqual(parse("LIVE EVENT 11 - NO EVENT")["kind"], "idle")
        for name in ("UK | Sky Sports | Main Event", "US| ESPN 2 | HD", "US (P+) AFC Champions League 1"):
            self.assertIsNone(parse(name), name)

    def test_feeds_of_one_event_share_one_listing(self):
        a = parse("NHL 02: Winnipeg Jets vs. Florida Panthers (Panthers Broadcast) @ 30 Oct 06:30 PM ET")
        b = parse("NHL 03: Winnipeg Jets vs. Florida Panthers (Jets Broadcast) @ 30 Oct 06:30 PM ET")
        c = parse("NHL 04: Florida Panthers at Winnipeg Jets @ 30 Oct 06:35 PM ET")   # sides swapped, time moved
        self.assertEqual(a["key"], b["key"])
        self.assertEqual(a["key"], c["key"])
        self.assertEqual(a["notes"], ["Panthers Broadcast"])

    def test_other_language_gender_or_game_is_another_event(self):
        base = parse("Peacock 04: Elversberg vs. Mainz @ 30 Oct 03:00 PM ET")["key"]
        self.assertNotEqual(parse("Peacock 03: Elversberg vs. Mainz (Español) @ 30 Oct 03:00 PM ET")["key"], base)
        w = parse("ESPN+ 08: ASUN Cross Country Championships (Women's Championship) @ Oct 30 10:25AM ET")
        m = parse("ESPN+ 09: ASUN Cross Country Championships (Men's Championship) @ Oct 30 10:25AM ET")
        self.assertNotEqual(w["key"], m["key"])
        g1 = parse("MLB 05: Red Sox at Yankees (Game 1) @ Oct 30 1:05 PM ET")
        g2 = parse("MLB 06: Red Sox at Yankees (Game 2) @ Oct 30 7:05 PM ET")
        self.assertNotEqual(g1["key"], g2["key"])

    def test_another_day_is_another_event(self):
        self.assertNotEqual(parse("NBA 01: Pelicans vs Kings @ 29 Oct 08:00 PM ET")["key"],
                            parse("NBA 01: Pelicans vs Kings @ 31 Oct 08:00 PM ET")["key"])

    def test_nothing_on(self):
        for name in ("NHL 06: NO EVENT", "NFL  | 01 -", "Peacock 08: NO EVENT", "NFL 05: (2098 12 31 08:00:17)",
                     "NBA 09: No Games Scheduled"):
            self.assertEqual(parse(name)["kind"], "idle", name)

    def test_names_that_get_nothing(self):
        for name in ("ESPN", "NHL Network HD", "NFL REDZONE SUNDAY", "FOX 5 Atlanta", "BBC One: London",
                     "NFL Teams: CBS Bears (WBBM) Chicago IL",
                     # a window in no stated zone, whose stop is the provider's block
                     "MLB 04: Yankees vs Red Sox start: 2026 10 30 19:25:15 stop: 2026 10 31 02:38:35",
                     # more than a month away
                     "NFL 04: Bills vs Texans (09/13) 1:00PM ET"):
            self.assertIsNone(parse(name), name)

    def test_a_time_with_no_day_is_only_a_time_of_day(self):
        # Providers keep a finished game's name, so it is never read as today; only Channels DVR's guide can place it.
        p = parse("NFL  | 02 - 1pm Ravens at Bills")
        self.assertEqual((p["kind"], p["sides"], p["clock"], p["tz"]), ("undated", ["Ravens", "Bills"], (13, 0), ""))
        self.assertNotIn("start", p)
        p = parse("NFL  | 13 - SNF 8:20pm Eagles at Commanders")
        self.assertEqual((p["sides"], p["clock"]), (["Eagles", "Commanders"], (20, 20)))
        self.assertEqual(parse("NFL   | 4k - SNF 8:20pm Eagles at Commanders")["slot"], "4k")
        p = parse("NHL 09: Phillies vs Marlins 7:05 PM ET")
        self.assertEqual((p["kind"], p["clock"], p["tz"]), ("undated", (19, 5), "ET"))
        self.assertIsNone(parse("LIVE EVENT 02 - 8am ACA 209"))       # no matchup to look up

    def test_epg3r_shapes(self):
        now = datetime.datetime(2026, 9, 7, 12, 0, tzinfo=NY)
        p = parse("NFL 04: Bills vs Texans (09/13) 1:00PM ET", now)
        self.assertEqual((p["event"], p["start"]), ("Bills vs Texans", datetime.datetime(2026, 9, 13, 13, 0, tzinfo=NY)))
        p = parse("USA | NFL 04: Bills @ Texans | 09/13 07:00PM ET", now)
        self.assertEqual((p["label"], p["event"]), ("NFL", "Bills @ Texans"))
        p = parse("NBA 05: Lakers vs Celtics (Sunday 09/13) 7:30PM ET", now)
        self.assertEqual(p["start"], datetime.datetime(2026, 9, 13, 19, 30, tzinfo=NY))
        p = parse("MLB 03: Yankees vs Red Sox (09.08 1:00 PM ET) (FOX)", now)
        self.assertEqual((p["event"], p["notes"]), ("Yankees vs Red Sox", ["FOX"]))
        self.assertEqual(p["key"], parse("MLB 05: Red Sox at Yankees @ Sep 8 1:05 PM ET", now)["key"])

    def test_league_is_the_whole_label(self):
        self.assertEqual(parse("USA NBA 03: Hawks vs Cavaliers @ 30 Oct 08:00 PM ET")["league"], "NBA")
        self.assertEqual(parse("MLS NEXT Pro 04: Austin FC II vs San Jose Earthquakes II @ 30 Oct 08:30 PM ET")
                         ["league"], "")

    def test_year_follows_today(self):
        p = parse("NHL 01: Rangers vs Bruins @ Jan 2 7:00 PM ET", datetime.datetime(2026, 12, 28, tzinfo=NY))
        self.assertEqual(p["start"].year, 2027)

    def test_leading_date_dropped(self):
        self.assertEqual(parse("ESPN+ 12: Fri, 10/30 - The Rich Eisen Show @ Oct 30 12:00PM ET")["event"],
                         "The Rich Eisen Show")


class GuideSourceTests(unittest.TestCase):
    def test_rules(self):
        station = {"mode": "gracenote", "station_id": "123"}
        hint = {"mode": "gracenote", "station_id": "", "xmltv_id": "NHL01.us"}   # a new channel's provider tvg-id
        xml = {"mode": "xmltv", "xmltv_id": "x.us"}
        none = {"mode": "none"}
        self.assertEqual(events.guide_source("per-channel", station), "gracenote")
        self.assertEqual(events.guide_source("per-channel", xml), "xmltv")
        self.assertEqual(events.guide_source("per-channel", hint), "")      # the hint is not a guide there
        self.assertEqual(events.guide_source("per-channel", none), "")
        self.assertEqual(events.guide_source("xmltv", station), "")         # an xmltv lineup sends no station
        self.assertEqual(events.guide_source("both", station), "gracenote")
        self.assertEqual(events.guide_source("both", hint), "xmltv")        # a both lineup sends the hint
        self.assertEqual(events.guide_source("both", none), "")


STREAMS = {
    ("p1", "1"): "NHL 01: New Jersey Devils vs. Vegas Golden Knights @ 30 Oct 06:00 PM ET",
    ("p1", "2"): "NHL 02: New Jersey Devils vs. Vegas Golden Knights (Devils Broadcast) @ 30 Oct 06:00 PM ET",
    ("p1", "3"): "NHL 06: NO EVENT",
    ("p1", "4"): "CNN",
    ("p1", "5"): "NHL 07: Rangers vs Bruins @ 30 Oct 07:00 PM ET",
}


def channel(cid, sid, guide=None, **extra):
    ch = {"id": cid, "uuid": f"uuid-{cid}", "number": str(cid), "name": f"Channel {cid}", "enabled": True,
          "logo": f"http://logo/{cid}.png", "streams": [{"provider": "p1", "stream_id": sid}],
          "guide": guide or {"mode": "gracenote", "station_id": "", "xmltv_id": ""}}
    ch.update(extra)
    return ch


def make_config():
    return {
        "server": {"base_url": "http://coax.test:7780"}, "timezone": "America/New_York",
        "providers": [{"id": "p1", "name": "One", "type": "xtream", "lines": []}],
        "channels": [channel(1, "1"), channel(2, "2"), channel(3, "3"), channel(4, "4"),
                     channel(5, "5", {"mode": "gracenote", "station_id": "999"})],
        "lineups": [{"id": "main", "name": "Main", "channels": [1, 2, 3, 4, 5]}],
    }


def fake_find(pid, sid):
    name = STREAMS.get((pid, str(sid)))
    return {"stream_id": sid, "name": name} if name else None


class EntriesTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(catalog, "find_stream", fake_find)
        patcher.start()
        self.addCleanup(patcher.stop)

    def programs(self, cfg=None):
        _channels, progs = events.entries(cfg or make_config(), "main", NOW)
        return [(p.get("channel"), p) for p in progs], _channels

    def test_airings_and_blocks(self):
        progs, chans = self.programs()
        # CNN is not an event channel, channel 5 has a station: neither gets entries.
        self.assertEqual([c.get("id") for c in chans], ["coax-1", "coax-2", "coax-3"])
        one = [p for c, p in progs if c == "coax-1"]
        self.assertEqual([p.findtext("title") for p in one],
                         ["Up next: New Jersey Devils vs. Vegas Golden Knights", "NHL Hockey", "No event scheduled"])
        airing = one[1]
        self.assertEqual((airing.get("start"), airing.get("stop")), ("20261030214500 +0000", "20261031010000 +0000"))
        self.assertEqual(airing.findtext("sub-title"), "New Jersey Devils vs. Vegas Golden Knights")
        self.assertEqual(one[0].findtext("desc"), "Starts Fri Oct 30, 6:00 PM")     # the game's time, not the padded start
        self.assertEqual(airing.findtext("video/quality"), "HDTV")
        self.assertEqual([c.text for c in airing.findall("category")], ["Hockey", "Sports event", "Sports"])
        self.assertTrue(airing.findtext("episode-num").startswith("episode/"))
        self.assertIsNotNone(airing.find("live"))
        self.assertIsNotNone(airing.find("new"))
        self.assertEqual([k.tag for k in airing],
                         ["title", "sub-title", "desc", "date", "category", "category", "category", "icon",
                          "episode-num", "video", "new", "live"])
        # The second feed of the same game has the same episode id: Channels DVR records it once.
        two = [p for c, p in progs if c == "coax-2"][1]
        self.assertEqual(two.findtext("episode-num"), airing.findtext("episode-num"))
        self.assertEqual(two.findtext("desc"), "New Jersey Devils vs. Vegas Golden Knights (Devils Broadcast)")
        idle = [p for c, p in progs if c == "coax-3"]
        self.assertEqual([p.findtext("title") for p in idle], ["No event scheduled"])
        self.assertIsNone(idle[0].find("episode-num"))
        self.assertEqual((idle[0].get("start"), idle[0].get("stop")), ("20261030160000 +0000", "20261031160000 +0000"))

    def test_disabled_stream_and_provider_off(self):
        cfg = make_config()
        cfg["channels"][0]["streams"] = [{"provider": "p1", "stream_id": "4", "disabled": True},
                                         {"provider": "p1", "stream_id": "1"}]
        self.assertEqual(events.lineup_events(cfg, "main", NOW)[0][1]["slot"], "01")
        cfg["providers"][0]["enabled"] = False
        self.assertEqual(events.lineup_events(cfg, "main", NOW), [])

    def test_xmltv_id_without_programs_gets_entries(self):
        from app import server
        cfg = make_config()
        cfg["lineups"][0]["guide"] = "both"
        # Channel 1 made the way coax makes a channel from a stream: Gracenote, the tvg-id kept as a hint.
        fields = server.channel_fields_from_stream("p1", {"stream_id": "1", "name": STREAMS[("p1", "1")],
                                                          "tvg_id": "BossSports.NHL.01"})
        cfg["channels"][0]["guide"] = fields["guide"]
        self.assertEqual(fields["guide"]["xmltv_id"], "BossSports.NHL.01")
        chans, progs = events.entries(cfg, "main", NOW, covered=set(), listed={1})
        self.assertIn("coax-1", {p.get("channel") for p in progs})
        self.assertNotIn("coax-1", {c.get("id") for c in chans})   # the provider's <channel> is already there
        _chans, progs = events.entries(cfg, "main", NOW, covered={1}, listed={1})
        self.assertNotIn("coax-1", {p.get("channel") for p in progs})   # the provider's XMLTV has programs

    def test_gracenote_lineup_has_no_entries(self):
        cfg = make_config()
        cfg["lineups"][0]["guide"] = "gracenote"
        self.assertEqual(events.entries(cfg, "main", NOW), ([], []))
        self.assertEqual(events.fingerprint(cfg, "main"), [])

    def test_fingerprint_follows_the_event_not_the_clock(self):
        cfg = make_config()
        first = events.fingerprint(cfg, "main")
        self.assertEqual(len(first), 3)
        self.assertEqual(first, events.fingerprint(cfg, "main"))
        with mock.patch.dict(STREAMS, {("p1", "3"): "NHL 06: Rangers vs Bruins @ 31 Oct 07:00 PM ET"}):
            self.assertNotEqual(events.fingerprint(cfg, "main"), first)


FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "guide", "cdvr-airings.json")


def fake_dvr(fail=False):
    """Channels DVR's /dvr/guide/airings/<series> from trimmed real answers (NFL, college football, NHL)."""
    with open(FIXTURE, encoding="utf-8") as f:
        data = json.load(f)
    calls = []

    def get_json(url):
        calls.append(url)
        if fail:
            raise OSError("Channels DVR not reachable")
        return data.get(url.rsplit("/", 1)[-1], {"airings": []})
    return get_json, calls


def guide_config():
    cfg = make_config()
    cfg["channels_dvr"] = {"url": "http://dvr.test:8089"}
    return cfg


class TeamNameTests(unittest.TestCase):
    def test_names(self):
        same = events._same_team
        self.assertTrue(same("Devils", "New Jersey Devils"))
        self.assertTrue(same("NY Jets", "New York Jets"))
        self.assertTrue(same("KC Chiefs", "Kansas City Chiefs"))
        self.assertFalse(same("Clevland Browns", "Cleveland Browns"))
        self.assertTrue(same("Clevland Browns", "Cleveland Browns", 2))
        self.assertFalse(same("Kansas", "Arkansas", 2))                    # too far apart even when loose

    def test_exact_first_then_misspelled(self):
        teams = {("Kansas", "1"), ("Kansas State", "2"), ("Cleveland Browns", "3"), ("Ohio", "4"), ("Ohio State", "5")}
        pick = lambda side: events._hits(lambda level: {t for n, t in teams if events._same_team(side, n, level)})  # noqa: E731
        self.assertEqual(pick("Clevland"), {"3"})
        self.assertEqual(pick("Kansas State"), {"2"})
        self.assertEqual(pick("Ohio"), {"4"})
        self.assertEqual(pick("Browns"), {"3"})


class GuideTests(unittest.TestCase):
    def setUp(self):
        self.get_json, self.calls = fake_dvr()
        self.guide = events.Guide(guide_config(), get_json=self.get_json, now=NOW)

    def test_dated_name_takes_gracenote_listing(self):
        g = self.guide.match(parse("NHL 01: New Jersey Devils vs. Vegas Golden Knights @ 30 Oct 06:00 PM ET"))
        self.assertEqual((g["program"], g["title"], g["event"]),
                         ("EP051892153426", "NHL Hockey", "New Jersey Devils at Vegas Golden Knights"))
        self.assertTrue(g["live"])
        self.assertEqual(g["start"], datetime.datetime(2026, 10, 30, 18, 0, tzinfo=NY))

    def test_live_airing_wins_over_reruns_and_an_older_replay(self):
        g = self.guide.match(parse("NCAAF 03: Baylor vs. UCF @ 30 Oct 07:30 PM ET"))
        self.assertEqual((g["program"], g["live"]), ("EP062700991086", True))
        self.assertEqual(g["start"], datetime.datetime(2026, 10, 30, 19, 30, tzinfo=NY))

    def test_time_with_no_day_is_placed_by_gracenote(self):
        g = self.guide.match(parse("NFL  | 02 - 1pm Ravens at Bills"))
        self.assertEqual((g["program"], g["start"]),
                         ("EP000031285787", datetime.datetime(2026, 11, 1, 13, 0, tzinfo=NY)))
        self.assertEqual(self.guide.match(parse("NFL  | 11 - 4:25pm Chiefs at Broncos"))["program"], "EP000031285796")

    def test_stale_or_wrong_time_gets_nothing(self):
        self.assertIsNone(self.guide.match(parse("NFL  | 03 - 1pm Titans at Bengals")))     # not in the guide
        self.assertIsNone(self.guide.match(parse("NFL  | 02 - 8:20pm Ravens at Bills")))   # not at that time
        self.assertIsNone(self.guide.match(parse("NHL 01: New Jersey Devils vs. Vegas Golden Knights @ 2 Nov 06:00 PM ET")))

    def test_alt_cast_is_never_the_game(self):
        g = self.guide.match(parse("NFL 05: Panthers vs. Packers @ 30 Oct 08:00 PM ET"))
        self.assertIsNone(g)    # only the Prime Vision alt cast airs then; the game itself is the next morning's rerun

    def test_another_language_keeps_its_own_listing(self):
        self.assertIsNone(self.guide.match(parse("NCAAF 09: Baylor vs. UCF (Español) @ 30 Oct 07:30 PM ET")))
        self.assertIsNone(self.guide.match(parse("Disney+ Events 185: En Español-Baylor vs. UCF @ 30 Oct 07:30 PM ET")))

    def test_other_labels_search_every_league(self):
        g = self.guide.match(parse("Disney+ Events 162: Tulane vs. Charlotte @ 30 Oct 07:00 PM ET"))
        self.assertEqual(g["program"], "EP062700991085")

    def test_team_ids_from_the_guide_for_a_game_it_does_not_list(self):
        p = parse("NFL 07: Bills vs. Jets @ 8 Nov 01:00 PM ET")
        self.assertIsNone(self.guide.match(p))
        self.assertEqual(self.guide.team_ids(p), ["34", "52"])
        self.assertEqual(self.guide.team_ids(parse("NFL 08: Bills vs. Titans @ 8 Nov 01:00 PM ET")), [])  # unknown side

    def test_misspelled_team_still_matches(self):
        g = self.guide.match(parse("NFL  | 02 - 1pm Ravens at Bils"))
        self.assertIsNone(g)                                    # "Bils" is too short to be read as misspelled
        g = self.guide.match(parse("NCAAF 02: Tulane vs. Charlote @ 30 Oct 07:00 PM ET"))
        self.assertEqual(g["program"], "EP062700991085")

    def test_teams_are_remembered_after_their_games_leave_the_guide(self):
        p = parse("NFL 07: Bills vs. Jets @ 8 Nov 01:00 PM ET")
        self.assertEqual(self.guide.team_ids(p), ["34", "52"])
        empty = events.Guide(guide_config(), get_json=lambda url: {"airings": []}, now=NOW)
        self.assertEqual(empty.team_ids(p), ["34", "52"])        # from cache/gracenote-teams.json

    def test_dvr_down_or_unset_means_names_only(self):
        get_json, calls = fake_dvr(fail=True)
        guide = events.Guide(guide_config(), get_json=get_json, now=NOW)
        self.assertIsNone(guide.match(parse("NFL  | 02 - 1pm Ravens at Bills")))
        self.assertEqual(len(calls), 1)          # gave up after the first failure, logged once
        guide.match(parse("NFL  | 06 - 1pm Raiders at Jets"))
        self.assertEqual(len(calls), 1)          # and is not asked again in the same rebuild
        unset = events.Guide(make_config(), get_json=self.get_json, now=NOW)
        self.assertIsNone(unset.match(parse("NFL  | 02 - 1pm Ravens at Bills")))

    def test_asked_once_and_only_when_there_is_a_game(self):
        self.guide.match(parse("NHL 01: New Jersey Devils vs. Vegas Golden Knights @ 30 Oct 06:00 PM ET"))
        self.guide.match(parse("NCAAF 03: Baylor vs. UCF @ 30 Oct 07:30 PM ET"))
        self.assertEqual(len(self.calls), len(set(events.SERIES.values())))
        get_json, calls = fake_dvr()
        guide = events.Guide(guide_config(), get_json=get_json, now=NOW)
        guide.match(parse("ESPN+ 06: The Golics @ Oct 30 10:00AM ET"))    # not a matchup
        self.assertEqual(calls, [])


class EspnTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(guide, "CACHE_DIR", self.tmp.name)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_home_team_logo_then_league(self):
        get_json, calls = fake_espn()
        espn = events.Espn(get_json=get_json)
        self.assertEqual(espn.logo("NHL", parse("NHL 02: Winnipeg Jets vs. Florida Panthers @ 30 Oct 06:30 PM ET")),
                         "https://a.espncdn.com/i/teamlogos/nhl/500/wpg.png")
        self.assertEqual(espn.logo("NHL", parse("NHL 05: Devils at Golden Knights @ 30 Oct 10:00 PM ET")),
                         "https://a.espncdn.com/i/teamlogos/nhl/500/vgk.png")      # "at": the home team
        self.assertEqual(espn.logo("NHL", parse("NHL 06: Kraken vs. Flames @ 30 Oct 10:00 PM ET")),
                         "https://a.espncdn.com/i/teamlogos/leagues/500/nhl.png")  # not in the list: the league's
        self.assertEqual(espn.logo("", parse("ESPN+ 13: Temple vs. East Carolina @ Oct 30 12:00PM ET")), "")
        self.assertEqual(len(calls), 1)                    # one list per league, once
        self.assertEqual(events.Espn(get_json=get_json).teams("NHL")[0]["name"], "Florida Panthers")
        self.assertEqual(len(calls), 1)                    # the next rebuild reads it from disk

    def test_a_side_that_is_two_teams_falls_back_to_the_other(self):
        get_json, _calls = fake_espn()
        espn = events.Espn(get_json=get_json)
        p = parse("NFL 04: New York vs. Buffalo @ 8 Nov 01:00 PM ET")     # New York is the Jets and the Giants
        self.assertEqual(espn.logo("NFL", p), "https://a.espncdn.com/i/teamlogos/nfl/500/buf.png")

    def test_espn_down_keeps_the_last_list(self):
        get_json, _calls = fake_espn()
        events.Espn(get_json=get_json).teams("NHL")
        path = os.path.join(self.tmp.name, "espn-teams-hockey-nhl.json")
        with open(path, encoding="utf-8") as f:
            stored = json.load(f)
        stored["fetched_at"] = "2020-01-01T00:00:00+00:00"            # stale: asks ESPN again
        with open(path, "w", encoding="utf-8") as f:
            json.dump(stored, f)
        down, calls = fake_espn(fail=True)
        self.assertEqual(len(events.Espn(get_json=down).teams("NHL")), 5)
        self.assertEqual(len(calls), 1)
        self.assertEqual(events.Espn(get_json=down).teams("NFL"), [])  # never read: no logos, no error


BOARDS = os.path.join(os.path.dirname(__file__), "fixtures", "guide", "espn-scoreboards.json")


def fake_boards(fail=False):
    """ESPN's scoreboards for Oct 30 2026, trimmed from the real ones (men's and women's college soccer, college
    volleyball, field hockey, A-League)."""
    with open(BOARDS, encoding="utf-8") as f:
        data = json.load(f)
    calls = []

    def get_json(url):
        calls.append(url)
        if fail:
            raise OSError("ESPN not reachable")
        path = url.split("/sports/", 1)[1].split("/scoreboard", 1)[0]
        return data.get(path, {"events": []}) if "dates=20261030" in url else {"events": []}
    return get_json, calls


class EspnScoreboardTests(unittest.TestCase):
    def setUp(self):
        self.get_json, self.calls = fake_boards()
        self.espn = events.Espn(get_json=self.get_json)

    def test_streaming_games_found(self):
        g = self.espn.game(parse("ESPN+ 110: Georgia Southern vs. Kentucky @ Oct 30 7:00PM ET"))
        self.assertEqual((g["league"], g["name"], g["genre"]), ("NCAA Men's Soccer", "Georgia Southern at Kentucky", "Soccer"))
        g = self.espn.game(parse("ESPN+ 93: Buffalo vs. Central Michigan @ Oct 30 6:30PM ET"))
        self.assertEqual(g["league"], "NCAA Women's Volleyball")
        early = datetime.datetime(2026, 10, 30, 3, 0, tzinfo=NY)
        with mock.patch.object(events, "clock", lambda: early):
            g = self.espn.game(parse("ESPN+ 01: Brisbane Roar FC vs. Auckland FC @ Oct 30 4:55AM ET", early))
        self.assertEqual((g["league"], g["start"]), ("Australian A-League Men",
                                                     datetime.datetime(2026, 10, 30, 9, 0, tzinfo=datetime.timezone.utc)))
        self.assertEqual(len(self.calls), len(events.ESPN_BOARDS))      # one day, asked once

    def test_same_school_other_sport_is_told_apart_by_time(self):
        self.assertEqual(self.espn.game(parse("ESPN+ 20: Tulsa vs. Temple @ Oct 30 7:00PM ET"))["league"],
                         "NCAA Women's Soccer")
        self.assertEqual(self.espn.game(parse("ESPN+ 21: Providence vs. Temple @ Oct 30 12:30PM ET"))["league"],
                         "NCAA Women's Field Hockey")

    def test_not_a_game_or_not_listed(self):
        self.assertIsNone(self.espn.game(parse("ESPN+ 06: The Golics @ Oct 30 10:00AM ET")))
        self.assertIsNone(self.espn.game(parse("ESPN+ 13: Temple vs. East Carolina @ Oct 30 12:00PM ET")))
        self.assertIsNone(self.espn.game(parse("ESPN+ 110: Georgia Southern vs. Kentucky @ Oct 30 1:00PM ET")))

    def test_only_games_about_now_are_looked_up(self):
        self.assertIsNone(self.espn.game(parse("ESPN+ 50: Georgia Southern vs. Kentucky @ Oct 18 7:00PM ET")))
        self.assertIsNone(self.espn.game(parse("ESPN+ 51: Georgia Southern vs. Kentucky @ Nov 20 7:00PM ET")))
        self.assertEqual(self.calls, [])                                 # last week's name, or far ahead: no requests

    def test_one_refused_board_is_skipped(self):
        import urllib.error

        def get_json(url):
            if "usa.ncaa.w.1" in url:
                raise urllib.error.HTTPError(url, 400, "Bad Request", {}, None)
            return self.get_json(url)
        espn = events.Espn(get_json=get_json)
        self.assertIsNone(espn.game(parse("ESPN+ 20: Tulsa vs. Temple @ Oct 30 7:00PM ET")))     # on the refused board
        self.assertEqual(espn.game(parse("ESPN+ 110: Georgia Southern vs. Kentucky @ Oct 30 7:00PM ET"))["league"],
                         "NCAA Men's Soccer")                            # the other boards still count

    def test_slow_espn_stops_at_the_deadline(self):
        ticks = iter(range(0, 10000, 30))
        with mock.patch.object(events.time, "monotonic", lambda: next(ticks)):
            espn = events.Espn(get_json=self.get_json)
            espn.game(parse("ESPN+ 110: Georgia Southern vs. Kentucky @ Oct 30 7:00PM ET"))
        self.assertLess(len(self.calls), len(events.ESPN_BOARDS))      # gave up after LOOKUP_SECONDS

    def test_espn_down(self):
        get_json, calls = fake_boards(fail=True)
        espn = events.Espn(get_json=get_json)
        self.assertIsNone(espn.game(parse("ESPN+ 110: Georgia Southern vs. Kentucky @ Oct 30 7:00PM ET")))
        espn.game(parse("ESPN+ 93: Buffalo vs. Central Michigan @ Oct 30 6:30PM ET"))
        self.assertEqual(len(calls), 1)                                  # gave up for the rest of the rebuild

    def test_entry(self):
        get_json, _calls = fake_boards()
        guide_obj = events.Guide(make_config(), now=NOW, espn=events.Espn(get_json=get_json))
        p = parse("ESPN+ 110: Georgia Southern vs. Kentucky @ Oct 30 7:00PM ET")
        a = events._airing(p, None, guide_obj, NY)
        self.assertEqual((a["title"], a["name"]), ("NCAA Men's Soccer", "Georgia Southern at Kentucky"))
        self.assertEqual(a["categories"][0], "Soccer")
        self.assertTrue(a["image"].startswith("https://a.espncdn.com/"))


class GuideEntriesTests(unittest.TestCase):
    def setUp(self):
        streams = {("p1", "1"): "NFL  | 02 - 1pm Ravens at Bills",
                   ("p1", "2"): "NFL  | 03 - 1pm Titans at Bengals",
                   ("p1", "3"): "NHL 02: New Jersey Devils vs. Vegas Golden Knights (Devils Broadcast) @ 30 Oct 06:00 PM ET",
                   ("p1", "4"): "NFL 07: Bills vs. Jets @ 8 Nov 01:00 PM ET"}
        patcher = mock.patch.object(catalog, "find_stream",
                                    lambda pid, sid: {"name": streams[(pid, sid)]} if (pid, sid) in streams else None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_entries(self):
        get_json, _calls = fake_dvr()
        cfg = guide_config()
        cfg["channels"] = cfg["channels"][:4]
        cfg["lineups"][0]["channels"] = [1, 2, 3, 4]
        chans, progs = events.entries(cfg, "main", NOW, guide=events.Guide(cfg, get_json=get_json, now=NOW))
        by = {}
        for prog in progs:
            by.setdefault(prog.get("channel"), []).append(prog)
        # The dateless name Gracenote does not place (last week's game) gets nothing at all.
        self.assertEqual(sorted(c.get("id") for c in chans), ["coax-1", "coax-3", "coax-4"])
        ravens = [p for p in by["coax-1"] if p.findtext("title") == "NFL Football"][0]
        self.assertEqual(ravens.findtext("sub-title"), "Baltimore Ravens at Buffalo Bills")
        self.assertEqual(ravens.get("start"), "20261101180000 +0000")
        self.assertTrue(ravens.find("icon").get("src").startswith("https://tmsimg.fancybits.co/"))
        self.assertEqual(ravens.findall("team-id"), [])          # the game is already on a real channel
        self.assertIsNotNone(ravens.find("live"))
        devils = [p for p in by["coax-3"] if p.findtext("title") == "NHL Hockey"][0]
        self.assertEqual(devils.findtext("sub-title"), "New Jersey Devils at Vegas Golden Knights")
        self.assertTrue(devils.findtext("desc").endswith("(Devils Broadcast)"))
        jets = [p for p in by["coax-4"] if p.findtext("title") == "NFL Football"][0]
        self.assertEqual(jets.findtext("sub-title"), "Bills vs. Jets")
        self.assertEqual(jets.find("icon").get("src"), "https://a.espncdn.com/i/teamlogos/nfl/500/buf.png")
        self.assertEqual([(t.get("system"), t.text) for t in jets.findall("team-id")], [("tms", "34"), ("tms", "52")])


class RefreshTests(unittest.TestCase):
    """The lineup's XMLTV file carries the event entries next to the provider's own."""

    def test_refresh_writes_event_entries(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with mock.patch.object(guide, "CACHE_DIR", tmp.name), mock.patch.object(catalog, "find_stream", fake_find), \
                mock.patch.object(xmltv, "provider_xmltv_url", lambda p: ""):
            summary = xmltv.refresh(make_config())
        self.assertTrue(summary["lineups"]["main"]["ok"], summary)
        root = ET.parse(os.path.join(tmp.name, "xmltv-main.xml")).getroot()
        self.assertEqual([c.get("id") for c in root.findall("channel")], ["coax-1", "coax-2", "coax-3"])
        titles = [p.findtext("title") for p in root.findall("programme[@channel='coax-1']")]
        self.assertIn("NHL Hockey", titles)


if __name__ == "__main__":
    unittest.main()
