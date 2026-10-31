"""Video on demand: names, the Stream Files and DVR collections, and the passthrough proxy's one-connection rule."""

import http.client
import os
import random
import re
import shutil
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app import hub as hubmod
from app import vod as vodlib
from app import vodproxy
from app.providers import vod as vodcat
from tests.fixtures.hub.support import channel, config, fake_stream_url, provider

SIZE = 12 * 1024 * 1024
DATA = random.Random(7).randbytes(SIZE)
OTHER = random.Random(8).randbytes(SIZE - 300_000)   # title 43: other bytes, another size


def data_for(path):
    return OTHER if path.endswith("/43.mkv") else DATA


class FakePanel(ThreadingHTTPServer):
    """An Xtream panel: /movie/... redirects to a CDN path that serves DATA with ranges. Counts open CDN
    connections; with `strict`, a second one at once answers 509 like a one-connection line."""

    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), PanelHandler)
        self.lock = threading.Lock()
        self.open = 0
        self.max_open = 0
        self.requests = []
        self.strict = True
        self.busy_next = 0          # answer this many requests with 509 first
        self.header_delay = 0       # seconds before answering (a slow panel)
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def url(self):
        return "http://127.0.0.1:%d" % self.server_address[1]


class PanelHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        srv = self.server
        if self.path.startswith("/movie/") or self.path.startswith("/series/"):
            self.send_response(302)
            self.send_header("Location", "/cdn/" + self.path.rsplit("/", 1)[-1])
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        time.sleep(srv.header_delay)
        with srv.lock:
            srv.requests.append(self.headers.get("Range"))
            if srv.busy_next:
                srv.busy_next -= 1
                busy = True
            else:
                busy = srv.strict and srv.open >= 1
            if not busy:
                srv.open += 1
                srv.max_open = max(srv.max_open, srv.open)
        if busy:
            self.send_response(509)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        data = data_for(self.path)
        size = len(data)
        try:
            match = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range") or "")
            if match and not match.group(1):
                self.send_response(500)   # like a real CDN: no "last N bytes" ranges
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if match:
                start = int(match.group(1))
                end = min(int(match.group(2)), size - 1) if match.group(2) else size - 1
                self.send_response(206)
                self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
            else:
                start, end = 0, size - 1
                self.send_response(200)
            self.send_header("Content-Type", "video/x-matroska")
            self.send_header("Content-Length", str(end - start + 1))
            self.end_headers()
            pos = start
            while pos <= end:
                n = min(64 * 1024, end - pos + 1)
                self.wfile.write(data[pos:pos + n])
                pos += n
                time.sleep(0.002)
        except OSError:
            pass
        finally:
            self.close_connection = True
            with srv.lock:
                srv.open -= 1


class ProxyServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, plays):
        super().__init__(("127.0.0.1", 0), ProxyHandler)
        self.plays = plays
        threading.Thread(target=self.serve_forever, daemon=True).start()


class ProxyHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        m = re.match(r"^/vod/([^/]+)/(m|e)/([A-Za-z0-9_-]+)\.(\w+)$", self.path)
        self.server.plays.serve(self, *m.groups())

    do_HEAD = do_GET


