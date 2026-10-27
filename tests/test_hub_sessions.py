"""Hub tests: tuner sharing, line-slot allocation, 503 cases, backup URLs, status."""

import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "hub"))

from app import hub as hub_module  # noqa: E402
from app.hub import Hub  # noqa: E402
from support import EngineCommand, Reader, channel, config, fake_stream_url, provider, wait_for  # noqa: E402


class HubTestCase(unittest.TestCase):
    engine_flags = ()

    def make_hub(self, cfg, **kwargs):
        self.cfg = cfg
        self.engine = EngineCommand(*self.engine_flags)
        kwargs.setdefault("restart_delay", 0.05)
        kwargs.setdefault("stop_timeout", 2.0)
        self.hub = Hub(lambda: self.cfg, stream_url=fake_stream_url, engine_command=self.engine, **kwargs)
        self.readers = []
        return self.hub

    def read(self, session, delay=0.0):
        reader = Reader(session, delay)
        reader.start()
        self.readers.append(reader)
        return reader

    def tearDown(self):
        hub = getattr(self, "hub", None)
        if hub is None:
            return
        for reader in getattr(self, "readers", []):
            reader.stop()
        hub.shutdown()
        self.assertTrue(wait_for(lambda: not hub._channels, 8.0), "channels did not wind down")
        self.assertEqual(hub._slots, {}, "slots leaked")

    def used(self, pid, lid):
        return len(self.hub._slots.get((pid, lid), ()))


class TestSharing(HubTestCase):
    def test_two_clients_share_one_engine_and_one_slot(self):
        hub = self.make_hub(config([provider("p1", 1)], [channel(1, [("p1", "100")])]))
        a = hub.open("uuid-1", "10.0.0.1", "ua")
        b = hub.open("uuid-1", "10.0.0.2", "ua")
        self.assertIsNotNone(a)
        self.assertIsNotNone(b)
        ra, rb = self.read(a), self.read(b)
        self.assertTrue(wait_for(lambda: ra.bytes > 50000 and rb.bytes > 50000))
        self.assertEqual(self.engine.count(), 1)
        self.assertEqual(self.used("p1", "l1"), 1)
        self.assertEqual(ra.misaligned + rb.misaligned, 0)
        status = hub.status()
        self.assertEqual(len(status), 1)
        self.assertEqual(sorted(c["ip"] for c in status[0]["clients"]), ["10.0.0.1", "10.0.0.2"])
        self.assertEqual(status[0]["line_id"], "l1")
        self.assertGreater(status[0]["bytes"], 0)
        lines = hub.line_status()
        self.assertEqual(lines[0]["used"], 1)
        self.assertEqual(lines[0]["channels"], [{"id": 1, "name": "Channel 1"}])

    def test_engine_command_gets_url_user_agent_and_channel_id(self):
        prov = provider("p1", 1, fmt="ts")
        prov["user_agent"] = "MyUA/1"
        hub = self.make_hub(config([prov], [channel(7, [("p1", "55")])]))
        session = hub.open("uuid-7", "10.0.0.1", "ua")
        self.read(session)
        self.assertTrue(wait_for(lambda: self.engine.count() == 1))
        self.assertEqual(self.engine.calls[0], ("ts", "fake://p1/l1/55", "MyUA/1", 7))

    def test_default_engine_command_matches_spec(self):
        cmd = hub_module.default_engine_command("hls", "http://u", "UA", 3)
        self.assertEqual(cmd[0], sys.executable)
        self.assertEqual(cmd[1], "-u")
        self.assertTrue(cmd[2].endswith(os.path.join("app", "engine", "hlsloop.py")))
        self.assertEqual(cmd[3:], ["http://u", "UA", "3"])
        self.assertTrue(hub_module.default_engine_command("ts", "u", "UA", 3)[2].endswith("tsloop.py"))

    def test_unknown_or_disabled_channel_is_none(self):
        hub = self.make_hub(config([provider("p1", 1)], [channel(1, [("p1", "1")], enabled=False)]))
        self.assertIsNone(hub.open("uuid-1", "ip", "ua"))
        self.assertIsNone(hub.open("nope", "ip", "ua"))
        self.assertEqual(hub._slots, {})


