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

    def test_crashing_engine_keeps_restarting_with_keepalive(self):
        """Never give up while the DVR is connected; null packets fill the gaps between engines."""
        hubmod.RESTART_DELAY, old_max = 0.0, hubmod.BACKOFF_MAX
        hubmod.BACKOFF_MAX = 0.0
        try:
            h, eng = make(config([provider("p1", 1)], [channel(1, [("p1", "a")])]), "--exit-after", "0.05")
            s = h.open("uuid-1", "ip", "ua")
            r = Reader(s)
            r.start()
            self.assertTrue(wait_for(lambda: eng.count() >= 8, 15))
            self.assertFalse(r.finished.is_set())
            self.assertEqual(r.misaligned, 0)
            s.close()
            self.assertTrue(r.finished.wait(5))
            self.assertEqual(h.line_status()[0]["used"], 0)
        finally:
            hubmod.BACKOFF_MAX = old_max

    def test_frozen_engine_is_restarted(self):
        old = hubmod.STALL_TIMEOUT
        hubmod.STALL_TIMEOUT = 0.6
        try:
            h, eng = make(config([provider("p1", 1)], [channel(1, [("p1", "a")])]), "--freeze-after", "0.3")
            s = h.open("uuid-1", "ip", "ua")
            r = Reader(s)
            r.start()
            self.assertTrue(wait_for(lambda: eng.count() >= 2, 10))
            self.assertTrue(any(e["kind"] == "restart" and "froze" in e["text"] for e in s.events))
            s.close()
            self.assertTrue(r.finished.wait(5))
        finally:
            hubmod.STALL_TIMEOUT = old

    def test_no_video_retunes_and_moves_to_another_line(self):
        old = hubmod.DEAD_TIMEOUT
        hubmod.DEAD_TIMEOUT = 0.5
        try:
            h, eng = make(config([provider("p1", 1, 1)], [channel(1, [("p1", "a")])]), "--nulls-only")
            s = h.open("uuid-1", "ip", "ua")
            first = s.line_id
            r = Reader(s)
            r.start()
            self.assertTrue(wait_for(lambda: eng.count() >= 3, 15))
            self.assertFalse(r.finished.is_set())
            self.assertNotEqual(s.line_id, first)   # second retune in a row moved to the free line
            used = sorted(l["used"] for l in h.line_status())
            self.assertEqual(used, [0, 1])          # never two lines at once
            s.close()
            self.assertTrue(r.finished.wait(5))
            self.assertTrue(all(l["used"] == 0 for l in h.line_status()))
        finally:
            hubmod.DEAD_TIMEOUT = old

    def test_failover_holds_other_providers_line_and_failed_stream_goes_last(self):
        cfg = config([provider("p1", 1), provider("p2", 1)],
                     [channel(1, [("p1", "a"), ("p2", "b")]), channel(2, [("p2", "c")])])
        h, _ = make(cfg)
        s = h.open("uuid-1", "ip", "ua")
        h.backup_urls(1)
        ref_b = sorted(hubmod.url_refs("fake://p2/l1/b"))[0]
        s._watch("[hlsloop stream] stalled; switching to stream 2 of 2 [ref %s]" % ref_b)
        self.assertEqual(s.extra_slots, [("p2", "l1")])
        self.assertEqual(s.stream_no, 2)
        self.assertIsNone(h.open("uuid-2", "ip", "ua"))        # p2's only line is now held
        ref_a = sorted(hubmod.url_refs("fake://p1/l1/a"))[0]
        s._watch("[hlsloop stream] stream 1 of 2 failed; not using it again for 60s [ref %s]" % ref_a)
        self.assertEqual(h.backup_urls(1)[0], "fake://p2/l1/b")  # the failed main stream goes last
        s.close()
        self.assertTrue(all(l["used"] == 0 for l in h.line_status()))

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