def get(port, path, rng=None, read=None):
    """GET through the proxy: (status, body read, connection still open for more)."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    conn.request("GET", path, headers={"Range": rng} if rng else {})
    resp = conn.getresponse()
    body = resp.read(read) if read else resp.read()
    return resp.status, body, conn, resp


class ProxyTests(unittest.TestCase):
    def setUp(self):
        vodproxy.LINGER = 0.5
        vodproxy.RETRY_FOR = 4.0
        self.panel = FakePanel()
        self.cfg = config([provider("p1", 1)], [channel(1, [("p1", "s1")])])
        self.cfg["providers"][0]["server"] = self.panel.url
        self.hub = hubmod.Hub(lambda: self.cfg, stream_url=fake_stream_url)
        self.plays = vodproxy.Plays(self.hub, lambda: self.cfg)
        self.proxy = ProxyServer(self.plays)
        self.port = self.proxy.server_address[1]

    def tearDown(self):
        self.proxy.shutdown()
        self.panel.shutdown()

    def wait_idle(self):
        end = time.monotonic() + 5
        while time.monotonic() < end and (self.hub.line_status()[0]["used"] or self.panel.open):
            time.sleep(0.05)

    def test_apple_tv_start_and_fast_forward_never_open_two_provider_connections(self):
        path = "/vod/p1/m/42.mkv"
        # start: the beginning, then the index at the end while the first is still open, then the main read
        status, body, c1, r1 = get(self.port, path, "bytes=0-", read=256 * 1024)
        self.assertEqual((status, body), (206, DATA[:256 * 1024]))
        status, body, c2, _ = get(self.port, path, "bytes=%d-" % (SIZE - 1016))
        self.assertEqual((status, body), (206, DATA[SIZE - 1016:]))   # from memory
        c1.close()
        status, body, c3, r3 = get(self.port, path, "bytes=5749-", read=1024 * 1024)
        self.assertEqual((status, body), (206, DATA[5749:5749 + 1024 * 1024]))
        # fast forward: a burst of seeks, three at a time open, each a fresh index read plus a jump
        results = {}

        def seek(i, offset):
            try:
                st, data, conn, _ = get(self.port, path, "bytes=%d-" % offset, read=64 * 1024)
                results[i] = (st, data, offset)
                time.sleep(0.3)
                conn.close()
            except (OSError, http.client.HTTPException):
                results[i] = None
        threads = []
        for i in range(12):
            get(self.port, path, "bytes=%d-" % (SIZE - 80000))[2].close()
            t = threading.Thread(target=seek, args=(i, 1_000_000 + i * 700_000))
            t.start()
            threads.append(t)
            time.sleep(0.12)
        for t in threads:
            t.join()
        c3.close()
        last = results[11]
        self.assertEqual(last[0], 206)
        self.assertEqual(last[1], DATA[last[2]:last[2] + 64 * 1024])
        for res in results.values():
            if res and res[0] == 206:
                self.assertEqual(res[1], DATA[res[2]:res[2] + len(res[1])])
        self.assertEqual(self.panel.max_open, 1)
        self.wait_idle()
        self.assertEqual(self.hub.line_status()[0]["used"], 0)

    def test_last_bytes_range_from_the_player_becomes_an_explicit_one(self):
        status, body, conn, _ = get(self.port, "/vod/p1/m/5.mkv", "bytes=-1000")
        conn.close()
        self.assertEqual((status, body), (206, DATA[-1000:]))
        self.assertNotIn(True, [bool(r) and r.startswith("bytes=-") for r in self.panel.requests])

    def test_quick_switch_to_another_title_never_serves_the_old_titles_bytes(self):
        # 42 starts and reads its ending; the viewer backs out and starts 43 while 42's ending is still coming
        _s, _b, c42, _ = get(self.port, "/vod/p1/m/42.mkv", "bytes=0-", read=1000)
        t = threading.Thread(target=lambda: get(self.port, "/vod/p1/m/42.mkv", "bytes=%d-" % (SIZE - 2000)),
                             daemon=True)
        t.start()
        c42.close()
        status, body, c43, r43 = get(self.port, "/vod/p1/m/43.mkv", "bytes=0-", read=5000)
        self.assertEqual((status, body), (206, OTHER[:5000]))
        self.assertTrue(r43.getheader("Content-Range").endswith("/%d" % len(OTHER)))
        c43.close()
        status, body, conn, _ = get(self.port, "/vod/p1/m/43.mkv", "bytes=%d-" % (len(OTHER) - 1016))
        conn.close()
        self.assertEqual((status, body), (206, OTHER[-1016:]))
        t.join(5)
        self.assertEqual(self.panel.max_open, 1)

    def test_two_viewers_behind_one_address_get_their_own_plays(self):
        self.cfg["providers"][0]["lines"][0]["max_streams"] = 2
        self.panel.strict = False
        a = get(self.port, "/vod/p1/m/42.mkv", "bytes=100-", read=1000)
        b = get(self.port, "/vod/p1/m/43.mkv", "bytes=100-", read=1000)
        self.assertEqual((a[1], b[1]), (DATA[100:1100], OTHER[100:1100]))
        self.assertEqual(self.hub.line_status()[0]["used"], 2)
        a[2].close()
        b[2].close()

    def test_a_movie_cannot_take_back_the_line_a_tune_is_freeing(self):
        _s, _b, conn, _ = get(self.port, "/vod/p1/m/7.mp4", "bytes=0-", read=1000)
        self.assertTrue(self.hub._evict_vod({"p1"}, "Channel 1"))
        self.assertIsNone(self.hub.vod_hold("p1", "VOD: retry", lambda: None))
        session = self.hub._open({"id": 1, "uuid": "uuid-1", "name": "Channel 1", "streams": self.cfg["channels"][0]["streams"]},
                                 [(self.cfg["channels"][0]["streams"][0], self.cfg["providers"][0])], "dvr", "ua")
        self.assertIsNotNone(session)
        session.close()
        conn.close()

    def test_busy_answer_after_a_reconnect_is_retried(self):
        self.panel.busy_next = 2
        status, body, conn, _ = get(self.port, "/vod/p1/m/7.mp4", "bytes=100-", read=1000)
        conn.close()
        self.assertEqual((status, body), (206, DATA[100:1100]))

    def test_line_held_between_seeks_then_released(self):
        get(self.port, "/vod/p1/m/7.mp4", "bytes=0-", read=1000)[2].close()
        self.assertEqual(self.hub.line_status()[0]["used"], 1)
        self.assertEqual(self.hub.line_status()[0]["channels"][0]["name"], "VOD: movie 7")
        self.wait_idle()
        self.assertEqual(self.hub.line_status()[0]["used"], 0)

    def test_second_viewer_gets_503_on_a_full_line(self):
        _s, _b, conn, _ = get(self.port, "/vod/p1/m/7.mp4", "bytes=0-", read=1000)
        other = vodproxy.Plays(self.hub, lambda: self.cfg)   # same hub, a different address in effect
        play = other._play_for("10.0.0.9", self.cfg["providers"][0], ("movie", "8", "mkv"))
        self.assertIsNone(play)
        conn.close()

    def test_tune_takes_the_line_from_a_movie(self):
        _s, _b, conn, resp = get(self.port, "/vod/p1/m/7.mp4", "bytes=0-", read=1000)
        session = self.hub.open("uuid-1", "dvr", "ua")
        self.assertIsNotNone(session)
        end = time.monotonic() + 3
        while self.panel.open and time.monotonic() < end:   # the panel notices the close on its next write
            time.sleep(0.02)
        self.assertEqual(self.panel.open, 0)
        holders = self.hub.line_status()[0]
        self.assertEqual((holders["used"], holders["channels"][0]["name"]), (1, "Channel 1"))
        with self.assertRaises(Exception):
            while resp.read(65536):
                pass
            raise OSError("ended")
        conn.close()
        session.close()

    def test_tune_does_not_wait_long_for_a_movie_connection_still_opening(self):
        _s, _b, conn, _ = get(self.port, "/vod/p1/m/7.mp4", "bytes=0-", read=1000)
        self.panel.header_delay = 5
        seek = threading.Thread(target=lambda: self.assertRaises(Exception, get, self.port, "/vod/p1/m/7.mp4",
                                                                 "bytes=5000000-", 1000), daemon=True)
        seek.start()
        time.sleep(0.6)   # the seek is now waiting on the slow panel
        started = time.monotonic()
        session = self.hub.open("uuid-1", "dvr", "ua")
        self.assertIsNotNone(session)
        self.assertLess(time.monotonic() - started, 3.5)
        session.close()
        conn.close()
        self.panel.header_delay = 0

    def test_same_viewer_switching_title_keeps_the_line(self):
        get(self.port, "/vod/p1/m/7.mp4", "bytes=0-", read=1000)[2].close()
        status, body, conn, _ = get(self.port, "/vod/p1/e/9.mkv", "bytes=10-", read=1000)
        conn.close()
        self.assertEqual((status, body), (206, DATA[10:1010]))
        self.assertEqual(self.panel.max_open, 1)

    def test_head_answers_size(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("HEAD", "/vod/p1/m/7.mkv")
        resp = conn.getresponse()
        self.assertEqual((resp.status, resp.getheader("Content-Length"), resp.getheader("Accept-Ranges")),
                         (200, str(SIZE), "bytes"))
        conn.close()


class NameTests(unittest.TestCase):
    def test_titles(self):
        cases = {
            "EN - The King's Speech (2010)": ("The King's Speech", "2010"),
            "SE-4K - Rogue One: A Star Wars Story": ("Rogue One: A Star Wars Story", ""),
            "4KDV: Ode to the Spring (2022)": ("Ode to the Spring", "2022"),
            "4K - A Knight of the Seven Kingdoms (2026) (US)": ("A Knight of the Seven Kingdoms", "2026"),
            "|EN| The Matrix 1999": ("The Matrix", "1999"),
            "Blade Runner 2049": ("Blade Runner 2049", ""),
            "2012 (2009)": ("2012", "2009"),
            "Star Wars: Episode IV - A New Hope (1977) [MULTI]": ("Star Wars: Episode IV - A New Hope", "1977"),
        }
        for name, want in cases.items():
            self.assertEqual(vodcat.title_year(name), want, name)
        self.assertEqual(vodcat.title_year("Black Robe", "1991-09-05"[:4]), ("Black Robe", "1991"))

    def test_m3u_split(self):
        entries = [
            {"name": "CNN", "url": "http://h/live/u/p/1.ts", "group": "News"},
            {"name": "EN - Up (2009)", "url": "http://h/movie/u/p/55.mkv", "group": "Movies"},
            {"name": "Breaking Bad S01 E02", "url": "http://h/series/u/p/77.mp4", "group": "Shows"},
            {"name": "Some Film", "url": "http://cdn/x/film.mp4?t=1", "group": "Movies"},
        ]
        live, vod = vodcat.m3u_split(entries, lambda e: e["url"].rsplit("/", 1)[-1].split(".")[0])
        self.assertEqual([e["name"] for e in live], ["CNN"])
        self.assertEqual([m["name"] for m in vod["movies"]], ["EN - Up (2009)", "Some Film"])
        self.assertEqual(vod["series"][0]["name"], "Breaking Bad")
        ep = vod["episodes"][vod["series"][0]["id"]][0]
        self.assertEqual((ep["season"], ep["episode"], ep["id"]), (1, 2, "77"))


class EpisodeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.saved = (vodcat.catalog.DATA_DIR, vodcat.EPISODE_PAUSE, vodcat.xtream.get_series_episodes)
        vodcat.catalog.DATA_DIR = self.tmp
        vodcat.EPISODE_PAUSE = 0

    def tearDown(self):
        vodcat.catalog.DATA_DIR, vodcat.EPISODE_PAUSE, vodcat.xtream.get_series_episodes = self.saved
        shutil.rmtree(self.tmp)

    def test_throttled_answers_are_not_cached_and_end_the_run(self):
        calls = []

        def answer(provider, sid):
            calls.append(sid)
            if sid == "a":
                return [{"id": "1", "season": 1, "episode": 1, "title": "", "ext": "mkv"}]
            raise vodcat.ProviderError("Xtream get_series_info returned no series info")
        vodcat.xtream.get_series_episodes = answer
        provider = {"id": "p9", "type": "xtream"}
        shows = [{"id": sid, "modified": 5} for sid in "abcdefghij"]
        out = vodcat.episodes_for(provider, shows)
        self.assertEqual(len(out["a"]), 1)
        self.assertEqual(calls, list("abcdef"))          # five failures in a row end the run
        self.assertEqual(out["j"], [])
        info = {}
        vodcat.episodes_for(provider, shows, info=info)
        self.assertEqual((info["throttled"], info["left"]), (True, 9))
        vodcat.xtream.get_series_episodes = lambda p, sid: [{"id": sid, "season": 1, "episode": 1, "ext": "mp4"}]
        info = {}
        vodcat.episodes_for(provider, shows, max_fetch=3, info=info)   # a batch stops after three calls
        self.assertEqual((info["fetched"], info["left"], info["throttled"]), (3, 6, False))
        out = vodcat.episodes_for(provider, shows)       # the next run asks again for every failed one
        self.assertEqual(out["j"][0]["id"], "j")


class ShrinkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.saved = vodcat.catalog.DATA_DIR
        vodcat.catalog.DATA_DIR = self.tmp

    def tearDown(self):
        vodcat.catalog.DATA_DIR = self.saved
        shutil.rmtree(self.tmp)

    def test_a_short_answer_never_replaces_the_list_until_it_lasts_three_days(self):
        movies = [{"id": str(i), "name": "M %d" % i, "groups": ["A"]} for i in range(100)]
        vodcat.save("p1", {"movies": movies, "series": []})
        with self.assertRaises(vodcat.ProviderError):
            vodcat.save("p1", {"movies": movies[:30], "series": []})
        self.assertEqual(len(vodcat.load("p1")["movies"]), 100)
        groups = vodcat.cache_path("p1", "-groups")
        data = vodcat._load_json(groups)
        data["shrink_seen"] = time.time() - vodcat.SHRINK_ACCEPT - 1
        vodcat.catalog.atomic_write_json(groups, data)
        vodcat.save("p1", {"movies": movies[:30], "series": []})
        self.assertEqual(len(vodcat.load("p1")["movies"]), 30)


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.imports = os.path.join(self.tmp, "Imports")
        for sub in ("Movies", "TV", "Videos"):
            os.makedirs(os.path.join(self.imports, sub))
        os.makedirs(os.path.join(self.imports, "Movies", "Mine"))
        with open(os.path.join(self.imports, "Movies", "Mine", "Keep (2000).strm"), "w") as handle:
            handle.write("x")
        self.provider = {"id": "p2", "name": "TPS", "type": "xtream", "lines": [],
                         "vod": {"movies": ["Action", "4K"], "series": ["Drama"]}}
        self.vod = {
            "movies": [
                {"id": "1", "name": "EN - Up (2009)", "ext": "mkv", "groups": ["Action"]},
                {"id": "2", "name": "4K: Up (2009)", "ext": "mkv", "groups": ["4K"]},
                {"id": "3", "name": "Heat: Redux (1995)", "ext": "mp4", "groups": ["Action", "Comedy"]},
                {"id": "4", "name": "Other (2001)", "ext": "mp4", "groups": ["Comedy"]},
            ],
            "series": [{"id": "s1", "name": "Breaking Bad (2008)", "groups": ["Drama"]}],
        }
        self.episodes = {"s1": [{"id": "e2", "season": 1, "episode": 2, "ext": "mkv"},
                                {"id": "e1", "season": 1, "episode": 1, "ext": "mkv"}]}

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_plan(self):
        pl = vodlib.plan(self.provider, self.vod, self.episodes, "http://coax:7780")
        self.assertEqual(pl.visible, set())   # by default nothing shows in the main listings
        self.assertEqual(pl.files, {
            "Movies/.coax/p2/Up (2009)/Up (2009).strm": "http://coax:7780/vod/p2/m/1.mkv\n",
            "Movies/.coax/p2/Heat - Redux (1995)/Heat - Redux (1995).strm": "http://coax:7780/vod/p2/m/3.mp4\n",
            "TV/.coax/p2/Breaking Bad (2008)/Season 01/Breaking Bad (2008) - S01E01.strm": "http://coax:7780/vod/p2/e/e1.mkv\n",
            "TV/.coax/p2/Breaking Bad (2008)/Season 01/Breaking Bad (2008) - S01E02.strm": "http://coax:7780/vod/p2/e/e2.mkv\n",
        })
        self.assertEqual(pl.members[("movies", "4K")], ["Movies/.coax/p2/Up (2009)/Up (2009).strm"])   # the one copy of Up
        self.assertEqual(pl.members[("movies", "Action")], ["Movies/.coax/p2/Up (2009)/Up (2009).strm",
                                                            "Movies/.coax/p2/Heat - Redux (1995)/Heat - Redux (1995).strm"])
        self.assertEqual(pl.members[("series", "Drama")], ["coax-p2-s1"])
        self.assertEqual(pl.shows["coax-p2-s1"]["name"], "Breaking Bad")
        self.assertEqual(pl.meta["Movies/.coax/p2/Up (2009)/Up (2009).strm"]["title"], "Up")

    def test_item_bodies_carry_what_channels_requires(self):
        pl = vodlib.plan(self.provider, self.vod, self.episodes, "http://coax:7780")
        rel = "Movies/.coax/p2/Up (2009)/Up (2009).strm"
        body = vodlib.file_body(rel, pl.files[rel], dict(pl.meta[rel], tmdb="14160"), hidden=True)
        self.assertEqual((body["ImportPath"], body["Path"], body["GroupID"]),
                         ("Imports/Movies", ".coax/p2/Up (2009)/Up (2009).strm", "movies"))
        self.assertEqual(body["VideoURL"], "http://coax:7780/vod/p2/m/1.mkv")
        self.assertEqual(body["VisibilityMode"], "hidden")
        air = body["Airing"]
        for key in ("Source", "Title", "Time", "Duration", "Channel"):   # the DVR answers "invalid file" without
            self.assertTrue(air.get(key), key)
        self.assertEqual((air["MovieID"], air["ReleaseYear"]), ("tmdb/14160", 2009))
        rel = "TV/.coax/p2/Breaking Bad (2008)/Season 01/Breaking Bad (2008) - S01E02.strm"
        body = vodlib.file_body(rel, pl.files[rel], pl.meta[rel], hidden=False)
        self.assertEqual((body["ImportPath"], body["GroupID"], body["ImportGroup"]),
                         ("Imports/TV", "coax-p2-s1", "coax-p2-s1"))
        self.assertNotIn("VisibilityMode", body)
        self.assertEqual((body["Airing"]["SeasonNumber"], body["Airing"]["EpisodeNumber"]), (1, 2))

    def _fake_dvr(self, api_items=None):
        calls, made, sections, files = [], [], [], {}

        def dvr(cfg, method, path, body=None):
            calls.append((method, path, body))
            if method == "POST" and path == "/dvr/files":
                fid = str(100 + len(files))
                files[fid] = body
                return dict(body, ID=fid)
            if path.startswith("/api/v1/"):
                return api_items or []
            if method == "GET" and "/matches/tmdb/movies?q=Heat%3A%20Redux" in path:
                return [{"id": 6, "title": "Heat - Redux", "release_date": "1995-12-15", "overview": "A heist.",
                         "poster_path": "/h.jpg", "genre_ids": [80, 18]}]
            if method == "GET" and path.startswith("/dvr/groups/coax-p2-s1/matches/tmdb?q="):
                return [{"id": 1396, "name": "Breaking Bad", "first_air_date": "2008-01-20", "overview": "A teacher.",
                         "poster_path": "/p.jpg"}]
            if method == "GET" and "/matches/tmdb" in path:
                return []
            if path == "/dvr/collections/content":
                return [{"ID": c, "Name": "", "Items": []} for c in made]
            if path == "/dvr/custom_sections":
                return [{"ID": "9", "Rows": []}] if sections else []
            if method == "POST" and path == "/dvr/collections/content/new":
                made.append(str(len(made) + 1))
                return {"ID": made[-1]}
            if method == "POST" and path == "/dvr/custom_sections/new":
                sections.append(body)
                return {"ID": "9"}
            return {}
        return dvr, calls, sections, files

    def _library(self, dvr, cfg):
        class Store:
            def get(self):
                return cfg
        return vodlib.Library(Store(), self.tmp, imports_dir=self.imports, dvr_call=dvr)

    def _patched(self):
        orig = vodcat.load, vodcat.episodes_for
        vodcat.load = lambda pid: dict(self.vod, fetched_at=vodcat.catalog.now_iso())
        vodcat.episodes_for = lambda provider, series, **kw: self.episodes
        return orig

    def test_sync_creates_items_through_the_api_and_keeps_collections(self):
        dvr, calls, sections, files = self._fake_dvr()
        cfg = {"server": {"base_url": "http://coax:7780"}, "channels_dvr": {"url": "http://dvr:8089"},
               "providers": [self.provider]}
        lib = self._library(dvr, cfg)
        orig = self._patched()
        try:
            lib.sync("p2")
            st = lib.state["p2"]
            self.assertEqual(st.get("error"), "")
            self.assertTrue(os.path.isfile(os.path.join(self.imports, "Movies/.coax/p2/Up (2009)/Up (2009).strm")))
            for folder in ("Movies/.coax", "Movies/.coax/p2", "TV/.coax/p2/Breaking Bad (2008)/Season 01"):
                self.assertEqual(os.stat(os.path.join(self.imports, folder)).st_mode & 0o777, 0o777)
            self.assertEqual(os.stat(os.path.join(self.imports, "Movies/.coax/p2/Up (2009)/Up (2009).strm")).st_mode & 0o777,
                             0o666)
            self.assertFalse([p for m, p, b in calls if "scanner" in p])   # never the slow scanner
            posted = [b for m, p, b in calls if m == "POST" and p == "/dvr/files"]
            self.assertEqual(len(posted), 4)
            self.assertTrue(all(b.get("VisibilityMode") == "hidden" for b in posted))
            kinds = [b["GroupID"] == "movies" for b in posted]
            self.assertEqual(kinds, sorted(kinds, reverse=True))   # every movie before any episode
            # the show's first episode creates the group, then coax names it and hides it
            order = [(m, p) for m, p, b in calls if (p in ("/dvr/files", "/dvr/groups/coax-p2-s1")
                     or p.startswith("/dvr/groups/coax-p2-s1/")) and m != "GET"]
            first_ep = next(i for i, (m, p) in enumerate(order) if p == "/dvr/files" and
                            files[str(100 + sum(1 for x in order[:i] if x[1] == "/dvr/files"))].get("GroupID") != "movies")
            self.assertEqual(order[first_ep + 1], ("PUT", "/dvr/groups/coax-p2-s1"))
            named = next(b for m, p, b in calls if p == "/dvr/groups/coax-p2-s1")
            self.assertEqual((named["Name"], named["Categories"]), ("Breaking Bad", ["Show", "Series"]))
            # coax's own lookup filled what the provider lacked before Channels DVR got the show and the movie
            self.assertEqual((named["Summary"], named["Image"]), ("A teacher.", vodlib.POSTER % "/p.jpg"))
            heat = next(b for b in posted if b["Airing"]["Title"] == "Heat: Redux")
            self.assertEqual((heat["Airing"]["Summary"], heat["Airing"]["MovieID"], heat["Airing"]["Genres"]),
                             ("A heist.", "tmdb/6", ["Crime", "Drama"]))
            self.assertEqual(st["todo"]["g:coax-p2-s1"][0], "1396")
            self.assertIn(("PUT", "/dvr/groups/coax-p2-s1/visibility/hidden", None), calls)
            names = [b["Name"] for m, p, b in calls if p == "/dvr/collections/content/new"]
            self.assertEqual(names, ["TPS · Action", "TPS · 4K", "TPS · Drama"])
            items = {}   # each collection's latest items: made at the start, filled as the titles arrive
            for m, p, b in calls:
                if p.startswith("/dvr/collections/content/") and b:
                    items[b["Name"]] = b["Items"]
            self.assertEqual(items["TPS · Drama"], ["coax-p2-s1"])
            self.assertEqual(len(items["TPS · Action"]), 2)
            self.assertEqual([r["ParentID"] for r in sections[0]["Rows"]], ["1", "2", "3"])
            # movies and the show wait for their TMDB match
            self.assertEqual(sorted(k[0] for k in st["todo"]), ["f", "f", "g"])
            calls.clear()
            lib.sync("p2")   # nothing new: nothing created or changed
            self.assertFalse([p for m, p, b in calls if m == "POST" and p == "/dvr/files"])
            self.assertFalse([p for m, p, b in calls if "/visibility/" in p])
            # "Action" and the series group set to show with the library
            self.provider["vod"]["library"] = {"movies": ["Action"], "series": ["Drama"]}
            lib.sync("p2")
            shown = sorted(p for m, p, b in calls if p.endswith("/visibility/main"))
            self.assertEqual(len(shown), 5)   # two movies, two episodes, the show
            self.assertIn("/dvr/groups/coax-p2-s1/visibility/main", shown)
            calls.clear()
            self.provider["vod"]["library"] = {"movies": [], "series": []}
            lib.sync("p2")   # and hidden again on command
            self.assertEqual(len([p for m, p, b in calls if p.endswith("/visibility/hidden")]), 5)
            # the series group unpicked: its episodes and show are deleted one by one, never a DVR-wide prune
            calls.clear()
            self.provider["vod"] = {"movies": ["Action", "4K"], "series": []}
            lib.sync("p2")
            self.assertEqual(len([p for m, p, b in calls if p.endswith("/permanently_delete")]), 2)
            self.assertIn(("DELETE", "/dvr/groups/coax-p2-s1", None), calls)
            self.assertFalse([p for m, p, b in calls if "prune" in p])
            self.assertNotIn("g:coax-p2-s1", lib.state["p2"]["todo"])
            # picks cleared: items, files, collections and section go; the user's own file stays
            calls.clear()
            self.provider["vod"] = {"movies": [], "series": []}
            lib.sync("p2")
            self.assertEqual(len([p for m, p, b in calls if p.endswith("/permanently_delete")]), 2)
            self.assertFalse(os.path.exists(os.path.join(self.imports, "Movies", ".coax", "p2")))
            self.assertTrue(os.path.isfile(os.path.join(self.imports, "Movies", "Mine", "Keep (2000).strm")))
            self.assertIn(("DELETE", "/dvr/custom_sections/9", None), calls)
            self.assertNotIn("p2", lib.state)
        finally:
            vodcat.load, vodcat.episodes_for = orig

    def test_earlier_scanned_layout_is_taken_out_once(self):
        old = os.path.join(self.imports, "Movies", "coax", "p2")
        os.makedirs(old)
        with open(os.path.join(old, "Up (2009).strm"), "w") as handle:
            handle.write("x")
        api = [{"id": "7", "path": "/tv/DVR/Imports/Movies/coax/p2/Up (2009).strm"},
               {"id": "8", "path": "/tv/DVR/Imports/Movies/Mine/Keep (2000).strm"}]
        dvr, calls, _sections, _files = self._fake_dvr(api)
        cfg = {"server": {"base_url": "http://coax:7780"}, "channels_dvr": {"url": "http://dvr:8089"},
               "providers": [self.provider]}
        lib = self._library(dvr, cfg)
        self.assertTrue(lib._due(self.provider))
        orig = self._patched()
        try:
            lib.sync("p2")
        finally:
            vodcat.load, vodcat.episodes_for = orig
        deleted = [p for m, p, b in calls if p.endswith("/permanently_delete")]
        self.assertEqual(deleted, ["/dvr/files/7/permanently_delete"])   # never the user's own import
        self.assertFalse(os.path.exists(os.path.join(self.imports, "Movies", "coax")))
        self.assertTrue(os.path.isfile(os.path.join(self.imports, "Movies", "Mine", "Keep (2000).strm")))
        self.assertTrue(os.path.isfile(os.path.join(self.imports, "Movies/.coax/p2/Up (2009)/Up (2009).strm")))

    def test_tmdb_matching_fills_what_the_provider_lacks(self):
        calls = []
        searches = {
            "/dvr/files/101/matches/tmdb/movies": [
                {"id": 5, "title": "Heat: Redux", "release_date": "1980-01-01"},
                {"id": 6, "title": "Heat - Redux", "release_date": "1995-12-15", "vote_count": 10}],
            "/dvr/groups/coax-p2-s1/matches/tmdb?q=Breaking%20Bad": [
                {"id": 1396, "name": "Breaking Bad", "first_air_date": "2008-01-20", "overview": "A teacher.",
                 "poster_path": "/p.jpg", "backdrop_path": "/b.jpg"}],
        }

        def dvr(cfg, method, path, body=None):
            calls.append((method, path, body))
            if method == "GET" and path in searches:
                return searches[path]
            if method == "GET" and path == "/dvr/groups/coax-p2-s1":
                return {"Name": "Breaking Bad", "Genres": ["Drama"]}
            if method == "GET":
                return []
            return {}
        cfg = {"channels_dvr": {"url": "http://dvr:8089"}, "providers": [self.provider]}
        lib = self._library(dvr, cfg)
        lib.state = {"p2": {"todo": {"f:100": ["14160", "Up", "2009"], "f:101": ["", "Heat - Redux", "1995"],
                                     "f:102": ["", "Nothing Like It", "2001"],
                                     "g:coax-p2-s1": ["", "Breaking Bad", "2008"]}}}
        self.assertEqual(lib.match_pending(), 4)
        puts = [(p, b) for m, p, b in calls if m == "PUT"]
        self.assertIn(("/dvr/files/100/matches/tmdb/movies/14160", None), puts)   # the provider's id, no search
        self.assertIn(("/dvr/files/101/matches/tmdb/movies/6", None), puts)       # the right year wins
        self.assertFalse([p for p, b in puts if p.startswith("/dvr/files/102/")])  # no match: left alone
        group = [b for p, b in puts if p == "/dvr/groups/coax-p2-s1/matches/tmdb/1396"]
        self.assertEqual(group[0]["id"], 1396)
        fields = [b for p, b in puts if p == "/dvr/groups/coax-p2-s1"][0]
        self.assertEqual((fields["Summary"], fields["Image"], fields["ReleaseYear"]),
                         ("A teacher.", vodlib.POSTER % "/p.jpg", 2008))
        self.assertEqual(lib.state["p2"]["todo"], {})
        # an error is tried again later, not dropped
        lib.state["p2"]["todo"] = {"f:103": ["99", "Up", "2009"]}

        def broken(cfg, method, path, body=None):
            raise RuntimeError("down")
        lib._dvr_call = broken
        lib.match_pending()
        self.assertIn("f:103", lib.state["p2"]["todo"])
        self.assertGreater(lib.state["p2"]["retry"]["f:103"], time.time())

    def test_best_match_and_episode_titles(self):
        results = [{"id": 1, "title": "Up", "release_date": "2009-05-28", "vote_count": 100},
                   {"id": 2, "title": "Up", "release_date": "1984-01-01"}]
        self.assertEqual(vodlib.best_match(results, "Up", "2009")["id"], 1)
        self.assertEqual(vodlib.best_match(results, "Up", "1984")["id"], 2)
        self.assertIsNone(vodlib.best_match(results, "Down", "2009"))
        self.assertIsNone(vodlib.best_match(results, "Up", "2015"))
        self.assertEqual(vodlib.episode_title("Breaking Bad - S01E02 - Cat's in the Bag", 1, 2), "Cat's in the Bag")
        self.assertEqual(vodlib.episode_title("Breaking Bad - S01E02", 1, 2), "")
        self.assertEqual(vodlib.episode_title("Pilot", 1, 1), "Pilot")

    def test_movies_go_to_channels_before_episodes_are_read_then_batches_and_a_quick_continue(self):
        published = []
        cfg = {"server": {"base_url": "http://coax:7780"}, "channels_dvr": {"url": ""}, "providers": [self.provider]}

        class Store:
            def get(self):
                return cfg
        lib = vodlib.Library(Store(), self.tmp, imports_dir=self.imports, dvr_call=lambda *a: {})
        real_publish = lib._publish

        def publish(cfg_, provider, vod, episodes, base):
            published.append(sorted(k for k, v in episodes.items() if v))
            real_publish(cfg_, provider, vod, episodes, base)
        lib._publish = publish
        shows = [{"id": "s%d" % i, "name": "Show %d (2020)" % i, "groups": ["Drama"], "modified": i} for i in range(5)]
        vod = dict(self.vod, series=shows, fetched_at=vodcat.catalog.now_iso())
        fetched = []

        def episodes(provider, series, fetch=True, max_fetch=None, info=None, **kw):
            order = [s["id"] for s in series]
            todo = [sid for sid in order if sid not in fetched]
            if fetch:
                time.sleep(0.01)   # each batch takes a moment, like the panel
                todo, rest = todo[:max_fetch], todo[max_fetch:]
                fetched.extend(todo)
                if info is not None:
                    info.update(fetched=len(todo), left=len(rest), throttled=False)
            return {sid: ([{"id": sid + "e", "season": 1, "episode": 1}] if sid in fetched else []) for sid in order}
        orig = (vodcat.load, vodcat.episodes_for, vodlib.BATCH_SERIES, vodlib.EPISODE_BUDGET)
        vodcat.load, vodcat.episodes_for, vodlib.BATCH_SERIES = (lambda pid: vod), episodes, 2
        try:
            lib.sync("p2")
            self.assertEqual(published[0], [])                 # movies first, before any episode list
            self.assertEqual(published[1], ["s3", "s4"])       # newest shows first, two per batch
            self.assertEqual(published[-1], ["s0", "s1", "s2", "s3", "s4"])
            self.assertNotIn("continue_at", lib.state["p2"])
            # out of time with series left: the next batch is due in minutes, not at the next refresh
            fetched.clear()
            vodlib.EPISODE_BUDGET = 0.005
            lib.sync("p2")
            st = lib.state["p2"]
            self.assertTrue(st["series_left"] > 0)
            self.assertLess(st["continue_at"] - time.time(), vodlib.CONTINUE_AFTER + 5)
            self.assertFalse(lib._due(self.provider))
            st["continue_at"] = time.time() - 1
            self.assertTrue(lib._due(self.provider))
        finally:
            vodcat.load, vodcat.episodes_for, vodlib.BATCH_SERIES, vodlib.EPISODE_BUDGET = orig

    def test_switched_off_provider_keeps_its_library(self):
        cfg = {"server": {"base_url": "http://coax:7780"}, "channels_dvr": {"url": ""},
               "providers": [dict(self.provider, enabled=False)]}

        class Store:
            def get(self):
                return cfg
        lib = vodlib.Library(Store(), self.tmp, imports_dir=self.imports, dvr_call=lambda *a: {})
        rel = os.path.join(self.imports, "Movies", ".coax", "p2", "Up (2009).strm")
        os.makedirs(os.path.dirname(rel))
        with open(rel, "w") as handle:
            handle.write("x")
        lib.sync("p2")
        self.assertTrue(os.path.isfile(rel))

    def test_vod_hours(self):
        self.assertIsNone(vodlib.vod_hours({"refresh_hours": 0}))
        self.assertEqual(vodlib.vod_hours({"refresh_hours": 1}), 12.0)
        self.assertEqual(vodlib.vod_hours({"refresh_hours": 24}), 24.0)
        self.assertEqual(vodlib.vod_hours({}), 24.0)

    def test_imports_check(self):
        self.assertEqual(vodlib.imports_ready(self.imports), (True, ""))
        self.assertFalse(vodlib.imports_ready(os.path.join(self.tmp, "nope"))[0])
        self.assertFalse(vodlib.imports_ready(os.path.join(self.imports, "Videos"))[0])


if __name__ == "__main__":
    unittest.main()
