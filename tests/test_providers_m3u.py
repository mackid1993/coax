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
        with self.assertRaises(ProviderError):
            m3u.apply_line_rewrite(url, bad)

    def test_line_rewrite_backreference_styles(self):
        url = "http://host/live/user1/pass1/5.ts"
        named = {"search": r"live/(?P<u>\w+)/(?P<p>\w+)/", "replace": "live/$<p>/$<u>/"}
        self.assertEqual(m3u.apply_line_rewrite(url, named), "http://host/live/pass1/user1/5.ts")
        python = {"search": r"live/(\w+)/(\w+)/", "replace": r"live/\2/\1/"}
        self.assertEqual(m3u.apply_line_rewrite(url, python), "http://host/live/pass1/user1/5.ts")
        whole = {"search": r"\d+\.ts$", "replace": "x$&$$"}
        self.assertEqual(m3u.apply_line_rewrite(url, whole), "http://host/live/user1/pass1/x5.ts$")

    def test_line_rewrite_no_match_or_too_long_raises(self):
        url = "http://host/live/user1/pass1/5.ts"
        with self.assertRaises(ProviderError):
            m3u.apply_line_rewrite(url, {"id": "l2", "search": "/nomatch/", "replace": "/x/"})
        with self.assertRaises(ProviderError):
            m3u.apply_line_rewrite(url, {"id": "l2", "search": "a" * 501, "replace": ""})

    def test_stream_format_for_url(self):
        f = m3u.stream_format_for_url
        self.assertEqual(f("http://h/live/u/p/1.m3u8?token=abc"), "hls")
        self.assertEqual(f("http://h/x/chan.ts"), "ts")
        self.assertEqual(f("http://h/u/p/123"), "ts")  # Xtream live without extension
        self.assertEqual(f("http://h/stream/chan.mp4"), "")
        self.assertEqual(f("http://h/play?id=1"), "")