class BlockedBackupTest(unittest.TestCase):
    def test_blocked_stream_left_out_of_failover_list(self):
        cfg = config([provider("p1", 0)], [channel(1, [("p1", "a"), ("p1", "b"), ("p1", "c")])])
        h, _ = make(cfg)

        class Health:
            def is_blocked(self, ch, key):
                return key == ("p1", "c")
        h.health = Health()
        s = h.open("uuid-1", "ip", "ua")
        self.assertEqual(h.backup_urls(1), ["fake://p1/l1/a", "fake://p1/l1/b"])
        s.close()


class RankedFailoverTest(unittest.TestCase):
    def test_failover_list_in_score_order_playing_first(self):
        cfg = config([provider("p1", 0)], [channel(1, [("p1", "a"), ("p1", "b"), ("p1", "c")])])
        hub, _ = make(cfg)

        class Health:
            def is_blocked(self, ch, key):
                return False

            def rank(self, ch, key):
                return {"a": (0, 5.0), "b": (0, 9.0), "c": (0, -1.0)}[key[1]]
        hub.health = Health()
        s = hub.open("uuid-1", "ip", "ua")
        self.assertEqual(hub.backup_urls(1), ["fake://p1/l1/a", "fake://p1/l1/c", "fake://p1/l1/b"])
        s.close()


class TimelineTest(unittest.TestCase):
    def test_startup_keepalive_and_transcode_not_listed(self):
        h, _ = make(config([provider("p1", 1)], [channel(1, [("p1", "a")])]))
        s = h.open("uuid-1", "ip", "ua")
        s._watch("[hlsloop stream] transcoding with Intel/AMD graphics (VAAPI)")
        s._watch("[hlsloop stream] output: no video to send for 2s, sending keepalive packets so the stream stays open")
        s._watch("[hlsloop stream] output: video flowing again after 2.5s without video")
        self.assertEqual(list(s.events), [])
        self.assertEqual(s.encoder, "Intel/AMD graphics (VAAPI)")
        s.spawned_at -= 60
        s._watch("[hlsloop stream] output: no video to send for 2s, sending keepalive packets so the stream stays open")
        self.assertEqual([e["kind"] for e in s.events], ["keepalive"])
        s.close()


class ClientGoneTest(unittest.TestCase):
    def test_dvr_leaving_before_video_releases_the_line(self):
        hubmod.RESTART_DELAY = 0.05
        h, eng = make(config([provider("p1", 1)], [channel(1, [("p1", "a")])]), "--nulls-only")
        s = h.open("uuid-1", "ip", "ua")
        gone = {"v": False}
        s.client_gone = lambda: gone["v"]
        r = Reader(s)
        r.start()
        self.assertTrue(wait_for(lambda: eng.count() >= 1, 5))
        gone["v"] = True
        self.assertTrue(r.finished.wait(10))
        self.assertEqual(h.line_status()[0]["used"], 0)

    def test_move_line_resets_backup_slot_and_position(self):
        cfg = config([provider("p1", 1, 1), provider("p2", 1)], [channel(1, [("p1", "a"), ("p2", "b")])])
        h, _ = make(cfg)
        s = h.open("uuid-1", "ip", "ua")
        h.backup_urls(1)
        ref_b = sorted(hubmod.url_refs("fake://p2/l1/b"))[0]
        s._watch("[hlsloop stream] stalled; switching to stream 2 of 2 [ref %s]" % ref_b)
        self.assertEqual(s.extra_slots, [("p2", "l1")])
        self.assertTrue(h._move_line(s))
        self.assertEqual(s.extra_slots, [])
        self.assertEqual(s.cur_key, ("p1", "a"))
        used = {(l["provider_id"], l["line_id"]): l["used"] for l in h.line_status()}
        self.assertEqual(used[("p2", "l1")], 0)
        s.close()


