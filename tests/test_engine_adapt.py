"""Tests for the coax adaptation of the stream engines (app/engine/).

The engines are scripts that start streaming as soon as they run, so the tests
never import them. They compile both files, then pull the small coax-specific
helpers (load_settings, iapi, feeds) out of the source with ast and run them
against temp config files and a fake internal API on 127.0.0.1.
"""
import ast
import json
import os
import py_compile
import re
import socketserver
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ENGINE_DIR = Path(__file__).resolve().parent.parent / "app" / "engine"
ENGINES = ("hlsloop", "tsloop")


def engine_source(name):
    return (ENGINE_DIR / (name + ".py")).read_text()


def extract(name, wanted, namespace):
    """Exec the named top-level functions/assignments of an engine into namespace."""
    tree = ast.parse(engine_source(name))
    nodes = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in wanted:
            nodes.append(node)
        elif isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in wanted for t in node.targets):
            nodes.append(node)
    found = {n.name if isinstance(n, ast.FunctionDef) else n.targets[0].id for n in nodes}
    missing = set(wanted) - found
    if missing:
        raise AssertionError("%s.py is missing %s" % (name, sorted(missing)))
    exec(compile(ast.Module(body=nodes, type_ignores=[]), name + ".py", "exec"), namespace)
    return namespace


def base_namespace():
    import requests
    return {"os": os, "json": json, "re": re, "up": urllib.parse, "requests": requests}


class QuickServer(ThreadingHTTPServer):
    # HTTPServer.server_bind calls socket.getfqdn(), which can block for 30+ seconds
    # on hosts with slow reverse DNS. The fake server does not need a server name.
    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


