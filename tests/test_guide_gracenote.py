"""Tests for app.guide.gracenote using a fake Channels DVR (no network)."""

import json
import os
import tempfile
import unittest
import urllib.parse
from unittest import mock

from app import guide
from app.guide import gracenote

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "guide")


class FakeChannelsDvr:
    """Answers the three Channels DVR endpoints from tests/fixtures/guide/cdvr.json."""

    def __init__(self):
        with open(os.path.join(FIXTURES, "cdvr.json"), encoding="utf-8") as f:
            self.data = json.load(f)
        self.urls = []

    def __call__(self, url, timeout=None):
        self.urls.append(url)
        path = urllib.parse.urlsplit(url).path
        if path.startswith("/tms/stations/"):
            term = urllib.parse.unquote(path[len("/tms/stations/"):]).strip('"')
            return self.data["search"].get(term, [])
        if path.startswith("/tms/lineups/USA/"):
            return self.data["lineups"]
        if path.startswith("/tms/lineups/"):
            raise gracenote.GracenoteError("HTTP 400 from Channels DVR: invalid_lineup 1005")
        if path.startswith("/dvr/guide/stations/"):
            lineup = urllib.parse.unquote(path[len("/dvr/guide/stations/"):])
            return self.data["stations"].get(lineup, [])
        raise gracenote.GracenoteError("HTTP 404")


CDVR = "http://cdvr.test:8089"


class GracenoteTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(guide, "CACHE_DIR", self.tmp.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.fake = FakeChannelsDvr()
        patcher = mock.patch.object(gracenote, "_get_json", self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)

    def index(self):
        return gracenote.lineup_index(CDVR, [("USA", "10001")])


class SearchTests(GracenoteTestBase):
    def test_search_plain_then_quoted_and_normalizes(self):
        results = gracenote.search(CDVR + "/", "CNN")
        self.assertEqual(self.fake.urls, [CDVR + "/tms/stations/CNN", CDVR + "/tms/stations/%22CNN%22"])
        self.assertEqual(results[0]["station_id"], "10142")
        self.assertEqual(results[0]["name"], "Cable News Network")
        self.assertEqual(results[0]["logo"], "https://example.invalid/cnn.png")
        self.assertTrue(results[0]["outside"])

    def test_search_cleans_ampersand_and_non_ascii(self):
        gracenote.search(CDVR, "  A&E  Caf\u00e9 ")
        self.assertIn(CDVR + "/tms/stations/%22A%20and%20E%20Cafe%22", self.fake.urls)

    def test_empty_query_does_no_request(self):
        self.assertEqual(gracenote.search(CDVR, "  \u4e2d\u6587 "), [])
        self.assertEqual(self.fake.urls, [])

    def test_non_list_response_is_empty(self):
        with mock.patch.object(gracenote, "_get_json", return_value={"error": "x"}):
            self.assertEqual(gracenote.search(CDVR, "CNN"), [])


class IndexTests(GracenoteTestBase):
    def test_index_fills_names_by_call_sign_and_records_lineups(self):
        index = self.index()
        stations = index["stations"]
        self.assertEqual(stations["10142"]["name"], "Cable News Network")
        self.assertEqual(stations["10003"]["lineups"],
                         ["Spectrum New York", "Local Over the Air Broadcast 10001"])
        self.assertEqual(stations["10003"]["countries"], ["USA"])
        self.assertEqual(index["problems"], [])

    def test_index_is_cached(self):
        self.index()
        count = len(self.fake.urls)
        self.index()
        self.assertEqual(len(self.fake.urls), count)
        self.assertTrue(any(n == "gracenote-index-USA10001.json"
                            for n in os.listdir(self.tmp.name)))

    def test_bad_country_is_reported_not_fatal(self):
        index = gracenote.lineup_index(CDVR, [("USA", "10001"), ("GBR", "XX")])
        self.assertIn("10142", index["stations"])
        self.assertEqual(len(index["problems"]), 1)
        self.assertIn("postal code", index["problems"][0])

    def test_all_countries_failing_raises(self):
        with self.assertRaises(gracenote.GracenoteError):
            gracenote.lineup_index(CDVR, [("GBR", "XX")])


class NormalizationTests(unittest.TestCase):
    def test_tokens_drop_feed_words_and_prefixes(self):
        self.assertEqual(gracenote.tokens("US| Comedy Central HD"), frozenset({"comedy", "central"}))
        self.assertEqual(gracenote.tokens("[UK] The History Channel East"), frozenset({"history"}))
        self.assertEqual(gracenote.tokens("HBO West"), frozenset({"hbo", "pacific"}))
        self.assertEqual(gracenote.tokens("C-SPAN 2"), frozenset({"cspan2"}))
        self.assertEqual(gracenote.tokens("Nat Geo"), frozenset({"national", "geographic"}))
        self.assertEqual(gracenote.tokens("A&E"), frozenset({"a", "and", "e"}))

    def test_explicit_callsign(self):
        self.assertEqual(gracenote.explicit_callsign("ABC 7 New York (WABC)"), "WABC")
        self.assertEqual(gracenote.explicit_callsign("KTLA-DT Los Angeles"), "KTLA")
        self.assertEqual(gracenote.explicit_callsign("US| WNYC"), "WNYC")
        self.assertIsNone(gracenote.explicit_callsign("CNN"))
        self.assertIsNone(gracenote.explicit_callsign("Weather Channel"))

    def test_search_phrase(self):
        self.assertEqual(gracenote.search_phrase("US| ESPN HD (backup)"), "ESPN")

    def test_hd_preference(self):
        self.assertTrue(gracenote.wants_hd("CNN FHD"))
        self.assertFalse(gracenote.wants_hd("CNN"))


class SuggestTests(GracenoteTestBase):
    def test_explicit_callsign_is_matched(self):
        result = gracenote.suggest("ABC 7 (WABC)", CDVR, index=self.index())
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["best"]["station_id"], "10003")
        self.assertEqual(result["best"]["confidence"], 1.0)
        self.assertIn("call sign WABC", result["best"]["reason"])

    def test_acronym_call_sign_matches(self):
        result = gracenote.suggest("US| CNN", CDVR, index=self.index())
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["best"]["station_id"], "10142")
        self.assertEqual(result["best"]["name"], "Cable News Network")

    def test_hd_channel_picks_hd_station(self):
        sd = gracenote.suggest("ESPN", CDVR, index=self.index())
        hd = gracenote.suggest("US| ESPN FHD", CDVR, index=self.index())
        self.assertEqual(sd["best"]["station_id"], "10179")
        self.assertEqual(hd["best"]["station_id"], "32645")

    def test_hd_station_name_is_not_an_acronym_match(self):
        # "Cable News Network HD" has initials CNNH, so CNNHD is not taken as the
        # name "CNN"; like the reference scripts, the SD acronym match wins.
        result = gracenote.suggest("CNN HD", CDVR, index=self.index())
        self.assertEqual(result["best"]["station_id"], "10142")

    def test_exact_name_with_east_suffix(self):
        result = gracenote.suggest("Comedy Central East", CDVR, index=self.index())
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["best"]["station_id"], "10149")

    def test_west_feed_maps_to_pacific(self):
        result = gracenote.suggest("Comedy Central West", CDVR, index=self.index())
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["best"]["station_id"], "62420")

    def test_call_sign_that_is_not_the_name_goes_to_review(self):
        result = gracenote.suggest("Retro", CDVR, index=self.index())
        self.assertEqual(result["status"], "review")
        self.assertIsNone(result["best"])
        self.assertEqual(result["candidates"][0]["station_id"], "84317")

    def test_outside_lineups_never_matched(self):
        result = gracenote.suggest("UK: Sky Sports Main Event", CDVR, index=self.index())
        self.assertEqual(result["status"], "review")
        self.assertIsNone(result["best"])
        ids = [c["station_id"] for c in result["candidates"]]
        self.assertEqual(ids[0], "77001")
        self.assertNotIn("77999", ids)   # test feed filtered
        self.assertNotIn("77998", ids)   # VOD provider filtered
        self.assertTrue(result["candidates"][0]["outside"])
        self.assertIn("outside your lineups", result["candidates"][0]["reason"])

    def test_without_index_only_review(self):
        result = gracenote.suggest("CNN", CDVR)
        self.assertEqual(result["status"], "review")
        self.assertEqual(result["candidates"][0]["station_id"], "10142")

    def test_countries_argument_builds_index(self):
        result = gracenote.suggest("ESPN HD", CDVR, countries=[("USA", "10001")])
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["best"]["station_id"], "32645")

    def test_nothing_found(self):
        result = gracenote.suggest("Zzqx Qwerty", CDVR, index=self.index())
        self.assertEqual(result["status"], "unmatched")
        self.assertEqual(result["candidates"], [])

    def test_candidates_have_confidence_and_reason(self):
        result = gracenote.suggest("ESPN", CDVR, index=self.index())
        for cand in result["candidates"]:
            self.assertGreaterEqual(cand["confidence"], 0)
            self.assertLessEqual(cand["confidence"], 1)
            self.assertTrue(cand["reason"])
        scores = [c["confidence"] for c in result["candidates"]]
        self.assertEqual(scores[0], max(scores))


