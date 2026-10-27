import os
import shutil
import tempfile
import unittest

from app import providers
from app.providers import catalog, m3u, net
from app.providers.net import ProviderError
from tests.test_providers_support import FakeProvider, fixture_bytes, gzipped


def make_provider(m3u_url, **extra):
    provider = {
        "id": "m1", "name": "M3U", "type": "m3u", "server": "", "m3u_url": m3u_url, "xmltv_url": "",
        "user_agent": "", "refresh_hours": 24, "groups": ["News"],
        "lines": [{"id": "l1", "username": "", "password": "", "max_streams": 0,
                   "search": "", "replace": ""}],
    }
    provider.update(extra)
    return provider


class ParseTests(unittest.TestCase):
    def setUp(self):
        self.parsed = m3u.parse_text(fixture_bytes("playlist.m3u").decode("utf-8-sig"))
        self.entries = self.parsed["entries"]

    def test_header_attributes(self):
        self.assertEqual(self.parsed["header"]["x-tvg-url"], "http://epg.example/guide.xml")
        self.assertEqual(self.parsed["header"]["url-tvg"], "http://epg.example/g2.xml")

    def test_attributes_and_commas_in_names(self):
        first = self.entries[0]
        self.assertEqual(first["name"], "CNN, International HD")
        self.assertEqual(first["tvg_name"], "CNN, International")
        self.assertEqual(first["tvg_id"], "cnn.us")
        self.assertEqual(first["logo"], "http://logo/cnn.png")
        self.assertEqual(first["group"], "News")
        self.assertEqual(first["url"], "http://stream.example/live/u/p/1.ts")

    def test_unquoted_single_quoted_and_mixed_case_keys(self):
        second = self.entries[1]
        self.assertEqual(second["tvg_id"], "espn.us")
        self.assertEqual(second["group"], "Sports")
        self.assertEqual(second["url"], "http://stream.example/live/u/p/2.ts")  # EXTVLCOPT skipped

    def test_missing_attributes_extgrp_and_fallback_names(self):
        self.assertEqual(self.entries[2]["name"], "No Attributes")
        self.assertEqual(self.entries[2]["group"], "Misc")
        self.assertEqual(self.entries[3]["name"], "Only TVG Name")
        bare = self.entries[4]
        self.assertEqual(bare["name"], bare["url"])
        self.assertEqual(bare["group"], "Uncategorized")
        self.assertEqual(len(self.entries), 6)

    def test_split_extinf(self):
        self.assertEqual(m3u.split_extinf('-1 tvg-name="a,b" x="y",Name, with comma'),
                         ('-1 tvg-name="a,b" x="y"', "Name, with comma"))
        self.assertEqual(m3u.split_extinf("-1"), ("-1", ""))

    def test_stream_id_is_stable_sha1_prefix(self):
        sid = m3u.stream_id_for("http://a/b.ts")
        self.assertEqual(len(sid), 16)
        self.assertEqual(sid, m3u.stream_id_for(" http://a/b.ts "))
        self.assertNotEqual(sid, m3u.stream_id_for("http://a/c.ts"))

    def test_chunked_bytes_with_bom_crlf_and_gzip(self):
        data = fixture_bytes("playlist.m3u")
        expected = [e["url"] for e in self.entries]
        for body in (data, gzipped(data)):
            # One-byte chunks split every CRLF and multi-byte character.
            chunks = [body[i:i + 1] for i in range(len(body))]
            parsed = m3u.parse_lines(m3u.iter_text_lines(chunks))
            self.assertEqual([e["url"] for e in parsed["entries"]], expected)
            self.assertEqual(parsed["entries"][0]["name"], "CNN, International HD")
        self.assertEqual(len(m3u.parse_bytes(data)["entries"]), 6)

    def test_line_rewrite(self):
        url = "http://host/live/user1/pass1/5.ts"
        self.assertEqual(m3u.apply_line_rewrite(url, {"search": "", "replace": "x"}), url)
        line = {"search": r"/user1/pass1/", "replace": "/user2/pass2/"}
        self.assertEqual(m3u.apply_line_rewrite(url, line), "http://host/live/user2/pass2/5.ts")
        line = {"search": r"live/(\w+)/(\w+)/", "replace": "live/$2/$1/"}
        self.assertEqual(m3u.apply_line_rewrite(url, line), "http://host/live/pass1/user1/5.ts")
        bad = {"id": "l9", "search": "(", "replace": ""}
        with self.assertLogs("coax.providers", "WARNING"):
            self.assertEqual(m3u.apply_line_rewrite(url, bad), url)


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeProvider()
        self.tmp = tempfile.mkdtemp()
        self._data_dir = catalog.DATA_DIR
        catalog.DATA_DIR = self.tmp
        self._sleep = net.sleep
        net.sleep = lambda seconds: None

    def tearDown(self):
        catalog.DATA_DIR = self._data_dir
        net.sleep = self._sleep
        self.fake.close()
        shutil.rmtree(self.tmp)

    def test_plain_download_dedupes_by_url(self):
        self.fake.set("/list.m3u", (200, {}, fixture_bytes("playlist.m3u")))
        result = m3u.fetch_catalog(make_provider(self.fake.url + "/list.m3u", user_agent="UA/2"))
        self.assertEqual(len(result["streams"]), 5)  # duplicate URL dropped
        self.assertEqual(result["groups"], ["News", "Sports", "Misc", "Uncategorized"])
        self.assertEqual(self.fake.requests[0]["headers"]["User-Agent"], "UA/2")
        first = result["streams"][0]
        self.assertEqual(first["stream_id"], m3u.stream_id_for(first["url"]))

    def test_gzip_body_and_content_encoding(self):
        body = gzipped(fixture_bytes("playlist.m3u"))
        self.fake.set("/list.m3u.gz", (200, {}, body))
        self.assertEqual(len(m3u.fetch_catalog(make_provider(self.fake.url + "/list.m3u.gz"))["streams"]), 5)
        self.fake.set("/enc.m3u", (200, {"Content-Encoding": "gzip"}, body))
        self.assertEqual(len(m3u.fetch_catalog(make_provider(self.fake.url + "/enc.m3u"))["streams"]), 5)

    def test_html_and_empty_raise(self):
        self.fake.set("/html", (200, {}, b"<!DOCTYPE html><html>login</html>"))
        with self.assertRaises(ProviderError):
            m3u.fetch_catalog(make_provider(self.fake.url + "/html"))
        self.fake.set("/empty", (200, {}, b"#EXTM3U\r\n"))
        with self.assertRaises(ProviderError):
            m3u.fetch_catalog(make_provider(self.fake.url + "/empty"))
        with self.assertRaises(ProviderError):
            m3u.fetch_catalog(make_provider(""))

    def test_http_error_retries_then_raises(self):
        self.fake.set("/down", (503, {}, b"busy"))
        with self.assertRaises(ProviderError):
            m3u.fetch_catalog(make_provider(self.fake.url + "/down"))
        self.assertEqual(len(self.fake.requests), 3)

    def test_stream_url_uses_cached_url_and_line_rewrite(self):
        self.fake.set("/list.m3u", (200, {}, fixture_bytes("playlist.m3u")))
        provider = make_provider(self.fake.url + "/list.m3u")
        catalog.refresh(provider)
        url = "http://stream.example/live/u/p/2.ts"
        sid = m3u.stream_id_for(url)
        self.assertEqual(providers.stream_url(provider, provider["lines"][0], sid), url)
        line = {"id": "l2", "search": "/u/p/", "replace": "/u2/p2/"}
        self.assertEqual(providers.stream_url(provider, line, sid),
                         "http://stream.example/live/u2/p2/2.ts")
        with self.assertRaises(ProviderError):
            providers.stream_url(provider, line, "missing")
        self.assertEqual(providers.xmltv_url(provider), "")

    def test_test_connection_counts_streams(self):
        self.fake.set("/list.m3u", (200, {}, fixture_bytes("playlist.m3u")))
        result = providers.test_connection(make_provider(self.fake.url + "/list.m3u"))
        self.assertEqual(result, {"ok": True, "streams": 6})


if __name__ == "__main__":
    unittest.main()