class FakeCoax:
    """Fake coax internal API: /internal/channels/<id>[/streams]."""

    def __init__(self, channels):
        self.channels = channels
        self.requests = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                fake.requests.append((self.path, dict(self.headers)))
                m = re.fullmatch(r"/internal/channels/(\d+)(/streams)?", self.path)
                ch = fake.channels.get(int(m.group(1))) if m else None
                if ch is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                body = json.dumps([{"url": u} for u in ch["streams"]] if m.group(2) else {"uuid": ch["uuid"]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = QuickServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


class CompileTest(unittest.TestCase):
    def test_engines_compile(self):
        for name in ENGINES:
            with self.subTest(engine=name), tempfile.TemporaryDirectory() as d:
                py_compile.compile(str(ENGINE_DIR / (name + ".py")), cfile=os.path.join(d, "x.pyc"), doraise=True)

    def test_no_dispatcharr_settings_or_api_left(self):
        for name in ENGINES:
            src = engine_source(name)
            with self.subTest(engine=name):
                for gone in ("DISPATCHARR_API_KEY", "DISPATCHARR_URL", "DISPATCHARR_PORT", "dapi(", "X-API-Key",
                             "/api/channels/channels", "END OF SETTINGS", "Dispatcharr restart"):
                    self.assertNotIn(gone, src)
                self.assertIn("[%s stream] " % name, src)
                self.assertIn('iapi("/internal/channels/%d/streams" % int(chid))', src)
                self.assertIn('iapi("/internal/channels/%d" % int(chid))["uuid"]', src)


class LoadSettingsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "config.json")
        old = os.environ.get("COAX_CONFIG")
        self.addCleanup(lambda: os.environ.__setitem__("COAX_CONFIG", old) if old is not None else os.environ.pop("COAX_CONFIG", None))
        old_port = os.environ.pop("COAX_PORT", None)
        self.addCleanup(lambda: os.environ.__setitem__("COAX_PORT", old_port) if old_port is not None else os.environ.pop("COAX_PORT", None))

    def write(self, data):
        with open(self.path, "w") as f:
            f.write(data if isinstance(data, str) else json.dumps(data))

    def load(self, name, path=None):
        return extract(name, {"load_settings"}, base_namespace())["load_settings"](path)

    def test_reads_config_from_coax_config_env(self):
        self.write({"server": {"port": 7999}, "channels_dvr": {"url": "http://192.168.1.50:8089"}, "transcode": "1080p60"})
        os.environ["COAX_CONFIG"] = self.path
        for name in ENGINES:
            with self.subTest(engine=name):
                self.assertEqual(self.load(name), ("http://192.168.1.50:8089", "1080p60", 7999))

    def test_safe_defaults(self):
        cases = {
            "missing file": None,
            "not json": "{not json",
            "not an object": "[1, 2]",
            "empty object": {},
            "wrong types": {"server": {"port": "abc"}, "channels_dvr": {"url": 5}, "transcode": None},
            "wrong containers": {"server": 7780, "channels_dvr": "http://x", "transcode": 3},
            "port out of range": {"server": {"port": 70000}},
        }
        for name in ENGINES:
            for label, data in cases.items():
                with self.subTest(engine=name, case=label):
                    if os.path.exists(self.path):
                        os.remove(self.path)
                    if data is not None:
                        self.write(data)
                    os.environ["COAX_CONFIG"] = self.path
                    self.assertEqual(self.load(name), ("", "off", 7780))

    def test_port_as_string_number(self):
        self.write({"server": {"port": "8123"}})
        for name in ENGINES:
            with self.subTest(engine=name):
                self.assertEqual(self.load(name, self.path), ("", "off", 8123))

    def test_coax_port_env_overrides_config(self):
        self.write({"server": {"port": 7999}})
        for name in ENGINES:
            for env, want in (("8124", 8124), ("", 7999), ("abc", 7999), ("-5", 7999)):
                with self.subTest(engine=name, env=env):
                    os.environ["COAX_PORT"] = env
                    self.assertEqual(self.load(name, self.path), ("", "off", want))


class EngineFilesTest(unittest.TestCase):
    def test_job_files_live_in_data_cache(self):
        for name in ENGINES:
            with self.subTest(engine=name):
                src = engine_source(name)
                self.assertIn('os.makedirs("/data/cache", exist_ok=True)', src)
                self.assertIn('JF += "/%s-jobs-%%s.json" %% chid' % name, src)
                self.assertNotIn('("/data" if', src)

    def test_state_file_takes_session_suffix(self):
        for name in ENGINES:
            with self.subTest(engine=name):
                self.assertIn('re.sub("[^0-9~]", "", os.environ.get("COAX_SESSION", ""))', engine_source(name))


class InternalApiTest(unittest.TestCase):
    CH = {7: {"uuid": "8c2b0d0e-1111-2222-3333-444455556666",
              "streams": ["http://prov:80/live/u1/p1/100.ts", "http://prov:80/live/u1/p1/200.ts",
                          "http://other:80/live/u9/p9/300.ts", "http://m3u.example/x.m3u8"]}}

    def namespace(self, name, port, src, chid):
        ns = base_namespace()
        ns.update({"COAX_PORT": port, "src": src, "chid": chid, "logged": []})
        ns["log"] = ns["logged"].append
        wanted = {"IAPI", "iapi", "XC", "feeds"}
        if name == "hlsloop":
            wanted |= {"HSX", "hsu"}
        return extract(name, wanted, ns)

    def test_iapi_reads_streams_and_uuid_without_key(self):
        with FakeCoax(self.CH) as fake:
            for name in ENGINES:
                with self.subTest(engine=name):
                    ns = self.namespace(name, fake.port, "http://prov:80/live/u1/p1/100.ts", "7")
                    self.assertEqual(ns["IAPI"], "http://127.0.0.1:%d" % fake.port)
                    self.assertEqual(ns["iapi"]("/internal/channels/7")["uuid"], self.CH[7]["uuid"])
                    self.assertEqual([x["url"] for x in ns["iapi"]("/internal/channels/7/streams")], self.CH[7]["streams"])
            for path, headers in fake.requests:
                self.assertNotIn("X-API-Key", headers)
                self.assertEqual(headers.get("Accept"), "application/json")

    def test_iapi_raises_on_http_error(self):
        import requests
        with FakeCoax(self.CH) as fake:
            for name in ENGINES:
                with self.subTest(engine=name):
                    ns = self.namespace(name, fake.port, "x", "8")
                    with self.assertRaises(requests.HTTPError):
                        ns["iapi"]("/internal/channels/8")

    def test_feeds_builds_backup_list(self):
        src = "http://prov:80/live/u1/p1/100.ts"
        with FakeCoax(self.CH) as fake:
            for name in ENGINES:
                with self.subTest(engine=name):
                    ns_src = ns_hsu(name, src)
                    ns = self.namespace(name, fake.port, ns_src, "7")
                    got = ns["feeds"]()
                    ext = ".m3u8" if name == "hlsloop" else ".ts"
                    self.assertEqual(got, [ns_src, "http://prov:80/live/u1/p1/200" + ext,
                                           "http://other:80/live/u9/p9/300" + ext, "http://m3u.example/x.m3u8"])
                    self.assertEqual(ns["logged"], [])

    def test_feeds_rewrites_same_host_credentials(self):
        # Backups on the playing stream's host get its line's credentials (kept from the original engine).
        ch = {3: {"uuid": "u", "streams": ["http://prov:80/live/other/pw/200.ts", "http://far:80/live/other/pw/300.ts"]}}
        src = "http://prov:80/live/u1/p1/100.ts"
        with FakeCoax(ch) as fake:
            for name in ENGINES:
                with self.subTest(engine=name):
                    ns = self.namespace(name, fake.port, ns_hsu(name, src), "3")
                    ext = ".m3u8" if name == "hlsloop" else ".ts"
                    self.assertEqual(ns["feeds"]()[1:], ["http://prov:80/live/u1/p1/200" + ext,
                                                         "http://far:80/live/other/pw/300" + ext])

    def test_feeds_falls_back_to_src_and_logs(self):
        with FakeCoax(self.CH) as fake:
            for name in ENGINES:
                with self.subTest(engine=name):
                    ns = self.namespace(name, fake.port, "http://prov:80/live/u1/p1/100.ts", "99")
                    self.assertEqual(ns["feeds"](), ["http://prov:80/live/u1/p1/100.ts"])
                    self.assertEqual(len(ns["logged"]), 1)
                    self.assertTrue(ns["logged"][0].startswith("could not read backup streams ("))
                    ns = self.namespace(name, fake.port, "http://s/x", "")
                    self.assertEqual(ns["feeds"](), ["http://s/x"])

    def test_feeds_when_coax_is_unreachable(self):
        with FakeCoax({}) as fake:
            port = fake.port
        for name in ENGINES:
            with self.subTest(engine=name):
                ns = self.namespace(name, port, "http://s/live/a/b/1.ts", "7")
                self.assertEqual(ns["feeds"](), ["http://s/live/a/b/1.ts"])
                self.assertIn("could not read backup streams", ns["logged"][0])


def ns_hsu(name, url):
    """The HLS engine converts Xtream .ts URLs to .m3u8 at start; mirror that for src."""
    if name != "hlsloop":
        return url
    return re.sub(r"(/live/[^/?]+/[^/?]+/[0-9]+)\.ts(?=$|\?)", r"\1.m3u8", url, count=1)


class ViewerBufferMatchTest(unittest.TestCase):
    def test_uuid_appears_in_coax_stream_url(self):
        # The viewer buffer finds this channel in a Channels DVR M3U source by `uid in url`;
        # coax's output.m3u lines are {base_url}/stream/{uuid}.ts.
        uid = "8c2b0d0e-1111-2222-3333-444455556666"
        for name in ENGINES:
            with self.subTest(engine=name):
                src = engine_source(name)
                self.assertIn('hit = [i for i, (n, x) in enumerate(ent) if uid in x]', src)
                self.assertIn('"/output/m3u" not in str(v)', src)
        self.assertIn(uid, "http://192.168.1.50:7780/stream/%s.ts" % uid)


if __name__ == "__main__":
    unittest.main()


class FfmpegArgsTest(unittest.TestCase):
    def test_both_paths_probe_long_and_add_no_mux_delay(self):
        import os
        for name in ("hlsloop", "tsloop"):
            src = open(os.path.join(os.path.dirname(__file__), "..", "app", "engine", name + ".py")).read()
            i = src.index("def ffcmd():")
            body = src[i:src.index("\ndef ", i + 10)]
            self.assertNotIn('"-muxpreload"', body, name)
            self.assertIn("min_hard_comp=0.1", body, name)
            self.assertNotIn("async=1000", body, name)


class DropChangedTest(unittest.TestCase):
    def test_transcode_drops_changed_audio_frames(self):
        import os
        for name in ("hlsloop", "tsloop"):
            src = open(os.path.join(os.path.dirname(__file__), "..", "app", "engine", name + ".py")).read()
            self.assertEqual(src.count('"-drop_changed:a", "1"'), 1, name)
            self.assertNotIn("first_pts", src, name)



def _pes_packet(stream_id):
    """One 188-byte TS packet starting a PES of the given stream id (0xC0 audio, 0xE0 video)."""
    head = b"\x47\x41\x00\x10" + b"\x00\x00\x01" + bytes([stream_id]) + b"\x00\x00\x80\x00\x00"
    return head + b"\xff" * (188 - len(head))

class SilentAudioGuardTest(unittest.TestCase):
    """The guard restarts an encoder that lost its sound, but never every 15 s and never for a feed with no sound."""

    def run_guard(self, name, chunks):
        clock = [0.0]
        ns = {"mono": lambda: clock[0]}
        extract(name, ("AQ", "SK", "silent"), ns)
        pid, kills = 100, []
        for t, chunk in chunks:
            clock[0] = t
            if ns["silent"](pid, chunk):
                kills.append(t)
                pid += 1   # the writer starts a new encoder
        return kills, ns["SK"]

    def test_video_only_feed_is_never_restarted(self):
        video = _pes_packet(0xE0) * 10
        for name in ENGINES:
            kills, _ = self.run_guard(name, [(i * 0.5, video) for i in range(600)])
            self.assertEqual(kills, [], name)

    def test_lost_sound_restarts_with_backoff_not_every_15s(self):
        video, both = _pes_packet(0xE0) * 10, _pes_packet(0xE0) + _pes_packet(0xC0)
        for name in ENGINES:
            chunks = [(i * 0.5, both) for i in range(40)] + [(20 + i * 0.5, video) for i in range(600)]
            kills, sk = self.run_guard(name, chunks)
            self.assertEqual(len(kills), 3, (name, kills))   # ~30 s, then 60 s and 120 s later within 5 minutes
            self.assertGreaterEqual(kills[1] - kills[0], 60, name)
            self.assertGreaterEqual(kills[2] - kills[1], 120, name)
            self.assertEqual(len(sk), 3, name)   # coax's own kills are not GPU decoding failures

    def test_own_kill_is_not_a_gpu_failure(self):
        for name in ENGINES:
            src = engine_source(name)
            self.assertIn("old.pid not in SK", src[src.index("def remux("):], name)