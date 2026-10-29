"""Fixes from the Oct 29 targeted audit (1.0.6-7), one test per fix."""
import unittest

from tests.test_server_api import ApiTestCase


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
