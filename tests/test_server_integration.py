"""Integration of app.server with the real providers/guide modules, against a fake Xtream server on 127.0.0.1."""

import http.client
import importlib
import json
import os
import shutil
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app import config as configmod
from app import server as servermod

try:
    catalog = importlib.import_module("app.providers.catalog")
    guide = importlib.import_module("app.guide")
    importlib.import_module("app.guide.output")
except Exception:  # noqa: BLE001 - other agents' modules may not be present yet
    catalog = None


class FakeXtream(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if "get_live_categories" in self.path:
            data = [{"category_id": "1", "category_name": "US| NEWS"}, {"category_id": "2", "category_name": "US| SPORTS"}]
        elif "get_live_streams" in self.path:
            data = [{"stream_id": 11, "name": "CNN", "category_id": "1", "stream_icon": "", "epg_channel_id": "cnn.us"},
                    {"stream_id": 12, "name": "ESPN", "category_id": "2", "stream_icon": "", "epg_channel_id": ""}]
        else:
            data = {"user_info": {"auth": 1, "status": "Active", "exp_date": None, "max_connections": "2",
                                  "active_cons": "0"}, "server_info": {}}
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class FastServer(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self):   # skip the slow reverse DNS lookup in HTTPServer.server_bind
        import socketserver
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


@unittest.skipIf(catalog is None, "providers/guide modules not available")
class RealModules(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.saved = (catalog.DATA_DIR, guide.CACHE_DIR, configmod.DATA_DIR)
        catalog.DATA_DIR = self.dir
        guide.CACHE_DIR = os.path.join(self.dir, "cache")
        configmod.DATA_DIR = self.dir
        self.fake = FastServer(("127.0.0.1", 0), FakeXtream)
        threading.Thread(target=self.fake.serve_forever, daemon=True).start()
        self.store = configmod.ConfigStore(os.path.join(self.dir, "config.json"))
        self.store.load()
        self.app = servermod.App(self.store, None, servermod.RingLogHandler(50))
        self.httpd = servermod.make_server(self.app, "127.0.0.1", 0)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.fake.shutdown()
        self.fake.server_close()
        catalog.DATA_DIR, guide.CACHE_DIR, configmod.DATA_DIR = self.saved
        shutil.rmtree(self.dir)

    def request(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.httpd.server_address[1], timeout=20)
        conn.request(method, path, body=json.dumps(body).encode() if body is not None else None,
                     headers={"Content-Type": "application/json"})
        response = conn.getresponse()
        raw = response.read()
        conn.close()
        if response.getheader("Content-Type", "").startswith("application/json"):
            return response.status, json.loads(raw)
        return response.status, raw

    def test_provider_to_m3u(self):
        server = "http://127.0.0.1:%d" % self.fake.server_address[1]
        status, provider = self.request("POST", "/api/providers", {
            "name": "Fake", "type": "xtream", "server": server,
            "lines": [{"username": "u", "password": "pw", "max_streams": 1}]})
        self.assertEqual(status, 200, provider)
        status, result = self.request("POST", "/api/providers/p1/test")
        self.assertTrue(result["ok"], result)
        status, result = self.request("POST", "/api/providers/p1/refresh?wait=1")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["streams"], 2)
        status, groups = self.request("GET", "/api/providers/p1/groups")
        self.assertEqual({g["name"]: g["count"] for g in groups["groups"]}, {"US| NEWS": 1, "US| SPORTS": 1})
        self.request("PUT", "/api/providers/p1/groups", {"groups": ["US| NEWS"]})
        status, page = self.request("GET", "/api/catalog/streams")
        self.assertEqual([i["name"] for i in page["items"]], ["CNN"])
        status, added = self.request("POST", "/api/channels/bulk-add", {"streams": [{"provider": "p1", "stream_id": "11"}]})
        self.assertEqual(added["created"], [1])
        self.request("PUT", "/api/config", {"server": {"base_url": "http://coax:7780"}})
        status, body = self.request("GET", "/output/m3u/main.m3u")
        self.assertEqual(status, 200)
        uuid = self.store.get()["channels"][0]["uuid"]
        self.assertIn(("http://coax:7780/stream/%s.ts" % uuid).encode(), body)
        status, body = self.request("GET", "/internal/channels/1/streams")
        self.assertEqual(body, [{"url": server + "/live/u/pw/11.ts"}])


if __name__ == "__main__":
    unittest.main()