class StreamIdTests(unittest.TestCase):
    def entry(self, url, name="", tvg_id=""):
        return {"url": url, "name": name or url, "tvg_id": tvg_id}

    def test_xtream_numeric_id_from_path(self):
        self.assertEqual(m3u.xtream_id("http://h:8080/live/user/pass/1234.ts"), "1234")
        self.assertEqual(m3u.xtream_id("http://h/user/pass/1234"), "1234")
        self.assertEqual(m3u.xtream_id("http://h/live/user/pass/77.m3u8?t=1"), "77")
        self.assertEqual(m3u.xtream_id("http://h/a/b/c/1234.ts"), "")
        self.assertEqual(m3u.candidate_ids(self.entry("http://h/live/u/p/42.ts", "X"))[0], "42")

    def test_ids_survive_credential_and_token_rotation(self):
        pairs = [
            ("http://h/live/userA/passA/42.ts", "http://h/live/userB/passB/42.ts"),
            ("http://h/userA/passA/42", "http://h/userB/passB/42"),
            ("http://cdn/hls/news.m3u8?token=aaa111", "http://cdn/hls/news.m3u8?token=bbb222"),
            ("http://cdn/a1b2c3d4e5f6a7b8c9/news.m3u8", "http://cdn/f9e8d7c6b5a4f3e2d1/news.m3u8"),
        ]
        for old, new in pairs:
            for name in ("", "News"):  # with and without an EXTINF name
                self.assertEqual(m3u.candidate_ids(self.entry(old, name))[0],
                                 m3u.candidate_ids(self.entry(new, name))[0], (old, new, name))

    def test_tvg_id_and_name_identity(self):
        a = m3u.candidate_ids(self.entry("http://x/1.ts?k=1", "CNN", "cnn.us"))[0]
        b = m3u.candidate_ids(self.entry("http://y/other.ts", "CNN", "cnn.us"))[0]
        c = m3u.candidate_ids(self.entry("http://x/1.ts?k=1", "CNN HD", "cnn.us"))[0]
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)

    def test_normalized_url_keeps_plain_paths(self):
        self.assertEqual(m3u.normalized_url("HTTP://user:pw@Host:81/live/news.ts?t=1#f"),
                         "http://host:81/live/news.ts")
        self.assertNotEqual(m3u.normalized_url("http://h/a/news.ts"), m3u.normalized_url("http://h/a/sport.ts"))


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
        self.assertEqual(self.fake.requests[0]["headers"]["User-Agent"], "TiviMate/5.1.6 (Android 12)")
        first = result["streams"][0]
        self.assertEqual(first["stream_id"], "1")  # Xtream style path id
        self.assertEqual(first["format"], "ts")

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
        # A line whose pattern does not match is skipped, not served with line 1's URL.
        with self.assertRaises(ProviderError):
            providers.stream_url(provider, {"id": "l3", "search": "/nomatch/", "replace": "/x/"}, sid)
        self.assertEqual(providers.stream_url(provider, provider["lines"][0], "2"), url)
        self.assertEqual(providers.xmltv_url(provider), "")

    def test_rotated_tokens_keep_ids_across_refresh(self):
        def playlist(token, cred):
            return ("#EXTM3U\n"
                    "#EXTINF:-1 tvg-id=\"a.us\" group-title=\"News\",A\n"
                    "http://cdn.example/hls/a.m3u8?token=%s\n"
                    "#EXTINF:-1 group-title=\"News\",B\n"
                    "http://p.example/live/%s/%s/77.ts\n"
                    "http://cdn.example/raw/c.ts?token=%s\n" % (token, cred, cred, token)).encode()
        provider = make_provider(self.fake.url + "/list.m3u")
        self.fake.set("/list.m3u", (200, {}, playlist("t1", "user1")))
        first = catalog.refresh(provider)
        self.fake.set("/list.m3u", (200, {}, playlist("t2", "user2")))
        second = catalog.refresh(provider)
        ids = [s["stream_id"] for s in first["streams"]]
        self.assertEqual(ids, [s["stream_id"] for s in second["streams"]])
        self.assertEqual(len(set(ids)), 3)
        self.assertIn("77", ids)
        self.assertEqual((second["added"], second["removed"], second["updated"]), ([], [], 3))
        self.assertEqual([s["format"] for s in second["streams"]], ["hls", "ts", "ts"])
        self.assertEqual(providers.stream_url(provider, provider["lines"][0], ids[0]),
                         "http://cdn.example/hls/a.m3u8?token=t2")

    def test_duplicate_names_get_distinct_ids(self):
        body = (b"#EXTM3U\n#EXTINF:-1,Same\nhttp://h/a.ts\n#EXTINF:-1,Same\nhttp://h/b.ts\n"
                b"#EXTINF:-1,Same\nhttp://h/a.ts?x=1\n")
        self.fake.set("/dup.m3u", (200, {}, body))
        streams = m3u.fetch_catalog(make_provider(self.fake.url + "/dup.m3u"))["streams"]
        self.assertEqual(len(streams), 3)
        self.assertEqual(len({s["stream_id"] for s in streams}), 3)

    def test_legacy_url_hash_ids_still_resolve_and_count_as_renamed(self):
        provider = make_provider(self.fake.url + "/list.m3u")
        url = "http://stream.example/live/u/p/2.ts"
        old = {"fetched_at": catalog.now_iso(), "groups": ["Sports"],
               "streams": [{"stream_id": m3u.stream_id_for(url), "name": "ESPN", "group": "Sports",
                            "logo": "", "tvg_id": "espn.us", "url": url}]}
        catalog.atomic_write_json(catalog.cache_path("m1"), old)
        self.fake.set("/list.m3u", (200, {}, fixture_bytes("playlist.m3u")))
        result = catalog.refresh(provider)
        self.assertEqual(result["renamed"], {m3u.stream_id_for(url): "2"})
        self.assertNotIn("2", result["added"])
        self.assertEqual(result["removed"], [])
        self.assertEqual(providers.stream_url(provider, provider["lines"][0], m3u.stream_id_for(url)), url)

    def test_stream_format_helper_and_usable_lines(self):
        self.fake.set("/list.m3u", (200, {}, b"#EXTM3U\nhttp://h/a.m3u8\nhttp://h/b.mp4\n"))
        provider = make_provider(self.fake.url + "/list.m3u", format="ts")
        streams = catalog.refresh(provider)["streams"]
        self.assertEqual(providers.stream_format(provider, streams[0]), "hls")
        self.assertEqual(providers.stream_format(provider, streams[1]["stream_id"]), "ts")  # provider default
        self.assertEqual(providers.stream_format(provider, streams[0]["stream_id"]), "hls")
        self.assertEqual(providers.stream_format({"format": "hls"}, {"stream_id": "1"}), "hls")
        provider["lines"] = [{"id": "a"}, {"id": "b", "enabled": False}, {"id": "c", "enabled": True}]
        self.assertEqual([l["id"] for l in providers.usable_lines(provider)], ["a", "c"])

    def test_total_deadline_aborts_trickling_download_and_keeps_cache(self):
        import socket
        import threading
        import time

        self.fake.set("/list.m3u", (200, {}, fixture_bytes("playlist.m3u")))
        provider = make_provider(self.fake.url + "/list.m3u")
        catalog.refresh(provider)
        before = catalog.load("m1")

        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        stop = threading.Event()

        def trickle():
            conn, _ = listener.accept()
            conn.recv(4096)
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100000\r\n\r\n#EXTM3U\n")
            while not stop.is_set():
                try:
                    conn.sendall(b"#")
                except OSError:
                    break
                time.sleep(0.05)
            conn.close()

        threading.Thread(target=trickle, daemon=True).start()
        provider["m3u_url"] = "http://127.0.0.1:%d/slow.m3u" % listener.getsockname()[1]
        old_deadline = m3u.DOWNLOAD_DEADLINE
        m3u.DOWNLOAD_DEADLINE = 0.5
        try:
            started = time.monotonic()
            with self.assertRaises(ProviderError) as ctx:
                catalog.refresh(provider)
            self.assertLess(time.monotonic() - started, 5)
            self.assertIn("deadline", str(ctx.exception))
        finally:
            m3u.DOWNLOAD_DEADLINE = old_deadline
            stop.set()
            listener.close()
        self.assertEqual(catalog.load("m1")["fetched_at"], before["fetched_at"])

    def test_test_connection_counts_streams(self):
        self.fake.set("/list.m3u", (200, {}, fixture_bytes("playlist.m3u")))
        result = providers.test_connection(make_provider(self.fake.url + "/list.m3u"))
        self.assertEqual(result, {"ok": True, "streams": 6})