class BulkSuggestTests(GracenoteTestBase):
    def test_bulk_keeps_order_and_ids(self):
        channels = [{"id": 1, "name": "CNN"}, {"id": 2, "name": "ESPN HD"},
                    {"id": 3, "name": "Zzqx"}]
        results = gracenote.bulk_suggest(channels, CDVR, index=self.index())
        self.assertEqual([r["channel_id"] for r in results], [1, 2, 3])
        self.assertEqual(results[0]["best"]["station_id"], "10142")
        self.assertEqual(results[1]["best"]["station_id"], "32645")
        self.assertEqual(results[2]["status"], "unmatched")

    def test_bulk_survives_unreachable_server(self):
        index = self.index()

        def down(url, timeout=None):
            raise gracenote.GracenoteUnreachable("down")

        with mock.patch.object(gracenote, "_get_json", down):
            results = gracenote.bulk_suggest(
                [{"id": 1, "name": "Sky Sports Main Event"}, {"id": 2, "name": "CNN"}],
                CDVR, index=index)
        self.assertEqual(results[0]["status"], "unmatched")
        self.assertEqual(results[1]["status"], "matched")


if __name__ == "__main__":
    unittest.main()


class LookupFixTests(unittest.TestCase):
    """Failures seen on a real server: foreign feeds ranked like US ones, brand-less look-alikes, run-together names."""

    def st(self, sid, name, call, langs=("en",), outside=True):
        return {"station_id": sid, "call_sign": call, "name": name, "type": "Satellite", "affiliate": "", "logo": "",
                "langs": list(langs), "lineups": [], "countries": [], "outside": outside}

    def test_language_weight(self):
        self.assertEqual(gracenote.language_weight(self.st("1", "x", "X", ["en"])), 1.0)
        self.assertEqual(gracenote.language_weight(self.st("1", "x", "X", [])), 1.0)
        self.assertLess(gracenote.language_weight(self.st("1", "x", "X", ["en-GB"])), 1.0)
        self.assertLess(gracenote.language_weight(self.st("1", "x", "X", ["es"])),
                        gracenote.language_weight(self.st("1", "x", "X", ["en-GB"])))

    def test_us_feed_ranks_above_uk_feed_of_same_name(self):
        uk = self.st("109252", "Love Nature", "LONAVM", ["en-GB"])
        us = self.st("52745", "Love Nature", "NATUR", ["en"])
        mixed = self.st("115097", "Love Nature", "PTVUKLN", ["en", "en-GB"])
        ranked = gracenote.merge_candidates("US: Love Nature", [uk, mixed, us])
        self.assertEqual([s["station_id"] for s in ranked], ["52745", "115097", "109252"])
        cand = gracenote._candidate("US: Love Nature", uk, None, None)
        self.assertIn("UK feed", cand["reason"])

    def test_missing_brand_is_penalized(self):
        good = gracenote.name_score("Spectrum News 1 Hudson Valley NY", "Spectrum News 1 - Hudson Valley HD")
        wrong = gracenote.name_score("Spectrum News 1 Hudson Valley NY", "News 12 Hudson Valley")
        self.assertGreater(good, wrong + 0.2)
        self.assertLess(gracenote.name_score("NBC New York News", "SportsNet New York"),
                        gracenote.SUGGEST_THRESHOLD)

    def test_search_tries_run_together_name(self):
        urls = []

        def fake(url, timeout=None):
            urls.append(url)
            return [{"stationId": "111140", "callSign": "GUSTOTV", "name": "Gusto TV", "bcastLangs": ["en"]}] \
                if url.endswith("/GustoTV") else []
        with mock.patch.object(gracenote, "_get_json", fake):
            found = gracenote.search(CDVR, "Gusto TV")
        self.assertEqual([s["station_id"] for s in found], ["111140"])
        self.assertEqual(found[0]["langs"], ["en"])

    def test_one_failing_search_form_keeps_the_others(self):
        def fake(url, timeout=None):
            if "%22" in url:
                raise gracenote.GracenoteError("HTTP 503")
            return [{"stationId": "19339", "callSign": "MC07", "name": "Music Choice Hip-Hop Classics"}]
        with mock.patch.object(gracenote, "_get_json", fake):
            self.assertEqual(gracenote.search(CDVR, "Music Choice Hip-Hop Classics")[0]["station_id"], "19339")

    def test_nyc_is_new_york(self):
        self.assertEqual(gracenote.tokens("News12+ New York"), gracenote.tokens("News 12+ NYC"))
