"""Tests for the ah4c-style hub: one DVR connection, one engine, one line slot."""

import os
import threading
import time
import unittest

from app import hub as hubmod
from tests.fixtures.hub.support import EngineCommand, Reader, channel, config, fake_stream_url, provider


def make(cfg, *flags):
    eng = EngineCommand(*flags)
    return hubmod.Hub(lambda: cfg, stream_url=fake_stream_url, engine_command=eng), eng


def wait_for(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


class HubTests(unittest.TestCase):
    def setUp(self):
        hubmod.RESTART_DELAY = 0.05

    def test_stream_flows_and_close_releases_slot_and_kills_engine(self):
        h, _ = make(config([provider("p1", 1)], [channel(1, [("p1", "s1")])]))
        s = h.open("uuid-1", "1.2.3.4", "DVR")
        r = Reader(s)
        r.start()
        self.assertTrue(wait_for(lambda: r.bytes > 188 * 50))
        self.assertEqual(r.misaligned, 0)
        pid = s.proc.pid
        self.assertEqual(h.line_status()[0]["used"], 1)
        s.close()
        self.assertTrue(r.finished.wait(5))
        self.assertTrue(wait_for(lambda: not alive(pid)))
        self.assertEqual(h.line_status()[0]["used"], 0)
        self.assertEqual(h.status(), [])

    def test_busy_line_gives_none_then_second_line_then_other_provider(self):
        cfg = config([provider("p1", 1, 1), provider("p2", 1)],
                     [channel(1, [("p1", "a"), ("p2", "b")]), channel(2, [("p1", "c"), ("p2", "d")]),
                      channel(3, [("p1", "e"), ("p2", "f")]), channel(4, [("p1", "g")])])
        h, _ = make(cfg)
        s1 = h.open("uuid-1", "ip", "ua")
        s2 = h.open("uuid-2", "ip", "ua")
        s3 = h.open("uuid-3", "ip", "ua")
        self.assertEqual((s1.provider_id, s1.line_id), ("p1", "l1"))
        self.assertEqual((s2.provider_id, s2.line_id), ("p1", "l2"))
        self.assertEqual((s3.provider_id, s3.line_id), ("p2", "l1"))
        self.assertIsNone(h.open("uuid-4", "ip", "ua"))
        s1.close()
        s4 = h.open("uuid-4", "ip", "ua")
        self.assertEqual((s4.provider_id, s4.line_id), ("p1", "l1"))
        for s in (s2, s3, s4):
            s.close()
        self.assertTrue(all(l["used"] == 0 for l in h.line_status()))

    def test_unlimited_line_and_disabled_or_unknown_channel(self):
        h, _ = make(config([provider("p1", 0)], [channel(1, [("p1", "a")]), channel(2, [("p1", "b")], enabled=False)]))
        a = h.open("uuid-1", "ip", "ua")
        b = h.open("uuid-1", "ip", "ua")
        self.assertIsNotNone(a)
        self.assertIsNotNone(b)
        self.assertIsNone(h.open("uuid-2", "ip", "ua"))
        self.assertIsNone(h.open("nope", "ip", "ua"))
        a.close()
        b.close()

    def test_close_without_reading_and_double_close(self):
        h, eng = make(config([provider("p1", 1)], [channel(1, [("p1", "a")])]))
        s = h.open("uuid-1", "ip", "ua")
        s.close()
        s.close()
        self.assertEqual(eng.count(), 0)  # engine never started
        self.assertEqual(h.line_status()[0]["used"], 0)
        self.assertIsNotNone(h.open("uuid-1", "ip", "ua"))

    def test_url_failure_releases_and_tries_next_stream(self):
        cfg = config([provider("p1", 1), provider("p2", 1)], [channel(1, [("p1", "bad"), ("p2", "ok")])])

        def url(p, l, sid):
            if sid == "bad":
                raise ValueError("gone")
            return fake_stream_url(p, l, sid)
        h = hubmod.Hub(lambda: cfg, stream_url=url, engine_command=EngineCommand())
        s = h.open("uuid-1", "ip", "ua")
        self.assertEqual(s.provider_id, "p2")
        used = {(l["provider_id"], l["line_id"]): l["used"] for l in h.line_status()}
        self.assertEqual(used, {("p1", "l1"): 0, ("p2", "l1"): 1})
        s.close()

    def test_engine_crash_restarts_on_same_slot(self):
        h, eng = make(config([provider("p1", 1)], [channel(1, [("p1", "a")])]), "--exit-after", "0.3")
        s = h.open("uuid-1", "ip", "ua")
        r = Reader(s)
        r.start()
        self.assertTrue(wait_for(lambda: eng.count() >= 3, 10))
        self.assertFalse(r.finished.is_set())
        self.assertEqual(h.line_status()[0]["used"], 1)
        s.close()
        self.assertTrue(r.finished.wait(5))
        self.assertEqual(h.line_status()[0]["used"], 0)

    def test_restart_limit_ends_stream_and_close_still_releases(self):
        hubmod.RESTART_DELAY = 0.0
        h, eng = make(config([provider("p1", 1)], [channel(1, [("p1", "a")])]), "--exit-after", "0.05")
        s = h.open("uuid-1", "ip", "ua")
        r = Reader(s)
        r.start()
        self.assertTrue(r.finished.wait(15))
        self.assertEqual(eng.count(), hubmod.RESTART_LIMIT + 1)
        s.close()  # the server always closes after iter_bytes ends
        self.assertEqual(h.line_status()[0]["used"], 0)

    def test_engine_ignoring_sigterm_is_killed(self):
        h, _ = make(config([provider("p1", 1)], [channel(1, [("p1", "a")])]), "--ignore-term")
        hubmod.STOP_TIMEOUT = 0.5
        try:
            s = h.open("uuid-1", "ip", "ua")
            r = Reader(s)
            r.start()
            self.assertTrue(wait_for(lambda: r.bytes > 0))
            pid = s.proc.pid
            s.close()
            self.assertTrue(wait_for(lambda: not alive(pid), 5))
        finally:
            hubmod.STOP_TIMEOUT = 3.0

    def test_concurrent_tunes_never_exceed_limit(self):
        chans = [channel(i, [("p1", str(i))]) for i in range(1, 41)]
        h, _ = make(config([provider("p1", 2, 3)], chans))
        got, lock = [], threading.Lock()

        def worker(i):
            s = h.open("uuid-%d" % i, "ip", "ua")
            if s is not None:
                with lock:
                    got.append(s)
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(1, 41)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(got), 5)
        for line in h.line_status():
            self.assertLessEqual(line["used"], line["max"])
        for s in got:
            s.close()
        self.assertTrue(all(l["used"] == 0 for l in h.line_status()))

    def test_provider_total_cap(self):
        p = provider("p1", 1, 1, 1)
        p["max_streams"] = 2
        h, _ = make(config([p], [channel(i, [("p1", str(i))]) for i in (1, 2, 3)]))
        a = h.open("uuid-1", "ip", "ua")
        b = h.open("uuid-2", "ip", "ua")
        self.assertIsNone(h.open("uuid-3", "ip", "ua"))
        a.close()
        c = h.open("uuid-3", "ip", "ua")
        self.assertIsNotNone(c)
        b.close()
        c.close()

    def test_close_from_other_thread_while_reading(self):
        h, _ = make(config([provider("p1", 1)], [channel(1, [("p1", "a")])]))
        for _ in range(5):
            s = h.open("uuid-1", "ip", "ua")
            r = Reader(s)
            r.start()
            self.assertTrue(wait_for(lambda: r.bytes > 0))
            pid = s.proc.pid
            stdout = s.proc.stdout
            s.close()                      # another thread, while the reader is in os.read
            self.assertTrue(r.finished.wait(5))
            self.assertTrue(wait_for(lambda: not alive(pid)))
            self.assertTrue(stdout.closed)  # the reader closed its own pipe
            self.assertEqual(h.line_status()[0]["used"], 0)

    def test_backup_urls_use_session_line(self):
        cfg = config([provider("p1", 1, 1), provider("p2", 1)], [channel(1, [("p1", "a"), ("p1", "b"), ("p2", "c")]),
                                                                 channel(2, [("p1", "z")])])
        h, _ = make(cfg)
        other = h.open("uuid-2", "ip", "ua")   # takes p1/l1
        s = h.open("uuid-1", "ip", "ua")       # gets p1/l2
        self.assertEqual(h.backup_urls(1), ["fake://p1/l2/a", "fake://p1/l2/b", "fake://p2/l1/c"])
        s.close()
        other.close()


    def test_backup_slot_respects_line_limit_and_is_released_once(self):
        cfg = config([provider("p1", 1), provider("p2", 1)],
                     [channel(1, [("p1", "a"), ("p2", "b")]), channel(2, [("p2", "c")])])
        h, _ = make(cfg)
        s = h.open("uuid-1", "ip", "ua")                          # p1/l1
        self.assertEqual(h.backup_urls(1), ["fake://p1/l1/a", "fake://p2/l1/b"])
        self.assertEqual(h.backup_urls(1), ["fake://p1/l1/a", "fake://p2/l1/b"])
        used = {(l["provider_id"], l["line_id"]): l["used"] for l in h.line_status()}
        self.assertEqual(used, {("p1", "l1"): 1, ("p2", "l1"): 0})  # reading the list reserves nothing
        t = h.open("uuid-2", "ip", "ua")                          # so another channel can still tune p2
        self.assertEqual(t.provider_id, "p2")
        self.assertEqual(h.backup_urls(1), ["fake://p1/l1/a"])     # p2 now busy: left out
        s.close()
        s.close()
        t.close()
        self.assertTrue(all(l["used"] == 0 for l in h.line_status()))

    def test_backup_omitted_when_other_provider_is_full(self):
        cfg = config([provider("p1", 1), provider("p2", 1)],
                     [channel(1, [("p1", "a"), ("p2", "b")]), channel(2, [("p2", "c")])])
        h, _ = make(cfg)
        other = h.open("uuid-2", "ip", "ua")                      # takes p2/l1
        s = h.open("uuid-1", "ip", "ua")
        self.assertEqual(h.backup_urls(1), ["fake://p1/l1/a"])
        self.assertEqual(s.extra_slots, [])
        other.close()
        self.assertEqual(h.backup_urls(1), ["fake://p1/l1/a", "fake://p2/l1/b"])  # free again: reserved now
        s.close()
        self.assertTrue(all(l["used"] == 0 for l in h.line_status()))

    def test_backup_respects_provider_total_cap(self):
        p2 = provider("p2", 1, 1)
        p2["max_streams"] = 1
        cfg = config([provider("p1", 1), p2], [channel(1, [("p1", "a"), ("p2", "b")]), channel(2, [("p2", "c")])])
        h, _ = make(cfg)
        other = h.open("uuid-2", "ip", "ua")                      # p2/l1; p2/l2 is free but p2 is at its cap
        s = h.open("uuid-1", "ip", "ua")
        self.assertEqual(h.backup_urls(1), ["fake://p1/l1/a"])
        for line in h.line_status():
            if line["provider_id"] == "p2":
                self.assertLessEqual(line["used"], 1)
        other.close()
        s.close()
        self.assertTrue(all(l["used"] == 0 for l in h.line_status()))

    def test_backup_urls_need_exactly_one_session(self):
        cfg = config([provider("p1", 0), provider("p2", 0)], [channel(1, [("p1", "a"), ("p2", "b")])])
        h, _ = make(cfg)
        self.assertEqual(h.backup_urls(1), [])                    # nothing streams it: reserve nothing
        a = h.open("uuid-1", "ip", "ua")
        b = h.open("uuid-1", "ip", "ua")
        self.assertEqual((a.instance, b.instance), (0, 1))
        self.assertEqual(h.backup_urls(1), [])
        a.close()
        self.assertEqual(h.backup_urls(1), ["fake://p1/l1/a", "fake://p2/l1/b"])
        b.close()
        self.assertTrue(all(l["used"] == 0 for l in h.line_status()))

    def test_popen_failure_ends_stream_and_close_releases_once(self):
        h, _ = make(config([provider("p1", 1), provider("p2", 1)], [channel(1, [("p1", "a"), ("p2", "b")])]))
        real = hubmod.subprocess.Popen

        def boom(*a, **k):
            raise OSError("no such engine")
        hubmod.subprocess.Popen = boom
        try:
            s = h.open("uuid-1", "ip", "ua")
            h.backup_urls(1)                                       # read-only, holds nothing
            self.assertEqual(list(s.iter_bytes()), [])
        finally:
            hubmod.subprocess.Popen = real
        self.assertIsNone(s.proc)
        self.assertEqual(sum(l["used"] for l in h.line_status()), 1)
        s.close()
        s.close()
        self.assertTrue(all(l["used"] == 0 for l in h.line_status()))
        self.assertEqual(h.status(), [])

    def test_engine_env_gets_port_and_session_suffix(self):
        h, _ = make(config([provider("p1", 0)], [channel(1, [("p1", "a")])]))
        h.port = 8123
        seen = []
        real = hubmod.subprocess.Popen

        def spy(*a, **k):
            seen.append(k.get("env") or {})
            return real(*a, **k)
        hubmod.subprocess.Popen = spy
        try:
            a = h.open("uuid-1", "ip", "ua")
            b = h.open("uuid-1", "ip", "ua")
            for s in (a, b):
                next(iter(s.iter_bytes()))
        finally:
            hubmod.subprocess.Popen = real
        a.close()
        b.close()
        self.assertEqual([e.get("COAX_PORT") for e in seen], ["8123", "8123"])
        self.assertEqual([e.get("COAX_SESSION") for e in seen], ["", "~1"])
        h.port = None
        h._get_config = lambda: {"server": {"port": 7999}}
        self.assertEqual(h._port(), 7999)

    def test_shutdown_kills_and_releases(self):
        h, _ = make(config([provider("p1", 0)], [channel(i, [("p1", str(i))]) for i in (1, 2)]))
        sessions = [h.open("uuid-%d" % i, "ip", "ua") for i in (1, 2)]
        readers = [Reader(s) for s in sessions]
        for r in readers:
            r.start()
        self.assertTrue(wait_for(lambda: all(s.proc is not None for s in sessions)))
        pids = [s.proc.pid for s in sessions]
        h.shutdown()
        self.assertFalse(any(alive(pid) for pid in pids))  # synchronous: already reaped
        self.assertEqual(h.status(), [])
        self.assertTrue(all(l["used"] == 0 for l in h.line_status()))

if __name__ == "__main__":
    unittest.main()