if __name__ == "__main__":
    unittest.main()


class StabilityTests(unittest.TestCase):
    setUp = DownloadTests.setUp
    tearDown = DownloadTests.tearDown

    def test_duplicate_name_entries_keep_their_ids_when_reordered(self):
        a = b'#EXTINF:-1 tvg-id="x" group-title="News",Same\nhttp://h1/one.ts\n'
        b = b'#EXTINF:-1 tvg-id="x" group-title="News",Same\nhttp://h2/two.ts\n'
        self.fake.set("/list.m3u", (200, {}, b"#EXTM3U\n" + a + b))
        provider = make_provider(self.fake.url + "/list.m3u")
        first = {s["url"]: s["stream_id"] for s in catalog.refresh(provider)["streams"]}
        self.fake.set("/list.m3u", (200, {}, b"#EXTM3U\n" + b + a))
        second = {s["url"]: s["stream_id"] for s in catalog.refresh(provider)["streams"]}
        self.assertEqual(first, second)

    def test_empty_answer_keeps_the_good_catalog(self):
        self.fake.set("/list.m3u", (200, {}, fixture_bytes("playlist.m3u")))
        provider = make_provider(self.fake.url + "/list.m3u")
        catalog.refresh(provider)
        before = catalog.load("m1")
        from unittest import mock
        with mock.patch.object(catalog, "fetch", lambda p: {"groups": [], "streams": []}):
            with self.assertRaises(ProviderError):
                catalog.refresh(provider)
        self.assertEqual(catalog.load("m1")["streams"], before["streams"])

    def test_channel_ref_uses_the_streams_own_format(self):
        self.fake.set("/list.m3u", (200, {}, b"#EXTM3U\nhttp://h/a.m3u8\nhttp://h/b.ts\n"))
        provider = make_provider(self.fake.url + "/list.m3u", format="ts")
        streams = catalog.refresh(provider)["streams"]
        ref = {"provider": "m1", "stream_id": streams[0]["stream_id"]}
        self.assertEqual(providers.stream_format(provider, ref), "hls")


class HlsFirstTests(unittest.TestCase):
    """HLS comes first: MPEG-TS only when nothing else can play the stream."""

    def test_format_choice(self):
        hls, ts = {"format": "hls"}, {"format": "ts"}
        live = {"url": "http://h/live/u/p/123.ts", "format": "ts"}
        bare = {"url": "http://h/live/u/p/123", "format": "ts"}
        plain = {"url": "http://h/channel/abc.ts", "format": "ts"}
        playlist = {"url": "http://h/x/index.m3u8", "format": "hls"}
        self.assertEqual(providers.stream_format(hls, live), "hls")      # the HLS engine asks for 123.m3u8
        self.assertEqual(providers.stream_format(hls, bare), "hls")
        self.assertEqual(providers.stream_format(ts, live), "ts")        # the provider was set to MPEG-TS
        self.assertEqual(providers.stream_format(hls, plain), "ts")      # no HLS form: only TS can play it
        self.assertEqual(providers.stream_format(ts, playlist), "hls")   # a playlist is always HLS
        self.assertEqual(providers.stream_format({}, {"url": "http://h/x.mp4"}), "")