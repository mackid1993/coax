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
        names = ["PPV 10", "ESPN2 ᴿᴬᵂ", "ESPN ᴿᴬᵂ", "PPV 2", "Équipe 21", "ppv 1", "NEWS 12", "NEW\xa0JERSEY"]
        self.assertEqual(m3u_names(lineup_cfg(names, list(range(1, 9)), "alpha")),
                         ["Équipe 21", "ESPN ᴿᴬᵂ", "ESPN2 ᴿᴬᵂ", "NEW\xa0JERSEY", "NEWS 12", "ppv 1", "PPV 2", "PPV 10"])


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

    def test_reordering_streams_never_renumbers_a_provider_sorted_lineup(self):
        self.add_provider()
        status, ch, _ = self.request("POST", "/api/channels", {"name": "ABC", "streams": [
            {"provider": "p1", "stream_id": "1"}, {"provider": "p1", "stream_id": "2"}]})
        self.app.catalogs.get = lambda pid: {"by_id": {"1": {"num": 5}, "2": {"num": 905}}}
        channel = lambda: self.store.get()["channels"][0]  # noqa: E731
        self.assertEqual(self.app.provider_num(channel()), "5")
        self.store.update(lambda cfg: cfg["channels"][0]["streams"].reverse())   # a Smart Standby promotion
        self.assertEqual(self.app.provider_num(channel()), "5")
        self.store.update(lambda cfg: cfg["channels"][0]["streams"].pop())       # its stream is removed
        self.assertEqual(self.app.provider_num(channel()), "905")

    def test_settings_xmltv_sources_get_fresh_ids_and_removal_clears_channels(self):
        put = lambda sources: self.request("PUT", "/api/config", {"xmltv_sources": sources})  # noqa: E731
        status, cfg, _ = put([{"id": "", "name": "A", "url": "http://a/x.xml"},
                              {"id": "", "name": "B", "url": "http://b/x.xml"}])
        self.assertEqual((status, [x["id"] for x in cfg["xmltv_sources"]]), (200, ["x1", "x2"]))
        status, ch, _ = self.request("POST", "/api/channels", {"name": "One", "guide": {
            "mode": "xmltv", "xmltv_id": "one.a", "xmltv_provider": "x1"}})
        # remove A (used by a channel) and add C in the same save: C must not become x1 or collide with x2
        status, cfg, _ = put([{"id": "x2", "name": "B", "url": "http://b/x.xml"},
                              {"id": "", "name": "C", "url": "http://c/x.xml"}])
        self.assertEqual((status, [x["id"] for x in cfg["xmltv_sources"]]), (200, ["x2", "x3"]), cfg)
        guide = self.store.get()["channels"][0]["guide"]
        self.assertEqual((guide["xmltv_provider"], guide["xmltv_id"]), ("", ""))

    def test_deleting_a_provider_drops_its_held_suggestions_and_its_id_is_never_reused(self):
        self.add_provider()
        p2 = self.add_provider(name="Provider B")
        self.assertEqual(p2["id"], "p2")
        status, ch, _ = self.request("POST", "/api/channels", {"name": "CNN", "streams": [
            {"provider": "p1", "stream_id": "1"}]})
        held = {str(ch["id"]): {"guide": None, "backups": [{"provider": "p2", "stream_id": "9", "checked": True},
                                                          {"provider": "p1", "stream_id": "2", "checked": True}]}}
        self.request("PUT", "/api/suggestions", {"rev": 0, "pending": held})
        status, r, _ = self.request("DELETE", "/api/providers/p2")
        self.assertEqual(status, 200, r)
        status, d, _ = self.request("GET", "/api/suggestions")
        self.assertEqual([b["provider"] for b in d["pending"][str(ch["id"])]["backups"]], ["p1"])
        self.assertEqual(d["rev"], 2)   # an open page takes the new copy instead of saving over it
        # a stale page that still sends the p2 backup: it is skipped, the rest is saved
        status, r, _ = self.request("POST", "/api/channels/apply", {"channels": [{"id": ch["id"], "add_streams": [
            {"provider": "p2", "stream_id": "9"}, {"provider": "p1", "stream_id": "2"}]}]})
        self.assertEqual(status, 200, r)
        self.assertEqual([s["stream_id"] for s in self.store.get()["channels"][0]["streams"]], ["1", "2"])
        self.assertEqual(self.add_provider(name="Provider C")["id"], "p3")

    def test_force_deleting_a_provider_names_channels_left_with_only_disabled_streams(self):
        self.add_provider()
        self.add_provider(name="Provider B")
        self.request("POST", "/api/channels", {"name": "CNN", "streams": [
            {"provider": "p1", "stream_id": "1"}, {"provider": "p2", "stream_id": "7", "disabled": True}]})
        status, r, _ = self.request("DELETE", "/api/providers/p1?force=1")
        self.assertEqual((status, r["only_disabled_streams"]), (200, ["CNN"]))

    def test_renamed_stream_ids_carry_held_suggestions_and_smart_standby_blocks(self):
        from app.feedhealth import FeedHealth
        self.app.health = FeedHealth(self.store, self.dir)
        self.refreshed_provider()
        s, d, _ = self.request("POST", "/api/channels/bulk-add", {"streams": [{"provider": "p1", "stream_id": "1"}]})
        cid = d["created"][0]
        self.store.update(lambda cfg: cfg["channels"][0]["streams"].append({"provider": "p1", "stream_id": "3"}))
        held = {str(cid): {"backups": [{"provider": "p1", "stream_id": "2", "checked": True}]}}
        self.request("PUT", "/api/suggestions", {"rev": 0, "pending": held})
        now = __import__("time").time()
        self.app.health._block(cid, ("p1", "1"), "placeholder", "", now)
        self.app._apply_renamed("p1", {"1": "101", "2": "102"})
        self.assertIn("101", [r["stream_id"] for r in self.store.get()["channels"][0]["streams"]])
        self.assertEqual(self.request("GET", "/api/suggestions")[1]["pending"][str(cid)]["backups"][0]["stream_id"],
                         "102")
        self.assertGreater(self.app.health.blocked_until(cid, ("p1", "101"), now=now + 1), now + 1)
        self.assertEqual(self.app.health.blocked_until(cid, ("p1", "1"), now=now + 1), 0)

    def test_refresh_and_test_errors_hide_the_username(self):
        from app import server
        provider = {"lines": [{"username": "alice123", "password": "s3cret!!"}]}
        text = server.redact("GET http://h/player_api.php?username=alice123&password=s3cret!! failed", provider)
        self.assertNotIn("alice123", text)
        self.assertNotIn("s3cret!!", text)

    def test_auto_match_takes_backups_only_from_picked_groups_none_when_none_are_picked(self):
        from app import backups
        seen = []
        saved = backups.suggest
        backups.suggest = lambda channel, catalogs, *a, **k: seen.append(
            sorted(s["stream_id"] for c in catalogs.values() for s in c.get("streams") or [])) or []
        self.addCleanup(setattr, backups, "suggest", saved)
        self.refreshed_provider()   # picks US| NEWS
        self.request("POST", "/api/channels", {"name": "CNN", "streams": [{"provider": "p1", "stream_id": "1"}]})
        self.request("POST", "/api/automatch", {"ids": [1]})
        self.request("PUT", "/api/providers/p1/groups", {"groups": []})
        self.request("POST", "/api/automatch", {"ids": [1]})
        self.assertEqual(seen, [["1", "2"], []])


class GuideRefreshRequests(unittest.TestCase):
    def test_a_changed_xmltv_source_refreshes_the_guide_now_and_at_its_own_interval(self):
        from app.providers import scheduler
        runs = []
        saved = scheduler.refresh_guide
        scheduler.refresh_guide = lambda cfg: runs.append(1)
        try:
            cfg = {"providers": [], "xmltv_sources": [{"id": "x1", "url": "http://x", "refresh_hours": 2}]}
            state = {"guide_at": scheduler.clock()}
            scheduler.run_once(lambda: cfg, state)
            self.assertEqual(runs, [])
            scheduler.request_guide()
            scheduler.run_once(lambda: cfg, state)
            self.assertEqual(runs, [1])
            self.assertEqual(scheduler._guide_hours(cfg), 2)
        finally:
            scheduler.refresh_guide = saved


if __name__ == "__main__":
    unittest.main()