class FrozenFeedBlockedEndToEnd(unittest.TestCase):
    """A main stream that keeps freezing: the real hub reports each freeze to the real feed health,
    and on the third within an hour the stream is blocked, the proven backup becomes main, and the bell
    says so. Fake engine, temp folder, no provider."""

    def test_three_freezes_do_not_block_main(self):
        import copy
        import tempfile
        from app import feedhealth as fh

        hubmod.RESTART_DELAY = 0.05
        old_stall = hubmod.STALL_TIMEOUT
        hubmod.STALL_TIMEOUT = 0.6
        self.addCleanup(setattr, hubmod, "STALL_TIMEOUT", old_stall)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)

        cfg = config([provider("p1", 0)], [channel(1, [("p1", "main"), ("p1", "b2")])])

        class Store:
            def get(self):
                return cfg

            def update(self, mutate):
                c = copy.deepcopy(cfg)
                r = mutate(c)
                cfg.clear()
                cfg.update(c)
                return r

        health = fh.FeedHealth(Store(), tmp.name)
        clock = [1000.0]
        real_time = fh.time.time
        # Feed health counts one freeze per 2 minutes (one episode is one problem), so each freeze the
        # hub reports is placed 10 minutes after the last, as in a real evening of a bad feed.
        def tick():
            clock[0] += 600
            return clock[0]
        fh.time.time = tick
        self.addCleanup(setattr, fh.time, "time", real_time)
        health.good(1, ("p1", "b2"), 30)          # the backup has played clean before: it is proven
        h, eng = make(cfg, "--freeze-after", "0.3")
        h.health = health
        s = h.open("uuid-1", "ip", "ua")
        r = Reader(s)
        r.start()
        order = lambda: [x["stream_id"] for x in cfg["channels"][0]["streams"]]
        # Hub restarts are recovery attempts: they never block or reorder on their own.
        self.assertTrue(wait_for(lambda: eng.count() >= 3, 30))
        self.assertEqual(order(), ["main", "b2"])
        self.assertFalse(health.is_blocked(1, ("p1", "main"), now=clock[0]))
        s.close()


class PlaceholderBlameTest(unittest.TestCase):
    def test_placeholder_blames_the_stream_that_showed_it(self):
        h, _ = make(config([provider("p1", 0)], [channel(1, [("p1", "a"), ("p1", "b")])]))
        seen = []

        class Health:
            def problem(self, ch, key, kind, *a):
                seen.append((key, kind))

            def switched(self, *a):
                pass

            def is_blocked(self, *a):
                return False

            def rank(self, *a):
                return (0, 0.0)
        h.health = Health()
        s = h.open("uuid-1", "ip", "ua")
        h.backup_urls(1)
        ref_b = sorted(hubmod.url_refs("fake://p1/l1/b"))[0]
        s._watch("[hlsloop stream] provider is serving an offline placeholder; switching to stream 2 of 2 [ref %s]" % ref_b)
        self.assertEqual(seen, [(("p1", "a"), "placeholder")])
        self.assertEqual(s.cur_key, ("p1", "b"))
        s.close()


class DisabledStreamTest(unittest.TestCase):
    def test_disabled_streams_are_skipped_and_kept(self):
        from app import hub as h, server
        cls = next(v for v in vars(h).values() if isinstance(v, type) and hasattr(v, "_candidates"))
        ch = {"streams": [{"provider": "p1", "stream_id": "1", "disabled": True}, {"provider": "p1", "stream_id": "2"}]}
        self.assertEqual([st["stream_id"] for st, _ in cls._candidates(ch, {"p1": {"id": "p1"}})], ["2"])
        refs = server.stream_refs(ch["streams"])
        self.assertTrue(refs[0].get("disabled"))
        self.assertNotIn("disabled", refs[1])


