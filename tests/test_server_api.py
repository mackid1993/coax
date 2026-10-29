"""HTTP tests for app.server with stub providers, guide and hub modules (no network beyond localhost)."""

import http.client
import json
import os as _os

_os.environ.setdefault("COAX_SKIP_URL_CHECK", "1")
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import types
import unittest

from app import config as configmod
from app import server as servermod

STUB_NAMES = ("app.providers.catalog", "app.providers.scheduler", "app.guide.output", "app.guide.gracenote",
              "app.guide.xmltv", "app.providers", "app.backups")


def make_stubs(data_dir):
    catalog = types.ModuleType("app.providers.catalog")
    catalog.refresh_calls = []

    def path(pid):
        return os.path.join(data_dir, "cache", "catalog-%s.json" % pid)

    def load(pid):
        try:
            with open(path(pid)) as handle:
                return json.load(handle)
        except OSError:
            return None

    def refresh(provider):
        catalog.refresh_calls.append(provider["id"])
        if getattr(catalog, "fail", False):
            raise RuntimeError("provider down")
        data = catalog.next_catalog
        os.makedirs(os.path.dirname(path(provider["id"])), exist_ok=True)
        with open(path(provider["id"]), "w") as handle:
            json.dump(data, handle)
        return data

    def stream_url(provider, line, stream_id):
        return "%s/live/%s/%s/%s.ts" % (provider["server"], line["username"], line["password"], stream_id)

    catalog.load, catalog.refresh, catalog.stream_url = load, refresh, stream_url

    providers_pkg = types.ModuleType("app.providers")
    providers_pkg.__path__ = []
    providers_pkg.stream_url = stream_url
    providers_pkg.catalog = catalog

    def test_connection(provider):
        return {"ok": provider["lines"][0]["password"] == "secret", "message": "tested"}
    providers_pkg.test_connection = test_connection

    output = types.ModuleType("app.guide.output")
    output.m3u = lambda cfg, lineup: "#EXTM3U\n" + "".join(
        "#EXTINF:-1,%s\n%s/stream/%s.ts\n" % (c["name"], cfg["server"]["base_url"], c["uuid"])
        for c in cfg["channels"] if c["id"] in next(l for l in cfg["lineups"] if l["id"] == lineup)["channels"])
    output.xmltv_path = lambda lineup: os.path.join(data_dir, "cache", "xmltv-%s.xml" % lineup)

    gracenote = types.ModuleType("app.guide.gracenote")
    gracenote.calls = []

    def search(cdvr, query):
        gracenote.calls.append((cdvr, query))
        return [{"station_id": "10149", "name": query, "call_sign": "CC"}]

    def suggest(name, cdvr_url=None):
        return [{"station_id": "1", "name": name, "confidence": 0.9, "cdvr": cdvr_url}]
    def bulk_suggest(channels, cdvr_url="", countries=None):
        return [{"status": "matched", "reason": "", "best": None, "channel_id": c["id"],
                 "candidates": [{"station_id": "10149", "name": c["name"], "confidence": 0.95}]} for c in channels]
    gracenote.search, gracenote.suggest, gracenote.bulk_suggest = search, suggest, bulk_suggest

    xmltv = types.ModuleType("app.guide.xmltv")   # no helper: server falls back to the cache file
    scheduler = types.ModuleType("app.providers.scheduler")
    scheduler.last_results = lambda: {}
    return {"app.providers.catalog": catalog, "app.providers": providers_pkg, "app.guide.output": output,
            "app.guide.gracenote": gracenote, "app.guide.xmltv": xmltv, "app.providers.scheduler": scheduler}


class StubSession:
    def __init__(self, chunks, hub):
        self.chunks, self.hub, self.closed = chunks, hub, False

    def iter_bytes(self):
        for chunk in self.chunks:
            if self.closed:
                return
            yield chunk
            time.sleep(0.01)

    def close(self):
        self.closed = True
        self.hub.closed.append(self)


class StubHub:
    def __init__(self, get_config):
        self.get_config = get_config
        self.closed = []
        self.chunks = [b"G" * 188 * 10] * 5

    def open(self, uuid, ip, ua):
        for channel in self.get_config()["channels"]:
            if channel["uuid"] == uuid and channel["enabled"]:
                return StubSession(self.chunks, self)
        return None

    def backup_urls(self, channel_id):
        return ["http://backup/%d" % channel_id]

    def status(self):
        return [{"channel_id": 1, "channel": "One", "provider_id": "p1", "line_id": "l1",
                 "clients": [{"ip": "x"}]}]


