"""Tests for app.channels_dvr against a fake Channels DVR server on localhost."""

import json
import os as _os

_os.environ.setdefault("COAX_SKIP_URL_CHECK", "1")
import os
import shutil
import socketserver
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock
from http.server import BaseHTTPRequestHandler, HTTPServer

from app import channels_dvr as dvr
from app import config as configmod
from app import server as servermod
from app.guide import events, output


class FakeDvr:
    """Records every request. Sources live in self.sources; /dvr/lineups maps device ids to lineup ids."""

    def __init__(self):
        self.requests = []
        self.sources = {}
        self.lineups = {}
        self.fail = None          # status code to answer every request with
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def _handle(self, method):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                body = json.loads(raw) if raw else None
                fake.requests.append((method, self.path, body, self.headers.get("User-Agent")))
                status, data = fake.answer(method, self.path, body)
                payload = json.dumps(data).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):
                self._handle("GET")

            def do_PUT(self):
                self._handle("PUT")

            def do_POST(self):
                self._handle("POST")

            def log_message(self, *args):
                pass

        class Server(HTTPServer):
            def server_bind(self):
                # Skip HTTPServer's getfqdn, which can stall for many seconds on macOS.
                socketserver.TCPServer.server_bind(self)
                self.server_name, self.server_port = "127.0.0.1", self.server_address[1]

        self.server = Server(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def answer(self, method, path, body):
        if self.fail:
            return self.fail, {"error": "DVR is unhappy"}
        prefix = "/providers/m3u/sources/"
        if path.startswith(prefix):
            rest = path[len(prefix):]
            if rest.endswith("/refresh") and method == "POST":
                return 200, {}
            if method == "PUT":
                self.sources[rest] = body
                # The DVR makes a lineup for the new source.
                self.lineups.setdefault("M3U-" + rest, "X-" + rest)
                return 200, body
            if method == "GET":
                if rest in self.sources:
                    return 200, self.sources[rest]
                return 404, {"error": "not found"}
        if path == "/dvr/lineups" and method == "GET":
            return 200, self.lineups
        if path.startswith("/dvr/lineups/") and method == "PUT":
            return 200, {}
        return 404, {"error": "no route"}

    def calls(self):
        """(method, path) pairs, ignoring status reads."""
        return [(m, p) for m, p, _b, _ua in self.requests]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def make_config(dvr_url):
    cfg = configmod.default_config()
    cfg["server"]["base_url"] = "http://coax.test:7780"
    cfg["channels_dvr"]["url"] = dvr_url
    cfg["providers"] = [dict(configmod.default_provider(), id="p1", name="One", server="http://p1.test",
                             max_streams=3,
                             lines=[dict(configmod.default_line(), id="l1", username="u", password="pw",
                                         max_streams=2),
                                    dict(configmod.default_line(), id="l2", username="v", password="pw",
                                         max_streams=2)])]
    cfg["channels"] = [
        dict(configmod.default_channel(), id=1, uuid="u-1", number="101", name="CNN",
             streams=[{"provider": "p1", "stream_id": "1"}],
             guide={"mode": "both", "station_id": "10142", "xmltv_id": "cnn.us", "xmltv_provider": ""}),
        dict(configmod.default_channel(), id=2, uuid="u-2", number="102", name="ESPN",
             streams=[{"provider": "p1", "stream_id": "2"}],
             guide={"mode": "none", "station_id": "", "xmltv_id": "", "xmltv_provider": ""}),
    ]
    cfg["next_channel_id"] = 3
    cfg["lineups"] = [{"id": "cable", "name": "Cable", "guide": "per-channel", "channels": [1]}]
    return cfg


class DvrTestCase(unittest.TestCase):
    def setUp(self):
        self.fake = FakeDvr()
        self.tmp = tempfile.mkdtemp()
        self.store = configmod.ConfigStore(os.path.join(self.tmp, "config.json"))
        configmod.atomic_write(self.store.path, json.dumps(make_config(self.fake.url)).encode())
        self.store.load()
        self._delay = dvr.LOOKUP_DELAY
        dvr.LOOKUP_DELAY = 0

    def tearDown(self):
        dvr.LOOKUP_DELAY = self._delay
        self.fake.close()
        shutil.rmtree(self.tmp, ignore_errors=True)


class AddTests(DvrTestCase):
    def add(self, **opts):
        return dvr.add_or_update(self.store, "cable", opts)

    def test_source_key_and_default_limit(self):
        self.assertEqual(dvr.source_key("my-cable_1"), "coaxmycable1")
        # Lines allow 4, the provider caps it at 3.
        self.assertEqual(dvr.default_limit(self.store.get(), "cable"), "3")

    def test_unlimited_provider_gives_empty_limit(self):
        def mutate(cfg):
            cfg["providers"][0]["max_streams"] = 0
            cfg["providers"][0]["lines"][0]["max_streams"] = 0
        self.store.update(mutate)
        self.assertEqual(dvr.default_limit(self.store.get(), "cable"), "")

    def test_gracenote_only_payload(self):
        result = self.add(guide="gracenote")
        body = self.fake.sources["coaxcable"]
        self.assertEqual(body["guide_preference"], "gracenote")
        self.assertEqual(body["xmltv_url"], "")
        self.assertEqual(body["url"], "http://coax.test:7780/output/m3u/cable.m3u")
        self.assertEqual(body["type"], "MPEG-TS")
        self.assertEqual(body["source"], "URL")
        self.assertEqual(body["limit"], "3")
        self.assertEqual(body["name"], "coax Cable")
        self.assertEqual(body["logos"], "guide")
        self.assertTrue(all(isinstance(v, str) for v in body.values()))
        self.assertTrue(result["created"])
        self.assertEqual(self.store.get()["lineups"][0]["guide"], "gracenote")

    def test_xmltv_only_payload(self):
        self.add(guide="xmltv")
        body = self.fake.sources["coaxcable"]
        self.assertEqual(body["guide_preference"], "xmltv")
        self.assertEqual(body["xmltv_url"], "http://coax.test:7780/output/xmltv/cable.xml")
        self.assertEqual(self.store.get()["lineups"][0]["guide"], "xmltv")
        # An xmltv lineup never sends station ids.
        self.assertNotIn("tvc-guide-stationid", output.m3u(self.store.get(), "cable"))

    def test_gracenote_with_xmltv_fallback_payload(self):
        self.add(guide="gracenote_xmltv", numbering="ignore", start_number="5000", logos="", name="My cable",
                 limit="")
        body = self.fake.sources["coaxcable"]
        self.assertEqual(body["guide_preference"], "gracenote")
        self.assertEqual(body["xmltv_url"], "http://coax.test:7780/output/xmltv/cable.xml")
        self.assertEqual((body["numbering"], body["start_number"], body["logos"], body["name"], body["limit"]),
                         ("ignore", "5000", "", "My cable", ""))
        lineup = self.store.get()["lineups"][0]
        self.assertEqual(lineup["guide"], "both")
        self.assertEqual(lineup["dvr"]["key"], "coaxcable")
        self.assertEqual(lineup["dvr"]["start_number"], "5000")
        self.assertTrue(lineup["dvr"]["added_at"])
        self.assertIn('tvc-guide-stationid="10142"', output.m3u(self.store.get(), "cable"))

    def test_request_sequence_and_user_agent(self):
        result = self.add(guide="gracenote")
        calls = self.fake.calls()
        self.assertEqual(calls, [
            ("GET", "/providers/m3u/sources/coaxcable"),
            ("PUT", "/providers/m3u/sources/coaxcable"),
            ("POST", "/providers/m3u/sources/coaxcable/refresh"),
            ("GET", "/dvr/lineups"),
            ("PUT", "/dvr/lineups/X-coaxcable"),
        ])
        self.assertTrue(all(ua == "coax" for *_rest, ua in self.fake.requests))
        self.assertTrue(result["guide_started"])
        self.assertEqual(dvr.status(self.store.get(), "cable")["in_dvr"], True)
        # Second time it is an update.
        self.assertFalse(self.add(guide="gracenote")["created"])

    def test_missing_guide_lineup_is_not_an_error(self):
        original = self.fake.answer

        def no_lineups(method, path, body):
            if path == "/dvr/lineups":
                return 200, {}
            return original(method, path, body)
        self.fake.answer = no_lineups
        result = self.add(guide="gracenote")
        self.assertFalse(result["guide_started"])
        self.assertEqual(self.fake.calls().count(("GET", "/dvr/lineups")), dvr.LOOKUP_TRIES)

    def test_dvr_error_and_missing_settings(self):
        self.fake.fail = 500
        with self.assertRaises(dvr.DvrError) as ctx:
            self.add(guide="gracenote")
        self.assertEqual(ctx.exception.status, 502)
        self.assertIn("DVR is unhappy", ctx.exception.message)
        self.assertNotIn("dvr", self.store.get()["lineups"][0])

        def no_base(cfg):
            cfg["server"]["base_url"] = ""
        self.store.update(no_base)
        with self.assertRaises(dvr.DvrError) as ctx:
            self.add(guide="gracenote")
        self.assertEqual(ctx.exception.status, 400)
        self.assertIn("base URL", ctx.exception.message)

    def test_status_when_missing(self):
        st = dvr.status(self.store.get(), "cable")
        self.assertEqual((st["in_dvr"], st["source"], st["error"]), (False, None, ""))
        self.fake.fail = 500
        self.assertFalse(dvr.status(self.store.get(), "cable")["in_dvr"])


class SyncerTests(DvrTestCase):
    def setUp(self):
        super().setUp()
        self.xmltv_calls = []
        self.syncer = dvr.Syncer(self.store, debounce=0.2, xmltv_refresh=self.xmltv_calls.append).register()
        self.syncer.add("cable", {"guide": "gracenote_xmltv"})
        self.fake.requests.clear()

    def tearDown(self):
        self.syncer.stop()
        super().tearDown()

    def wait_idle(self):
        """Wait out the debounce and the sync itself."""
        time.sleep(0.5)
        with self.syncer.lock:
            pass

    def test_add_does_not_trigger_a_second_sync(self):
        self.wait_idle()
        self.assertEqual(self.fake.calls(), [])

    def test_channel_change_refreshes_source_only(self):
        def rename(cfg):
            cfg["channels"][0]["name"] = "CNN HD"
        self.store.update(rename)
        self.store.update(rename)    # two quick changes, one sync
        self.wait_idle()
        self.assertEqual(self.fake.calls(), [("POST", "/providers/m3u/sources/coaxcable/refresh")])
        self.assertEqual(self.xmltv_calls, [])
        self.assertTrue(self.syncer.last_result("cable")["ok"])

    def test_new_channel_refreshes_and_downloads_guide(self):
        def add_channel(cfg):
            cfg["lineups"][0]["channels"].append(2)
        self.store.update(add_channel)
        self.wait_idle()
        self.assertEqual(self.fake.calls(), [
            ("POST", "/providers/m3u/sources/coaxcable/refresh"),
            ("GET", "/dvr/lineups"),
            ("PUT", "/dvr/lineups/X-coaxcable"),
        ])
        self.assertEqual(len(self.xmltv_calls), 1)   # both lineup: XMLTV rebuilt first
        self.assertTrue(self.syncer.last_result("cable")["guide_started"])

    def test_adding_a_disabled_channel_does_nothing(self):
        def add_disabled(cfg):
            cfg["channels"][1]["enabled"] = False
            cfg["lineups"][0]["channels"].append(2)
        self.store.update(add_disabled)
        self.wait_idle()
        self.assertEqual(self.fake.calls(), [])
        self.assertEqual(self.xmltv_calls, [])

    def test_guide_id_change_downloads_guide(self):
        def new_station(cfg):
            cfg["channels"][0]["guide"].update(mode="xmltv", xmltv_id="cnn2.us")
        self.store.update(new_station)
        self.wait_idle()
        self.assertIn(("PUT", "/dvr/lineups/X-coaxcable"), self.fake.calls())

    def test_new_event_on_a_slot_downloads_guide(self):
        with mock.patch.object(events, "fingerprint", lambda cfg, lineup_id: [(1, "event", "abc", "2026-10-30")]):
            self.syncer.on_change(None, None)     # what a provider refresh does
            self.wait_idle()
        self.assertIn(("PUT", "/dvr/lineups/X-coaxcable"), self.fake.calls())
        self.assertEqual(len(self.xmltv_calls), 1)

    def test_unrelated_change_does_nothing(self):
        def unrelated(cfg):
            cfg["transcode"] = "720p30"
            cfg["channels"][1]["name"] = "ESPN 2"     # channel 2 is not in the lineup
        self.store.update(unrelated)
        self.wait_idle()
        self.assertEqual(self.fake.calls(), [])

    def test_errors_do_not_raise(self):
        self.fake.fail = 500

        def rename(cfg):
            cfg["channels"][0]["name"] = "CNN HD"
        self.store.update(rename)
        self.wait_idle()
        result = self.syncer.last_result("cable")
        self.assertFalse(result["ok"])
        self.assertIn("DVR is unhappy", result["error"])
        # The DVR is back: the pending change is pushed on the next sync.
        self.fake.fail = None
        self.fake.requests.clear()
        self.syncer.sync_all()
        self.assertIn(("POST", "/providers/m3u/sources/coaxcable/refresh"), self.fake.calls())

    def test_sync_now(self):
        result = self.syncer.sync_now("cable")
        self.assertTrue(result["ok"] and result["refreshed"] and result["guide_started"])
        self.assertEqual(self.fake.calls()[0], ("POST", "/providers/m3u/sources/coaxcable/refresh"))

    def test_failing_listener_does_not_break_config(self):
        self.store.listeners.insert(0, lambda old, new: 1 / 0)

        def rename(cfg):
            cfg["channels"][0]["name"] = "CNN HD"
        self.store.update(rename)
        self.assertEqual(self.store.get()["channels"][0]["name"], "CNN HD")


class RouteTests(DvrTestCase):
    def setUp(self):
        super().setUp()
        self.app = servermod.App(self.store, None, servermod.RingLogHandler(50))
        self.httpd = servermod.make_server(self.app, "127.0.0.1", 0)
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, json.loads(exc.read())

    def test_add_status_and_sync(self):
        status, st = self.call("GET", "/api/lineups/cable/dvr")
        self.assertEqual(status, 200)
        self.assertFalse(st["in_dvr"])
        self.assertEqual(st["defaults"]["guide"], "gracenote_xmltv")
        self.assertEqual(st["defaults"]["limit"], "3")
        status, result = self.call("POST", "/api/lineups/cable/dvr", {"guide": "xmltv"})
        self.assertEqual(status, 200, result)
        self.assertTrue(result["created"])
        status, st = self.call("GET", "/api/lineups/cable/dvr")
        self.assertTrue(st["in_dvr"])
        self.assertTrue(st["last_sync"]["ok"])
        status, result = self.call("POST", "/api/lineups/cable/dvr/sync", {})
        self.assertEqual(status, 200)
        self.assertTrue(result["refreshed"])

    def test_errors(self):
        status, err = self.call("POST", "/api/lineups/cable/dvr", {"guide": "bogus"})
        self.assertEqual(status, 400)
        self.fake.fail = 500
        status, err = self.call("POST", "/api/lineups/cable/dvr", {"guide": "xmltv"})
        self.assertEqual(status, 502)
        self.assertIn("DVR is unhappy", err["error"])

        def no_dvr(cfg):
            cfg["channels_dvr"]["url"] = ""
        self.store.update(no_dvr)
        status, err = self.call("POST", "/api/lineups/cable/dvr", {"guide": "xmltv"})
        self.assertEqual(status, 400)
        self.assertIn("Channels DVR URL", err["error"])
        status, st = self.call("GET", "/api/lineups/cable/dvr")
        self.assertEqual((status, st["dvr_url_set"]), (200, False))
        self.assertEqual(self.call("GET", "/api/lineups/nope/dvr")[0], 404)


if __name__ == "__main__":
    unittest.main()