class FeedsViewTest(unittest.TestCase):
    def test_card_lists_every_stream_with_block_and_disabled_state(self):
        cfg = config([provider("p1", 0)], [channel(1, [("p1", "a"), ("p1", "b"), ("p1", "c")])])
        cfg["channels"][0]["streams"][2]["disabled"] = True
        h, _ = make(cfg)

        class Health:
            def block_info(self, ch, key):
                return {"until": 2e9, "kind": "placeholder", "why": "x"} if key == ("p1", "b") else None

            def is_blocked(self, *a):
                return False

            def rank(self, *a):
                return 0
        h.health = Health()

        class S:
            channel_id = 1
            cur_key = ("p1", "a")
        f = h._feeds_view(cfg, S())
        self.assertEqual([x["stream_id"] for x in f], ["a", "b", "c"])
        self.assertTrue(f[0]["playing"])
        self.assertTrue(f[1]["blocked"])
        self.assertEqual(f[1]["blocked_until"], 2e9)
        self.assertFalse(f[0]["blocked"])
        self.assertTrue(f[2]["disabled"])


class HandSwitchTest(unittest.TestCase):
    def setUp(self):
        hubmod.RESTART_DELAY = 0.05

    def test_old_line_held_until_old_engine_is_dead(self):
        cfg = config([provider("a", 1), provider("b", 1)],
                     [channel(1, [("a", "s1"), ("b", "s2")]), channel(2, [("a", "s3")])])
        h, eng = make(cfg, "--ignore-term")   # the old engine takes STOP_TIMEOUT to die
        s = h.open("uuid-1", "ip", "ua")
        r = Reader(s)
        r.start()
        self.assertTrue(wait_for(lambda: s.proc is not None and r.bytes > 0, 5))
        old = s.proc
        th = threading.Thread(target=h.switch, args=(1, 0, "b", "s2"))
        th.start()
        self.assertTrue(wait_for(lambda: s.provider_id == "b", 2))
        self.assertIsNone(old.poll())
        self.assertIsNone(h.open("uuid-2", "ip", "ua"))   # a/l1 still has the old engine on it
        th.join(10)
        self.assertIsNotNone(old.poll())
        s2 = h.open("uuid-2", "ip", "ua")
        self.assertEqual((s2.provider_id, s2.line_id), ("a", "l1"))
        s2.close()
        # The hand switch starts the new stream at once and is not counted as a crash.
        self.assertTrue(wait_for(lambda: eng.count() >= 2, 3))
        self.assertEqual(s.restarts, [])
        r.stop()
        self.assertTrue(r.finished.wait(10))
        self.assertEqual(sum(l["used"] for l in h.line_status()), 0)

    def test_switch_to_backup_provider_reuses_the_line_it_holds(self):
        cfg = config([provider("p1", 1), provider("p2", 1)], [channel(1, [("p1", "a"), ("p2", "b"), ("p2", "c")])])
        h, _ = make(cfg)
        s = h.open("uuid-1", "ip", "ua")
        h.backup_urls(1)
        ref_b = sorted(hubmod.url_refs("fake://p2/l1/b"))[0]
        s._watch("[hlsloop stream] stalled; switching to stream 2 of 3 [ref %s]" % ref_b)
        self.assertEqual(s.extra_slots, [("p2", "l1")])
        h.switch(1, 0, "p2", "c")
        self.assertEqual((s.provider_id, s.line_id, s.extra_slots), ("p2", "l1", []))
        used = {(l["provider_id"], l["line_id"]): l["used"] for l in h.line_status()}
        self.assertEqual(used, {("p1", "l1"): 0, ("p2", "l1"): 1})
        s.close()

    def test_timeline_names_the_stream_by_id(self):
        cfg = config([provider("p1", 1)], [channel(1, [("p1", "a"), ("p1", "b")])])
        h, _ = make(cfg)
        h._remember("fake://p1/l1/b", "p1", "l1", "b")
        ref = sorted(hubmod.url_refs("fake://p1/l1/b"))[0]
        line = "[hlsloop stream] stream 2 of 2 failed; not using it again for 60s [ref %s]" % ref
        self.assertEqual(h._label(line, "Stream 2 of 2 failed; skipping it for a while"),
                         "Stream b failed; skipping it for a while")