"""Fixes from the Oct 29 targeted audit (1.0.6-7), one test per fix."""
import unittest

from app.guide import output
from tests.test_server_api import ApiTestCase


def lineup_cfg(names, lineup_order, sort="manual"):
    channels = [{"id": i + 1, "uuid": "u%d" % (i + 1), "name": n, "number": str(i + 1), "enabled": True,
                 "guide": {}} for i, n in enumerate(names)]
    return {"server": {"base_url": "http://coax"}, "channels": channels,
            "lineups": [{"id": "main", "channels": lineup_order, "sort": sort, "start_number": 100}]}


def m3u_names(cfg):
    return [line.rsplit(",", 1)[1] for line in output.m3u(cfg, "main").splitlines() if line.startswith("#EXTINF")]


class LineupOrder(unittest.TestCase):
    def test_manual_lineup_follows_coax_order_not_the_order_channels_were_added(self):
        self.assertEqual(m3u_names(lineup_cfg(["CNN", "FOX", "ESPN"], [2, 3, 1])), ["CNN", "FOX", "ESPN"])

    def test_alphabetical_matches_the_channels_page(self):
        names = ["PPV 10", "ESPN", "PPV 2", "Équipe 21", "ppv 1"]
        self.assertEqual(m3u_names(lineup_cfg(names, [1, 2, 3, 4, 5], "alpha")),
                         ["Équipe 21", "ESPN", "ppv 1", "PPV 2", "PPV 10"])


class AuditFixes(ApiTestCase):
    def test_provider_total_stream_limit_is_saved(self):
        provider = self.add_provider(max_streams=2)
        self.assertEqual(provider["max_streams"], 2)
        status, provider, _ = self.request("PUT", "/api/providers/p1", {"max_streams": 3})
        self.assertEqual((status, provider["max_streams"]), (200, 3))
        self.assertEqual(self.store.get()["providers"][0]["max_streams"], 3)

    def test_new_channels_start_as_gracenote_with_the_tvg_id_as_a_hint(self):
        self.refreshed_provider()
        status, result, _ = self.request("POST", "/api/channels/bulk-add",
                                         {"streams": [{"provider": "p1", "stream_id": "1"}]})
        self.assertEqual(status, 200, result)
        guide = self.store.get()["channels"][0]["guide"]
        self.assertEqual((guide["mode"], guide["station_id"], guide["xmltv_id"]), ("gracenote", "", "cnn.us"))
        self.request("PUT", "/api/config", {"channels_dvr": {"url": "http://dvr:8089/"}})
        status, result, _ = self.request("POST", "/api/gracenote/bulk-suggest", {})
        self.assertEqual(status, 200, result)
        self.assertIn("1", result["results"])   # Auto-match looks for its station


if __name__ == "__main__":
    unittest.main()