class TestLineSlots(HubTestCase):
    def test_busy_single_line_gives_503(self):
        hub = self.make_hub(config([provider("p1", 1)], [channel(1, [("p1", "1")]), channel(2, [("p1", "2")])]))
        self.read(hub.open("uuid-1", "ip", "ua"))
        self.assertIsNone(hub.open("uuid-2", "ip", "ua"))
        self.assertEqual(self.used("p1", "l1"), 1)

    def test_second_line_used_when_first_is_full(self):
        hub = self.make_hub(config([provider("p1", 1, 1)],
                                   [channel(1, [("p1", "1")]), channel(2, [("p1", "2")]),
                                    channel(3, [("p1", "3")])]))
        self.read(hub.open("uuid-1", "ip", "ua"))
        s2 = hub.open("uuid-2", "ip", "ua")
        self.assertIsNotNone(s2)
        self.read(s2)
        self.assertEqual(s2.channel.line_id, "l2")
        self.assertTrue(wait_for(lambda: self.engine.count() == 2))
        self.assertIn("fake://p1/l2/2", [c[1] for c in self.engine.calls])
        self.assertIsNone(hub.open("uuid-3", "ip", "ua"))

    def test_other_provider_stream_when_first_provider_full(self):
        hub = self.make_hub(config([provider("p1", 1), provider("p2", 2)],
                                   [channel(1, [("p1", "1")]), channel(2, [("p1", "2"), ("p2", "20")])]))
        self.read(hub.open("uuid-1", "ip", "ua"))
        s2 = hub.open("uuid-2", "ip", "ua")
        self.assertIsNotNone(s2)
        self.read(s2)
        self.assertEqual((s2.channel.provider_id, s2.channel.line_id, s2.channel.stream_id), ("p2", "l1", "20"))
        self.assertEqual(self.used("p2", "l1"), 1)

    def test_unlimited_line(self):
        hub = self.make_hub(config([provider("p1", 0)], [channel(i, [("p1", str(i))]) for i in range(1, 5)]))
        for i in range(1, 5):
            session = hub.open("uuid-%d" % i, "ip", "ua")
            self.assertIsNotNone(session)
            self.read(session)
        self.assertEqual(self.used("p1", "l1"), 4)

    def test_slot_freed_after_engine_exit_lets_next_channel_tune(self):
        hub = self.make_hub(config([provider("p1", 1)], [channel(1, [("p1", "1")]), channel(2, [("p1", "2")])]))
        reader = self.read(hub.open("uuid-1", "ip", "ua"))
        self.assertTrue(wait_for(lambda: reader.bytes > 0))
        reader.stop()
        self.assertTrue(wait_for(lambda: self.used("p1", "l1") == 0))
        session = hub.open("uuid-2", "ip", "ua")
        self.assertIsNotNone(session)
        self.read(session)

    def test_url_failure_falls_through_to_next_stream_and_releases(self):
        def flaky(prov, line, stream_id):
            if stream_id == "bad":
                raise ValueError("vanished")
            return fake_stream_url(prov, line, stream_id)

        self.make_hub(config([provider("p1", 1), provider("p2", 1)], [channel(1, [("p1", "bad"), ("p2", "ok")])]))
        self.hub._stream_url = flaky
        session = self.hub.open("uuid-1", "ip", "ua")
        self.assertIsNotNone(session)
        self.read(session)
        self.assertEqual(self.used("p1", "l1"), 0)
        self.assertEqual(self.used("p2", "l1"), 1)


class TestBackupUrls(HubTestCase):
    def test_backup_urls_use_session_line_and_free_lines_elsewhere(self):
        hub = self.make_hub(config(
            [provider("p1", 1, 1), provider("p2", 1, 1)],
            [channel(1, [("p1", "a")]), channel(2, [("p2", "b")]),
             channel(3, [("p1", "c"), ("p1", "d"), ("p2", "e"), ("missing", "z")])]))
        self.read(hub.open("uuid-1", "ip", "ua"))  # p1/l1 busy
        self.read(hub.open("uuid-2", "ip", "ua"))  # p2/l1 busy
        session = hub.open("uuid-3", "ip", "ua")  # gets p1/l2
        self.read(session)
        self.assertEqual(session.channel.line_id, "l2")
        self.assertEqual(hub.backup_urls(3), ["fake://p1/l2/c", "fake://p1/l2/d", "fake://p2/l2/e"])
        self.assertEqual(hub.backup_urls("3"), hub.backup_urls(3))

    def test_backup_urls_without_session(self):
        hub = self.make_hub(config([provider("p1", 1)], [channel(1, [("p1", "a"), ("p1", "b")])]))
        self.assertEqual(hub.backup_urls(1), ["fake://p1/l1/a", "fake://p1/l1/b"])
        self.assertEqual(hub.backup_urls(99), [])


class TestConcurrency(HubTestCase):
    def test_concurrent_opens_share_one_session(self):
        hub = self.make_hub(config([provider("p1", 1)], [channel(1, [("p1", "1")])]))
        results = []
        lock = threading.Lock()

        def opener():
            session = hub.open("uuid-1", "ip", "ua")
            with lock:
                results.append(session)

        threads = [threading.Thread(target=opener) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertTrue(all(results))
        self.assertEqual(len({id(s.channel) for s in results}), 1)
        for session in results:
            self.read(session)
        self.assertTrue(wait_for(lambda: self.engine.count() == 1))
        time.sleep(0.2)
        self.assertEqual(self.engine.count(), 1)
        self.assertEqual(self.used("p1", "l1"), 1)

    def test_slot_accounting_stress(self):
        limits = {"l1": 2, "l2": 1}
        hub = self.make_hub(config([provider("p1", 2, 1)], [channel(i, [("p1", str(i))]) for i in range(1, 9)]),
                            stop_timeout=1.0)
        violations = []
        errors = []
        stop = threading.Event()

        def check():
            with hub._lock:
                for lid, limit in limits.items():
                    holders = hub._slots.get(("p1", lid), [])
                    if len(holders) > limit or len(set(holders)) != len(holders):
                        violations.append((lid, list(holders)))
                live = [c for c in hub._channels.values() if c.slot_held]
                if len(live) != sum(len(v) for v in hub._slots.values()):
                    violations.append(("mismatch", len(live), dict(hub._slots)))

        def worker(seed):
            import random
            rng = random.Random(seed)
            try:
                while not stop.is_set():
                    session = hub.open("uuid-%d" % rng.randint(1, 8), "ip", "ua")
                    check()
                    if session is None:
                        time.sleep(rng.random() * 0.01)
                        continue
                    for i, _ in enumerate(session.iter_bytes()):
                        if i >= rng.randint(0, 3):
                            break
                    session.close()
                    session.close()  # closing twice must be harmless
                    check()
            except Exception as exc:  # pragma: no cover - reported below
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(n,), daemon=True) for n in range(12)]
        for t in threads:
            t.start()
        time.sleep(4.0)
        stop.set()
        for t in threads:
            t.join(10)
        self.assertEqual(errors, [])
        self.assertEqual(violations, [])
        self.assertTrue(wait_for(lambda: not hub._channels, 8.0))
        self.assertEqual(hub._slots, {})
        self.assertGreater(self.engine.count(), 3)


if __name__ == "__main__":
    unittest.main()
