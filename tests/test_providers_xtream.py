import unittest

from app import providers
from app.providers import net, xtream
from app.providers.net import ProviderError
from tests.test_providers_support import FakeProvider


def make_provider(server, user_agent=""):
    return {
        "id": "p1", "name": "Test", "type": "xtream", "server": server, "user_agent": user_agent,
        "xmltv_url": "", "refresh_hours": 24, "groups": ["US| NEWS"],
        "lines": [{"id": "l1", "username": "user1", "password": "secret1", "max_streams": 1},
                  {"id": "l2", "username": "user2", "password": "p@ss/2", "max_streams": 2}],
    }


class XtreamTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeProvider()
        self.provider = make_provider(self.fake.url)
        self.sleeps = []
        self._sleep = net.sleep
        net.sleep = self.sleeps.append

    def tearDown(self):
        net.sleep = self._sleep
        self.fake.close()

    def test_fetch_catalog_maps_categories_and_normalizes(self):
        self.fake.json("get_live_categories", "xtream_categories.json")
        self.fake.json("get_live_streams", "xtream_streams.json")
        result = xtream.fetch_catalog(self.provider)
        by_id = {s["stream_id"]: s for s in result["streams"]}
        # Movie, duplicate, missing id and non-dict entries are dropped.
        self.assertEqual(sorted(by_id), ["101", "102", "103", "104", "105"])
        self.assertEqual(by_id["101"], {"stream_id": "101", "name": "CNN HD", "group": "US| NEWS",
                                        "logo": "http://logo/cnn.png", "tvg_id": "cnn.us", "num": "1"})
        self.assertEqual(by_id["102"]["group"], "US| SPORTS")  # numeric category_id
        self.assertEqual(by_id["102"]["logo"], "")             # null icon
        self.assertEqual(by_id["103"]["group"], "Category 3")  # null category name
        self.assertEqual(by_id["103"]["name"], "Stream 103")   # null name
        self.assertEqual(by_id["104"]["group"], "Uncategorized")
        self.assertEqual(by_id["105"]["group"], "US| SPORTS")  # category_ids fallback
        self.assertEqual(result["groups"], ["US| NEWS", "US| SPORTS", "Category 3", "Uncategorized"])
        self.assertNotIn("url", by_id["101"])

    def test_empty_categories_beside_categorized_streams_is_an_error(self):
        # A hiccup must not replace a good catalog with one where every stream is Uncategorized.
        self.fake.set("get_live_categories", (200, {}, b"[]"))
        self.fake.json("get_live_streams", "xtream_streams.json")
        with self.assertRaises(net.ProviderError):
            xtream.fetch_catalog(self.provider)
        # A panel that has no categories at all still works.
        self.fake.set("get_live_streams", (200, {}, b'[{"stream_id": 1, "name": "A", "stream_type": "live"}]'))
        self.assertEqual(xtream.fetch_catalog(self.provider)["groups"], ["Uncategorized"])

    def test_uses_first_line_and_user_agent(self):
        self.provider["user_agent"] = "MyAgent/1.0"
        self.fake.json("get_live_categories", "xtream_categories.json")
        self.fake.json("get_live_streams", "xtream_streams.json")
        xtream.fetch_catalog(self.provider)
        for req in self.fake.requests:
            self.assertEqual(req["query"]["username"], "user1")
            self.assertEqual(req["query"]["password"], "secret1")
            self.assertEqual(req["headers"].get("User-Agent"), "TiviMate/5.1.6 (Android 12)")

    def test_default_user_agent(self):
        self.fake.json("get_live_categories", "xtream_categories.json")
        xtream.get_live_categories(self.provider)
        self.assertEqual(self.fake.requests[0]["headers"]["User-Agent"], net.DEFAULT_USER_AGENT)

    def test_empty_lists_and_objects(self):
        for body in (b"[]", b"{}", b"null", b""):
            self.fake.set("get_live_streams", (200, {}, body))
            self.assertEqual(xtream.get_live_streams(self.provider), [], body)

    def test_dict_keyed_list(self):
        self.fake.set("get_live_categories",
                      (200, {}, b'{"0": {"category_id": 7, "category_name": "X"}}'))
        self.assertEqual(xtream.get_live_categories(self.provider),
                         [{"category_id": "7", "category_name": "X"}])

    def test_html_error_page_raises(self):
        self.fake.set("get_live_streams", (200, {"Content-Type": "text/html"},
                                           b"<html><body>Access denied secret1</body></html>"))
        with self.assertRaises(ProviderError) as ctx:
            xtream.get_live_streams(self.provider)
        self.assertIn("HTML", str(ctx.exception))
        self.assertNotIn("secret1", str(ctx.exception))

    def test_invalid_json_raises(self):
        self.fake.set("get_live_streams", (200, {}, b"{not json"))
        with self.assertRaises(ProviderError):
            xtream.get_live_streams(self.provider)

    def test_retries_5xx_with_backoff_then_succeeds(self):
        self.fake.set("get_live_categories", (503, {}, b"busy"), (502, {}, b"bad"),
                      (200, {}, b'[{"category_id": "1", "category_name": "A"}]'))
        cats = xtream.get_live_categories(self.provider)
        self.assertEqual(cats, [{"category_id": "1", "category_name": "A"}])
        self.assertEqual(self.sleeps, [2.0, 4.0])

    def test_retries_exhausted_raises_clear_error(self):
        self.fake.set("get_live_categories", (500, {}, b"oops"))
        with self.assertRaises(ProviderError) as ctx:
            xtream.get_live_categories(self.provider)
        self.assertIn("after 3 attempts", str(ctx.exception))
        self.assertIn("HTTP 500", str(ctx.exception))
        self.assertEqual(len(self.fake.requests), 3)

    def test_4xx_not_retried(self):
        self.fake.set("get_live_categories", (404, {}, b"no"))
        with self.assertRaises(ProviderError):
            xtream.get_live_categories(self.provider)
        self.assertEqual(len(self.fake.requests), 1)
        self.fake.requests.clear()
        self.fake.set("get_live_categories", (401, {}, b"no"))
        with self.assertRaises(ProviderError) as ctx:
            xtream.get_live_categories(self.provider)
        self.assertIn("rejected", str(ctx.exception))
        self.assertEqual(len(self.fake.requests), 1)

    def test_connection_error_raises_without_password(self):
        provider = make_provider("http://127.0.0.1:1")
        with self.assertRaises(ProviderError) as ctx:
            xtream.get_live_categories(provider)
        self.assertNotIn("secret1", str(ctx.exception))

    def test_auth_zero_raises(self):
        self.fake.set("get_live_streams", (200, {}, b'{"user_info": {"auth": 0}}'))
        with self.assertRaises(ProviderError):
            xtream.get_live_streams(self.provider)

    def test_account_info(self):
        self.fake.json("account", "xtream_account.json")
        info = xtream.account_info(self.provider)
        self.assertEqual(info["status"], "Active")
        self.assertEqual(info["max_connections"], 2)
        self.assertEqual(info["created_at"], "1600000000")
        self.assertNotIn("action", self.fake.requests[0]["query"])
        result = providers.test_connection(self.provider)
        self.assertTrue(result["ok"])

    def test_test_connection_failure_never_raises(self):
        self.fake.set("account", (200, {}, b"<html>bad</html>"))
        result = providers.test_connection(self.provider)
        self.assertFalse(result["ok"])
        self.assertTrue(result["error"])

    def test_stream_url_per_line(self):
        provider = make_provider("http://host:8080/")
        line1, line2 = provider["lines"]
        self.assertEqual(providers.stream_url(provider, line1, "2151800"),
                         "http://host:8080/live/user1/secret1/2151800.ts")
        self.assertEqual(providers.stream_url(provider, line2, 99),
                         "http://host:8080/live/user2/p@ss%2F2/99.ts")

    def test_base_url_normalization(self):
        self.assertEqual(xtream.base_url("host:80"), "http://host:80")
        self.assertEqual(xtream.base_url("https://h/player_api.php?x=1"), "https://h")
        with self.assertRaises(ProviderError):
            xtream.base_url("")

    def test_xmltv_url(self):
        provider = make_provider("http://host:80")
        self.assertEqual(providers.xmltv_url(provider),
                         "http://host:80/xmltv.php?username=user1&password=secret1")
        provider["xmltv_url"] = "http://epg/x.xml"
        self.assertEqual(providers.xmltv_url(provider), "http://epg/x.xml")

    def test_disabled_first_line_is_not_used_for_api(self):
        self.provider["lines"][0]["enabled"] = False
        self.fake.json("get_live_categories", "xtream_categories.json")
        xtream.get_live_categories(self.provider)
        self.assertEqual(self.fake.requests[0]["query"]["username"], "user2")
        self.assertEqual([l["id"] for l in providers.usable_lines(self.provider)], ["l2"])
        self.provider["lines"][1]["enabled"] = False
        with self.assertRaises(ProviderError):
            xtream.get_live_categories(self.provider)

    def test_api_deadline_aborts_trickling_response(self):
        import socket
        import threading
        import time

        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        stop = threading.Event()

        def trickle():
            conn, _ = listener.accept()
            conn.recv(4096)
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100000\r\n\r\n[")
            while not stop.is_set():
                try:
                    conn.sendall(b" ")
                except OSError:
                    break
                time.sleep(0.05)
            conn.close()

        threading.Thread(target=trickle, daemon=True).start()
        provider = make_provider("http://127.0.0.1:%d" % listener.getsockname()[1])
        old = xtream.API_DEADLINE
        xtream.API_DEADLINE = 0.5
        try:
            started = time.monotonic()
            with self.assertRaises(ProviderError) as ctx:
                xtream.get_live_streams(provider)
            self.assertLess(time.monotonic() - started, 5)
            self.assertIn("deadline", str(ctx.exception))
        finally:
            xtream.API_DEADLINE = old
            stop.set()
            listener.close()

    def test_redact(self):
        self.assertEqual(net.redact("http://h/?username=u&password=abc&x=1"),
                         "http://h/?username=u&password=********&x=1")


if __name__ == "__main__":
    unittest.main()
