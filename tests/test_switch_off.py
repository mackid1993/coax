"""On/off switches for providers and lineups: a provider switched off is not used, and channels with no other
provider leave the playlists; a lineup switched off serves an empty playlist, so Channels DVR drops its channels."""
import unittest

from app import channels_dvr
from app.guide import output
from tests.test_guide_xmltv import XmltvTestBase, make_config
from tests.test_server_api import ApiTestCase


def lineup_config():
    cfg = make_config()
    cfg["lineups"] = [{"id": "main", "name": "Main", "channels": [1, 2, 4, 5]}]
    # Channel 2 has a stream on each provider, so it stays when p2 is off; 4 and 5 are on p2 alone.
    cfg["channels"][1]["streams"].append({"provider": "p2", "stream_id": "y"})
    return cfg


def names(text):
    return [line.rsplit(",", 1)[1] for line in text.splitlines() if line.startswith("#EXTINF")]


class PlaylistTests(unittest.TestCase):
    def test_provider_off_drops_only_channels_without_another_provider(self):
        cfg = lineup_config()
        self.assertEqual(names(output.m3u(cfg, "main")), ["CNN 'Live'", "ESPN", "BBC One", "BBC One Copy"])
        cfg["providers"][1]["enabled"] = False
        self.assertEqual(names(output.m3u(cfg, "main")), ["CNN 'Live'", "ESPN"])

    def test_lineup_off_is_an_empty_playlist(self):
        cfg = lineup_config()
        cfg["lineups"][0]["enabled"] = False
        self.assertEqual(output.m3u(cfg, "main"), "#EXTM3U\n")

    def test_dvr_snapshot_follows_the_switches(self):
        cfg = lineup_config()
        snap = lambda: channels_dvr._snapshot(cfg, "main", output.m3u)  # noqa: E731
        self.assertEqual(snap()["channels"], {1, 2, 4, 5})
        cfg["providers"][1]["enabled"] = False
        self.assertEqual(snap()["channels"], {1, 2})
        cfg["lineups"][0]["enabled"] = False
        self.assertEqual((snap()["channels"], snap()["m3u"]), (set(), "#EXTM3U\n"))


class XmltvTests(XmltvTestBase):
    def test_provider_off_is_not_downloaded(self):
        cfg = make_config()
        cfg["providers"][1]["enabled"] = False
        summary, fake = self.run_refresh(self.default_mapping(), cfg)
        self.assertEqual([url for url, _ in fake.calls], ["http://p1.test/xmltv"])
        self.assertEqual((summary["providers"]["p2"]["ok"], summary["providers"]["p2"]["error"]),
                         (True, "switched off"))
        self.assertTrue(summary["lineups"]["main"]["ok"], summary)


class ApiTests(ApiTestCase):
    def test_switches_are_saved(self):
        provider = self.add_provider()
        self.assertTrue(provider["enabled"])
        status, data, _ = self.request("PUT", "/api/providers/p1", {"enabled": False})
        self.assertEqual((status, data["enabled"]), (200, False))
        stored = self.store.get()["providers"][0]
        self.assertEqual((stored["enabled"], stored["lines"][0]["password"]), (False, "secret"))
        self.assertEqual(self.request("PUT", "/api/providers/p1", {"enabled": "no"})[0], 400)

        self.request("PUT", "/api/config", {"server": {"base_url": "http://coax:7780"}})
        status, lineup, _ = self.request("POST", "/api/lineups", {"name": "Sports"})
        self.assertTrue(lineup["enabled"])
        status, lineup, _ = self.request("PUT", "/api/lineups/sports", {"enabled": False})
        self.assertEqual((status, lineup["enabled"]), (200, False))
        self.assertEqual(self.request("PUT", "/api/lineups/sports", {"enabled": 0})[0], 400)
        status, body, _ = self.request("GET", "/output/m3u/sports.m3u")
        self.assertEqual(body, b"#EXTM3U\n")


if __name__ == "__main__":
    unittest.main()


class LineupOffTests(ApiTestCase):
    def test_lineup_off_takes_no_channels_and_keeps_its_own(self):
        self.request("POST", "/api/channels", {"name": "One"})
        self.request("POST", "/api/lineups", {"name": "Sports", "channels": [1]})
        self.request("PUT", "/api/lineups/main", {"enabled": False})
        self.request("POST", "/api/channels", {"name": "Two"})
        lineups = {l["id"]: l["channels"] for l in self.store.get()["lineups"]}
        self.assertEqual(lineups, {"main": [1], "sports": [1, 2]})
        # A channel save that leaves the switched-off lineup out does not take the channel out of it.
        self.request("PUT", "/api/channels/1", {"lineups": ["sports", "main"]})
        self.request("PUT", "/api/channels/2", {"lineups": ["sports", "main"]})
        lineups = {l["id"]: l["channels"] for l in self.store.get()["lineups"]}
        self.assertEqual(lineups, {"main": [1], "sports": [1, 2]})

    def test_every_lineup_off_makes_a_new_one(self):
        self.request("PUT", "/api/lineups/main", {"enabled": False})
        status, created, _ = self.request("POST", "/api/channels", {"name": "One"})
        self.assertEqual(status, 200)
        lineups = {l["id"]: (l["enabled"], l["channels"]) for l in self.store.get()["lineups"]}
        self.assertEqual(lineups, {"main": (False, []), "main-2": (True, [created["id"]])})


class StatusTests(ApiTestCase):
    def test_status_leaves_out_what_is_switched_off(self):
        self.add_provider()
        self.add_provider(name="Provider B")
        self.request("POST", "/api/channels/bulk-add", {"streams": [{"provider": "p1", "stream_id": "1"}]})
        self.request("POST", "/api/channels/bulk-add", {"streams": [{"provider": "p2", "stream_id": "2"}]})
        self.request("POST", "/api/channels", {"name": "Test only"})
        self.request("POST", "/api/lineups", {"name": "Test", "channels": [3]})
        self.request("PUT", "/api/channels/3", {"lineups": ["test"]})
        status = self.request("GET", "/api/status")[1]
        self.assertEqual((status["channels"]["total"], len(status["lines"])), (3, 2))
        self.request("PUT", "/api/providers/p2", {"enabled": False})
        self.request("PUT", "/api/lineups/test", {"enabled": False})
        status = self.request("GET", "/api/status")[1]
        self.assertEqual(status["channels"]["total"], 1)
        self.assertEqual([l["provider"] for l in status["lines"]], ["p1"])
