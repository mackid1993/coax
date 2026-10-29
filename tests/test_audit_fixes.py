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


if __name__ == "__main__":
    unittest.main()