CATALOG = {"fetched_at": "2026-01-01T00:00:00Z", "groups": ["US| NEWS", "US| SPORTS"], "streams": [
    {"stream_id": "1", "name": "CNN HD", "group": "US| NEWS", "logo": "", "tvg_id": "cnn.us"},
    {"stream_id": "2", "name": "FOX News", "group": "US| NEWS", "logo": "", "tvg_id": ""},
    {"stream_id": "3", "name": "ESPN", "group": "US| SPORTS", "logo": "", "tvg_id": ""},
]}


class ApiTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.saved_modules = {n: sys.modules.get(n) for n in STUB_NAMES}
        self.stubs = make_stubs(self.dir)
        sys.modules.update(self.stubs)
        self.stubs["app.providers.catalog"].next_catalog = CATALOG
        self.store = configmod.ConfigStore(os.path.join(self.dir, "config.json"))
        self.store.load()
        self.hub = StubHub(self.store.get)
        self.app = servermod.App(self.store, self.hub, servermod.RingLogHandler(50))
        self.httpd = servermod.make_server(self.app, "127.0.0.1", 0)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        for name, module in self.saved_modules.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
        shutil.rmtree(self.dir)

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        data = json.dumps(body).encode() if body is not None else None
        conn.request(method, path, body=data, headers=dict({"Content-Type": "application/json"}, **(headers or {})))
        response = conn.getresponse()
        raw = response.read()
        conn.close()
        ctype = response.getheader("Content-Type", "")
        payload = json.loads(raw) if ctype.startswith("application/json") else raw
        return response.status, payload, response

    def add_provider(self, **extra):
        body = {"name": "Provider A", "type": "xtream", "server": "http://host:80/",
                "lines": [{"username": "u", "password": "secret", "max_streams": 2}]}
        body.update(extra)
        status, data, _ = self.request("POST", "/api/providers", body)
        self.assertEqual(status, 200, data)
        return data

    def refreshed_provider(self):
        provider = self.add_provider()
        status, result, _ = self.request("POST", "/api/providers/%s/refresh?wait=1" % provider["id"])
        self.assertEqual(status, 200, result)
        self.assertTrue(result["ok"], result)
        status, _, _ = self.request("PUT", "/api/providers/p1/groups", {"groups": ["US| NEWS"]})
        self.assertEqual(status, 200)
        return provider


