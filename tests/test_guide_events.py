"""Tests for app.guide.events: event channel names read into guide airings (no network)."""

import datetime
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
                     # a time with no day: providers keep a finished game's name, so it is not read as today
                     "NFL  | 02 - 1pm Ravens at Bills", "NHL 09: Phillies vs Marlins 7:05 PM",
                     # a window in no stated zone, whose stop is the provider's block
                     "MLB 04: Yankees vs Red Sox start: 2026 10 30 19:25:15 stop: 2026 10 31 02:38:35",
                     # more than a month away
                     "NFL 04: Bills vs Texans (09/13) 1:00PM ET"):
            self.assertIsNone(parse(name), name)

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

    def programmes(self, cfg=None):
        _channels, progs = events.entries(cfg or make_config(), "main", NOW)
        return [(p.get("channel"), p) for p in progs], _channels

    def test_airings_and_blocks(self):
        progs, chans = self.programmes()
        # CNN is not an event channel, channel 5 has a station: neither gets entries.
        self.assertEqual([c.get("id") for c in chans], ["coax-1", "coax-2", "coax-3"])
        one = [p for c, p in progs if c == "coax-1"]
        self.assertEqual([p.findtext("title") for p in one],
                         ["Up next: New Jersey Devils vs. Vegas Golden Knights", "NHL Hockey", "No event scheduled"])
        airing = one[1]
        self.assertEqual((airing.get("start"), airing.get("stop")), ("20261030220000 +0000", "20261031010000 +0000"))
        self.assertEqual(airing.findtext("sub-title"), "New Jersey Devils vs. Vegas Golden Knights")
        self.assertEqual([c.text for c in airing.findall("category")], ["Hockey", "Sports event", "Sports"])
        self.assertTrue(airing.findtext("episode-num").startswith("episode/"))
        self.assertIsNotNone(airing.find("live"))
        self.assertIsNotNone(airing.find("new"))
        self.assertEqual([k.tag for k in airing],
                         ["title", "sub-title", "desc", "date", "category", "category", "category", "icon",
                          "episode-num", "new", "live"])
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

    def test_xmltv_id_without_programmes_gets_entries(self):
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
        self.assertNotIn("coax-1", {p.get("channel") for p in progs})   # the provider's XMLTV has programmes

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