class BasicRoutes(ApiTestCase):
    def test_healthz_and_unknown_routes(self):
        status, body, _ = self.request("GET", "/healthz")
        self.assertEqual((status, body), (200, b"ok"))
        status, body, _ = self.request("GET", "/api/nope")
        self.assertEqual(status, 404)
        self.assertIn("error", body)
        status, body, _ = self.request("DELETE", "/api/config")
        self.assertEqual(status, 405)

    def test_version_is_the_stamped_one(self):
        import app
        status, body, _ = self.request("GET", "/api/version")
        self.assertEqual((status, body), (200, {"version": app.VERSION}))

    def test_static_files(self):
        web = servermod.WEB_DIR
        created = []
        index = os.path.join(web, "index.html")
        if not os.path.exists(index):
            os.makedirs(web, exist_ok=True)
            with open(index, "w") as handle:
                handle.write("<html></html>")
            created.append(index)
        try:
            status, body, response = self.request("GET", "/")
            self.assertEqual(status, 200)
            self.assertEqual(response.getheader("Content-Type"), "text/html; charset=utf-8")
            self.assertEqual(response.getheader("Cache-Control"), "no-cache")
            etag = response.getheader("ETag")
            status, _, _ = self.request("GET", "/index.html", headers={"If-None-Match": etag})
            self.assertEqual(status, 304)
            status, _, _ = self.request("GET", "/../../etc/passwd")
            self.assertEqual(status, 404)
            status, _, _ = self.request("GET", "/%2e%2e/config.py")
            self.assertEqual(status, 404)
        finally:
            for path in created:
                os.unlink(path)

    def test_bad_json_is_400(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("PUT", "/api/config", body=b"{bad", headers={"Content-Type": "application/json"})
        response = conn.getresponse()
        self.assertEqual(response.status, 400)
        self.assertIn("not valid JSON", json.loads(response.read())["error"])
        conn.close()


class ConfigRoutes(ApiTestCase):
    def test_get_put_masks_and_validates(self):
        self.add_provider()
        status, cfg, _ = self.request("GET", "/api/config")
        self.assertEqual(status, 200)
        self.assertEqual(cfg["providers"][0]["lines"][0]["password"], "********")
        status, cfg, _ = self.request("PUT", "/api/config", {"server": {"base_url": "http://10.0.0.2:7780"},
                                                            "transcode": "720p30"})
        self.assertEqual(status, 200, cfg)
        self.assertEqual(cfg["server"]["port"], 7780)
        self.assertEqual(cfg["server"]["base_url"], "http://10.0.0.2:7780")
        self.assertEqual(self.store.get()["providers"][0]["lines"][0]["password"], "secret")
        status, err, _ = self.request("PUT", "/api/config", {"transcode": "8k"})
        self.assertEqual(status, 400)
        self.assertIn("transcode", err["error"])
        status, err, _ = self.request("PUT", "/api/config", {"server": {"port": "x"}})
        self.assertEqual(status, 400)


class ProviderRoutes(ApiTestCase):
    def test_crud_and_masking(self):
        provider = self.add_provider()
        self.assertEqual(provider["id"], "p1")
        self.assertEqual(provider["server"], "http://host:80")
        self.assertEqual(provider["lines"][0]["id"], "l1")
        self.assertEqual(provider["lines"][0]["password"], "********")
        provider["name"] = "Renamed"
        provider["lines"].append({"username": "u2", "password": "pw2", "max_streams": 1})
        status, updated, _ = self.request("PUT", "/api/providers/p1", provider)
        self.assertEqual(status, 200, updated)
        stored = self.store.get()["providers"][0]
        self.assertEqual([l["password"] for l in stored["lines"]], ["secret", "pw2"])
        self.assertEqual(stored["lines"][1]["id"], "l2")
        status, listing, _ = self.request("GET", "/api/providers")
        self.assertEqual(listing["providers"][0]["name"], "Renamed")
        status, err, _ = self.request("POST", "/api/providers", {"name": "", "type": "xtream"})
        self.assertEqual(status, 400)
        status, err, _ = self.request("POST", "/api/providers", {"name": "M", "type": "m3u", "m3u_url": "ftp://x"})
        self.assertEqual(status, 400)
        self.assertIn("http", err["error"])
        status, m3u, _ = self.request("POST", "/api/providers", {"name": "M", "type": "m3u",
                                                               "m3u_url": "http://x/p.m3u"})
        self.assertEqual(status, 200, m3u)
        self.assertEqual(m3u["lines"][0]["max_streams"], 0)
        status, _, _ = self.request("DELETE", "/api/providers/p2")
        self.assertEqual(status, 200)
        status, _, _ = self.request("GET", "/api/providers/p2")
        self.assertEqual(status, 404)

    def test_test_connection(self):
        self.add_provider()
        status, result, _ = self.request("POST", "/api/providers/p1/test")
        self.assertEqual((status, result["ok"]), (200, True))
        status, result, _ = self.request("POST", "/api/providers/test", {
            "id": "p1", "name": "Provider A", "type": "xtream", "server": "http://host",
            "lines": [{"id": "l1", "username": "u", "password": "********"}]})
        self.assertTrue(result["ok"], result)

    def test_refresh_groups_and_catalog(self):
        self.refreshed_provider()
        status, groups, _ = self.request("GET", "/api/providers/p1/groups")
        self.assertEqual(status, 200)
        self.assertEqual(groups["groups"], [{"name": "US| NEWS", "count": 2, "selected": True},
                                            {"name": "US| SPORTS", "count": 1, "selected": False}])
        status, page, _ = self.request("GET", "/api/catalog/streams")
        self.assertEqual(page["total"], 2)
        status, page, _ = self.request("GET", "/api/catalog/streams?q=cnn&provider=p1")
        self.assertEqual([i["stream_id"] for i in page["items"]], ["1"])
        status, page, _ = self.request("GET", "/api/catalog/streams?all_groups=1&group=US%7C%20SPORTS")
        self.assertEqual([i["name"] for i in page["items"]], ["ESPN"])
        status, page, _ = self.request("GET", "/api/catalog/streams?limit=1&offset=1")
        self.assertEqual((page["total"], len(page["items"])), (2, 1))
        status, err, _ = self.request("GET", "/api/catalog/streams?limit=abc")
        self.assertEqual(status, 400)

    def test_refresh_failure_and_vanished(self):
        self.refreshed_provider()
        catalog = self.stubs["app.providers.catalog"]
        catalog.next_catalog = {"groups": CATALOG["groups"], "streams": CATALOG["streams"][1:] + [
            {"stream_id": "9", "name": "New", "group": "US| NEWS"}]}
        status, result, _ = self.request("POST", "/api/providers/p1/refresh?wait=1")
        self.assertEqual(result["vanished"], ["1"])
        self.assertEqual(result["added"], 1)
        catalog.fail = True
        status, result, _ = self.request("POST", "/api/providers/p1/refresh?wait=1")
        self.assertFalse(result["ok"])
        self.assertIn("provider down", result["error"])
        status, st, _ = self.request("GET", "/api/status")
        self.assertEqual(st["refresh"]["p1"]["state"], "error")
        # the old catalog still serves
        status, page, _ = self.request("GET", "/api/catalog/streams")
        self.assertEqual(page["total"], 2)

    def test_auto_add(self):
        provider = self.add_provider(auto_add=True)
        self.request("POST", "/api/providers/p1/refresh?wait=1")
        self.request("PUT", "/api/providers/p1/groups", {"groups": ["US| NEWS"]})
        self.stubs["app.providers.catalog"].next_catalog = {"groups": [], "streams": CATALOG["streams"] + [
            {"stream_id": "9", "name": "New", "group": "US| NEWS"}]}
        status, result, _ = self.request("POST", "/api/providers/p1/refresh?wait=1")
        self.assertEqual(result["auto_added"], 1)
        channel = self.store.get()["channels"][0]
        self.assertEqual((channel["name"], channel["enabled"]), ("New", False))

    def test_refresh_asks_the_dvr_sync_to_compare(self):
        calls = []
        self.app.dvr = types.SimpleNamespace(on_change=lambda old, new: calls.append(1))
        provider = self.refreshed_provider()   # first download: nothing to compare yet
        self.request("POST", "/api/providers/%s/refresh?wait=1" % provider["id"])
        self.assertEqual(calls, [1])

    def test_lineup_m3u_uses_provider_numbers_for_provider_sorted_lineups(self):
        self.refreshed_provider()
        self.request("POST", "/api/channels/bulk-add", {"streams": [{"provider": "p1", "stream_id": "1"}]})
        seen = []
        out = sys.modules["app.guide.output"]
        real, out.m3u = out.m3u, lambda cfg, lineup: seen.append([c.get("_pnum") for c in cfg["channels"]]) or ""
        try:
            cfg = self.store.get()
            self.app.lineup_m3u(cfg, cfg["lineups"][0]["id"])
            self.assertEqual(seen, [[None]])
            self.store.update(lambda c: c["lineups"][0].update(sort="provider"))
            cfg = self.store.get()
            self.app.lineup_m3u(cfg, cfg["lineups"][0]["id"])
            self.assertIsNotNone(seen[1][0])
        finally:
            out.m3u = real

    def test_renamed_stream_ids_carry_over_to_channels(self):
        self.refreshed_provider()
        self.request("POST", "/api/channels/bulk-add", {"streams": [{"provider": "p1", "stream_id": "1"}]})
        self.assertEqual(self.app._apply_renamed("p1", {"1": "77", "5": "6"}), 1)
        self.assertEqual(self.store.get()["channels"][0]["streams"], [{"provider": "p1", "stream_id": "77"}])
        self.assertEqual(self.app._apply_renamed("p1", {"5": "6"}), 0)

    def test_delete_provider_in_use(self):
        self.refreshed_provider()
        self.request("POST", "/api/channels/bulk-add", {"streams": [{"provider": "p1", "stream_id": "1"}]})
        status, err, _ = self.request("DELETE", "/api/providers/p1")
        self.assertEqual(status, 409)
        status, result, _ = self.request("DELETE", "/api/providers/p1?force=1")
        self.assertEqual((status, result["channels_detached"]), (200, 1))
        self.assertEqual(self.store.get()["channels"][0]["streams"], [])


class ChannelRoutes(ApiTestCase):
    def test_bulk_add_crud_and_flags(self):
        self.refreshed_provider()
        refs = [{"provider": "p1", "stream_id": "1"}, {"provider": "p1", "stream_id": "2"}]
        status, result, _ = self.request("POST", "/api/channels/bulk-add", {"streams": refs, "start_number": 101})
        self.assertEqual(status, 200, result)
        self.assertEqual(result["created"], [1, 2])
        status, again, _ = self.request("POST", "/api/channels/bulk-add", {"streams": refs[:1]})
        self.assertEqual((again["created"], len(again["skipped"])), ([], 1))
        status, listing, _ = self.request("GET", "/api/channels")
        channels = listing["channels"]
        self.assertEqual([c["number"] for c in channels], ["101", "102"])
        self.assertEqual(channels[0]["guide"]["xmltv_id"], "cnn.us")
        self.assertEqual(channels[0]["lineups"], ["main"])
        self.assertEqual(channels[0]["streams"][0]["name"], "CNN HD")
        self.assertFalse(channels[0]["streams"][0]["missing"])
        # attach a backup that vanished from the catalog: flagged, not deleted
        status, ch, _ = self.request("POST", "/api/channels/1/streams",
                                     {"streams": [{"provider": "p1", "stream_id": "77"}, refs[0]]})
        self.assertEqual(len(ch["streams"]), 2)
        self.assertTrue(ch["streams"][1]["missing"])
        self.assertEqual(ch["missing_streams"], 1)
        # order mode reorders what is there: a stale or unknown ref changes nothing else, flags stay
        self.app.store.update(lambda cfg: cfg["channels"][0]["streams"][0].update(disabled=True))
        status, ch, _ = self.request("POST", "/api/channels/1/streams",
                                     {"mode": "order", "streams": [{"provider": "p1", "stream_id": "77"},
                                                                   {"provider": "p1", "stream_id": "99"}]})
        self.assertEqual(status, 200, ch)
        self.assertEqual([(s["stream_id"], bool(s.get("disabled"))) for s in ch["streams"]], [("77", False), ("1", True)])
        status, ch, _ = self.request("PUT", "/api/channels/1", {"name": "CNN", "enabled": False,
                                                              "guide": {"mode": "gracenote", "station_id": "5"}})
        self.assertEqual((ch["name"], ch["enabled"], ch["guide"]["mode"]), ("CNN", False, "gracenote"))
        self.assertEqual(ch["guide"]["xmltv_id"], "cnn.us")
        status, err, _ = self.request("PUT", "/api/channels/1", {"number": "abc"})
        self.assertEqual(status, 400)
        status, err, _ = self.request("PUT", "/api/channels/1", {"guide": {"mode": "magic"}})
        self.assertEqual(status, 400)
        status, err, _ = self.request("POST", "/api/channels/bulk-add",
                                      {"streams": [{"provider": "zz", "stream_id": "1"}]})
        self.assertEqual(status, 404)
        status, _, _ = self.request("DELETE", "/api/channels/2")
        self.assertEqual(status, 200)
        status, created, _ = self.request("POST", "/api/channels", {"name": "Manual", "streams": refs[1:]})
        self.assertEqual(created["id"], 3)   # id 2 is never reused
        self.assertEqual(self.store.get()["lineups"][0]["channels"], [1, 3])

    def test_reorder_auto_number_bulk_update(self):
        for name in ("A", "B", "C"):
            self.request("POST", "/api/channels", {"name": name})
        status, result, _ = self.request("POST", "/api/channels/reorder", {"ids": [3, 1, 2]})
        self.assertEqual(result["order"], [3, 1, 2])
        self.assertEqual(self.store.get()["lineups"][0]["channels"], [3, 1, 2])
        status, result, _ = self.request("POST", "/api/channels/auto-number", {"start": 500, "step": 10})
        self.assertEqual(result["numbers"], {"3": "500", "1": "510", "2": "520"})
        status, result, _ = self.request("POST", "/api/channels/auto-number", {"start": 1, "step": 1, "ids": [2, 3, 1]})
        self.assertEqual(result["numbers"], {"2": "1", "3": "2", "1": "3"})   # in the order sent
        status, _, _ = self.request("POST", "/api/channels/bulk-update", {"ids": [1, 2], "set": {"enabled": False}})
        self.assertEqual(status, 200)
        self.assertEqual([c["enabled"] for c in self.store.get()["channels"]], [True, False, False])
        status, err, _ = self.request("POST", "/api/channels/bulk-update", {"ids": [1], "set": {"id": 9}})
        self.assertEqual(status, 400)
        status, err, _ = self.request("POST", "/api/channels/reorder", {"ids": [1, 1]})
        self.assertEqual(status, 400)
        status, _, _ = self.request("POST", "/api/channels/bulk-delete", {"ids": [1, 2]})
        self.assertEqual([c["id"] for c in self.store.get()["channels"]], [3])


class GuideAndLineupRoutes(ApiTestCase):
    def test_gracenote_proxy(self):
        status, err, _ = self.request("GET", "/api/gracenote/search?q=cnn")
        self.assertEqual(status, 400)
        self.assertIn("Channels DVR", err["error"])
        self.request("PUT", "/api/config", {"channels_dvr": {"url": "http://dvr:8089/"}})
        status, result, _ = self.request("GET", "/api/gracenote/search?q=cnn")
        self.assertEqual(result["results"][0]["station_id"], "10149")
        self.assertEqual(self.stubs["app.guide.gracenote"].calls[-1], ("http://dvr:8089", "cnn"))
        self.request("POST", "/api/channels", {"name": "CNN"})
        status, result, _ = self.request("GET", "/api/gracenote/suggest?channel=1&countries=USA:10001")
        self.assertEqual(result["result"][0]["cdvr"], "http://dvr:8089")
        status, err, _ = self.request("GET", "/api/gracenote/suggest?channel=1&countries=US")
        self.assertEqual(status, 400)
        status, result, _ = self.request("POST", "/api/gracenote/bulk-suggest", {"ids": [1]})
        self.assertEqual(result["results"]["1"]["candidates"][0]["name"], "CNN")

    def test_xmltv_ids(self):
        self.add_provider()
        os.makedirs(os.path.join(self.dir, "cache"), exist_ok=True)
        with open(os.path.join(self.dir, "cache", "xmltv-ids-p1.json"), "w") as handle:
            json.dump([{"id": "cnn.us", "name": "CNN"}, {"id": "fox.us", "names": ["FOX"]}], handle)
        status, result, _ = self.request("GET", "/api/xmltv/ids?provider=p1&q=fox")
        self.assertEqual(result["items"], [{"id": "fox.us", "name": "FOX"}])
        status, _, _ = self.request("GET", "/api/xmltv/ids")
        self.assertEqual(status, 400)

    def test_lineups_and_outputs(self):
        self.request("PUT", "/api/config", {"server": {"base_url": "http://coax:7780"}})
        self.request("POST", "/api/channels", {"name": "One"})
        status, lineup, _ = self.request("POST", "/api/lineups", {"name": "Kids TV"})
        self.assertEqual((status, lineup["id"]), (200, "kids-tv"))
        status, lineup, _ = self.request("PUT", "/api/lineups/kids-tv", {"channels": [1]})
        self.assertEqual(lineup["channels"], [1])
        status, err, _ = self.request("PUT", "/api/lineups/kids-tv", {"channels": [99]})
        self.assertEqual(status, 400)
        status, body, response = self.request("GET", "/output/m3u/kids-tv.m3u")
        self.assertEqual(status, 200)
        uuid = self.store.get()["channels"][0]["uuid"]
        self.assertIn(("http://coax:7780/stream/%s.ts" % uuid).encode(), body)
        status, body, _ = self.request("GET", "/output/xmltv/kids-tv.xml")
        self.assertIn(b"<tv", body)
        os.makedirs(os.path.join(self.dir, "cache"), exist_ok=True)
        with open(os.path.join(self.dir, "cache", "xmltv-kids-tv.xml"), "wb") as handle:
            handle.write(b"<tv><channel id='coax-1'/></tv>")
        status, body, _ = self.request("GET", "/output/xmltv/kids-tv.xml")
        self.assertEqual(body, b"<tv><channel id='coax-1'/></tv>")
        status, _, _ = self.request("GET", "/output/m3u/nope.m3u")
        self.assertEqual(status, 404)
        status, _, _ = self.request("DELETE", "/api/lineups/kids-tv")
        self.assertEqual(status, 200)
        status, listing, _ = self.request("GET", "/api/lineups")
        self.assertEqual([l["id"] for l in listing["lineups"]], ["main"])


class OneScreenSetup(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.refreshed_provider()
        self.request("POST", "/api/channels/bulk-add", {"streams": [{"provider": "p1", "stream_id": "1"},
                                                                  {"provider": "p1", "stream_id": "2"}]})

    def test_automatch_without_backups_module(self):
        sys.modules["app.backups"] = None   # import raises ImportError
        status, result, _ = self.request("POST", "/api/automatch", {"ids": [1, 2]})
        self.assertEqual(status, 200, result)
        self.assertEqual(result["1"]["guide"][0]["station_id"], "10149")
        self.assertEqual(result["2"]["backups"], [])
        status, _, _ = self.request("POST", "/api/automatch", {"ids": [99]})
        self.assertEqual(status, 404)

    def test_automatch_with_backups_module(self):
        backups = types.ModuleType("app.backups")
        seen = {}

        def suggest(channel, catalogs):
            seen["providers"] = sorted(catalogs)
            return [{"provider": "p1", "stream_id": "3", "name": "ESPN", "score": 0.5, "reason": "test"}]
        backups.suggest = suggest
        sys.modules["app.backups"] = backups
        status, result, _ = self.request("POST", "/api/automatch", {"ids": [1]})
        self.assertEqual(result["1"]["backups"][0]["stream_id"], "3")
        self.assertEqual(seen["providers"], ["p1"])

    def test_apply_is_atomic(self):
        status, result, _ = self.request("POST", "/api/channels/apply", {"channels": [
            {"id": 1, "guide": {"mode": "gracenote", "station_id": "10149"},
             "streams": [{"provider": "p1", "stream_id": "1"}, {"provider": "p1", "stream_id": "3"}]},
            {"id": 2, "guide": {"mode": "none"}}]})
        self.assertEqual(status, 200, result)
        self.assertEqual(result["updated"], [1, 2])
        ch = self.store.get()["channels"][0]
        self.assertEqual((ch["guide"]["station_id"], len(ch["streams"])), ("10149", 2))
        before = self.store.snapshot()
        status, err, _ = self.request("POST", "/api/channels/apply", {"channels": [
            {"id": 1, "guide": {"mode": "xmltv"}}, {"id": 2, "guide": {"mode": "bogus"}}]})
        self.assertEqual(status, 400)
        self.assertEqual(self.store.get(), before)   # nothing saved
        status, err, _ = self.request("POST", "/api/channels/apply", {"channels": [{"id": 1}, {"id": 1}]})
        self.assertEqual(status, 400)


class XmltvSourcesAndLineupGuide(ApiTestCase):
    def test_sources_crud(self):
        status, src, _ = self.request("POST", "/api/xmltv/sources", {"name": "EPG", "url": "http://epg/x.xml.gz"})
        self.assertEqual((status, src["id"], src["refresh_hours"]), (200, "x1", 24))
        status, err, _ = self.request("POST", "/api/xmltv/sources", {"name": "Bad", "url": "nope"})
        self.assertEqual(status, 400)
        status, src, _ = self.request("PUT", "/api/xmltv/sources/x1", {"refresh_hours": 6})
        self.assertEqual(src["refresh_hours"], 6)
        self.request("POST", "/api/channels", {"name": "A", "guide": {"mode": "xmltv", "xmltv_id": "a",
                                                                     "xmltv_provider": "x1"}})
        status, ids, _ = self.request("GET", "/api/xmltv/ids?provider=x1")
        self.assertEqual(status, 200)
        status, listing, _ = self.request("GET", "/api/xmltv/sources")
        self.assertEqual(len(listing["sources"]), 1)
        status, result, _ = self.request("DELETE", "/api/xmltv/sources/x1")
        self.assertEqual(result["channels_cleared"], 1)
        self.assertEqual(self.store.get()["channels"][0]["guide"]["xmltv_provider"], "")

    def test_lineup_guide(self):
        status, lineups, _ = self.request("GET", "/api/lineups")
        self.assertEqual(lineups["lineups"][0]["guide"], "per-channel")
        status, lineup, _ = self.request("POST", "/api/lineups", {"name": "NFL", "guide": "xmltv"})
        self.assertEqual(lineup["guide"], "xmltv")
        status, lineup, _ = self.request("PUT", "/api/lineups/nfl", {"guide": "both"})
        self.assertEqual(lineup["guide"], "both")
        status, err, _ = self.request("PUT", "/api/lineups/nfl", {"guide": "none"})
        self.assertEqual(status, 400)


class StatusAndLog(ApiTestCase):
    def test_status_lines_and_log(self):
        self.add_provider()
        status, st, _ = self.request("GET", "/api/status")
        self.assertEqual(status, 200)
        self.assertEqual(st["lines"][0]["used"], 1)
        self.assertEqual(st["lines"][0]["channels"], [{"id": 1, "name": "One"}])
        self.hub.line_status = lambda: [{"provider_id": "p1", "line_id": "l1", "used": 1, "max": 2,
                                         "channels": [{"id": 7, "name": "Seven"}]}]
        status, st, _ = self.request("GET", "/api/status")
        self.assertEqual(st["lines"][0]["channels"], [{"id": 7, "name": "Seven"}])
        self.assertEqual(st["lines"][0]["max"], 2)
        handler = self.app.log_buffer
        logger = servermod.logging.getLogger("coax.test")
        logger.addHandler(handler)
        try:
            for i in range(60):
                logger.warning("line %d", i)
        finally:
            logger.removeHandler(handler)
        status, tail, _ = self.request("GET", "/api/log?limit=5")
        self.assertEqual([l["msg"] for l in tail["lines"]], ["line %d" % i for i in range(55, 60)])
        status, tail2, _ = self.request("GET", "/api/log?since=%d" % tail["last_seq"])
        self.assertEqual(tail2["lines"], [])
        self.assertEqual(len(handler.records), 50)


class InternalAndStream(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.add_provider()
        status, channel, _ = self.request("POST", "/api/channels",
                                          {"name": "One", "streams": [{"provider": "p1", "stream_id": "5"}]})
        self.channel = channel

    def test_internal_routes(self):
        status, body, _ = self.request("GET", "/internal/channels/1/streams/")
        self.assertEqual(body, [{"url": "http://backup/1"}])
        status, body, _ = self.request("GET", "/internal/channels/1/")
        self.assertEqual(body["uuid"], self.channel["uuid"])
        status, _, _ = self.request("GET", "/internal/channels/42")
        self.assertEqual(status, 404)
        self.app.hub = None
        status, body, _ = self.request("GET", "/internal/channels/1/streams")
        self.assertEqual(body, [{"url": "http://host:80/live/u/secret/5.ts"}])

    def test_internal_rejects_non_local(self):
        saved = servermod.LOCAL_ADDRESSES
        servermod.LOCAL_ADDRESSES = ()
        try:
            status, body, _ = self.request("GET", "/internal/channels/1")
        finally:
            servermod.LOCAL_ADDRESSES = saved
        self.assertEqual(status, 403)

    def test_stream(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", "/stream/%s.ts" % self.channel["uuid"])
        response = conn.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Content-Type"), "video/mp2t")
        self.assertIsNone(response.getheader("Content-Length"))
        self.assertEqual(response.getheader("Connection"), "close")
        body = response.read()
        self.assertEqual(len(body), 188 * 10 * 5)
        conn.close()
        self._wait(lambda: len(self.hub.closed) == 1)

    def test_stream_unknown_and_disabled(self):
        status, _, _ = self.request("GET", "/stream/nope.ts")
        self.assertEqual(status, 503)
        self.request("PUT", "/api/channels/1", {"enabled": False})
        status, _, _ = self.request("GET", "/stream/%s.ts" % self.channel["uuid"])
        self.assertEqual(status, 503)

    def test_stream_client_disconnect(self):
        self.hub.chunks = [b"G" * 188 * 1000] * 2000   # long enough to outlast the client
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        sock.sendall(("GET /stream/%s.ts HTTP/1.1\r\nHost: x\r\n\r\n" % self.channel["uuid"]).encode())
        self.assertIn(b"200", sock.recv(4096))
        sock.close()
        self._wait(lambda: len(self.hub.closed) == 1)
        status, body, _ = self.request("GET", "/healthz")
        self.assertEqual(body, b"ok")

    def _wait(self, condition, seconds=10):
        deadline = time.time() + seconds
        while time.time() < deadline:
            if condition():
                return
            time.sleep(0.05)
        self.fail("condition not met in time")


if __name__ == "__main__":
    unittest.main()


class XmltvWordSearchTest(unittest.TestCase):
    def test_partial_phrase_matches_by_words(self):
        from app.server import search_xmltv_items
        items = [
            {"id": "loc.CBS (WBBM) Chicago, IL", "name": "US| CBS 2 CHICAGO IL (WBBM)"},
            {"id": "NFLNetwork.us", "name": "US| NFL NETWORK HD"},
            {"id": "FOX32WFLD.us", "name": "ENG| FOX CHICAGO (WFLD)"},
        ]
        got = [i["id"] for i in search_xmltv_items(items, "Chicago Bears")]
        self.assertEqual(set(got), {"loc.CBS (WBBM) Chicago, IL", "FOX32WFLD.us"})
        self.assertEqual(search_xmltv_items(items, "cbs chicago")[0]["id"], "loc.CBS (WBBM) Chicago, IL")
        self.assertEqual(search_xmltv_items(items, "nfl net")[0]["id"], "NFLNetwork.us")
        self.assertEqual(search_xmltv_items(items, "zzz"), [])


class XmltvContextRankTest(unittest.TestCase):
    def test_channel_name_and_group_rank_results(self):
        from app.server import search_xmltv_items
        items = [
            {"id": "NFLNetwork.us", "name": "US| NFL NETWORK HD"},
            {"id": "loc.CBS (WBBM) Chicago, IL", "name": "US| CBS 2 CHICAGO IL (WBBM)"},
            {"id": "FOX32WFLD.us", "name": "ENG| FOX CHICAGO (WFLD)"},
        ]
        ctx = "CBS BEARS CHICAGO IL NFL PACKAGE"
        self.assertEqual(search_xmltv_items(items, "chicago", ctx)[0]["id"], "loc.CBS (WBBM) Chicago, IL")
        self.assertEqual(search_xmltv_items(items, "", ctx)[0]["id"], "loc.CBS (WBBM) Chicago, IL")
        self.assertEqual(search_xmltv_items(items, "bears nfl", ctx)[0]["id"], "NFLNetwork.us")
