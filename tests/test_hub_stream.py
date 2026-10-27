"""Hub tests: ring buffer, join-behind, alignment, slow clients, restarts, shutdown delay."""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "hub"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from support import Reader, channel, config, provider, wait_for  # noqa: E402
from test_hub_sessions import HubTestCase  # noqa: E402

RATE = 188 * 1000  # fake engine default, bytes per second


def one_channel(behind=0, delay=0):
    return config([provider("p1", 1)], [channel(1, [("p1", "1")])], behind=behind, delay=delay)


class TestAlignment(HubTestCase):
    engine_flags = ("--junk", "50", "--odd-writes")

    def test_output_is_packet_aligned_despite_junk_and_split_writes(self):
        hub = self.make_hub(one_channel())
        reader = self.read(hub.open("uuid-1", "ip", "ua"))
        self.assertTrue(wait_for(lambda: reader.chunks > 20))
        self.assertEqual(reader.misaligned, 0)
        self.assertEqual(reader.first_counter, 0)


class TestJoinBehind(HubTestCase):
    def test_new_client_starts_about_behind_seconds_back(self):
        hub = self.make_hub(one_channel(behind=1))
        live = self.read(hub.open("uuid-1", "ip", "ua"))
        self.assertTrue(wait_for(lambda: live.bytes > RATE * 3, 8))
        late = self.read(hub.open("uuid-1", "ip2", "ua"))
        head = live.last_counter
        self.assertTrue(wait_for(lambda: late.first_counter is not None))
        behind_packets = head - late.first_counter
        # 1 second at 1000 packets/s, allowing for scheduling jitter and chunking.
        self.assertGreater(behind_packets, 600)
        self.assertLess(behind_packets, 1600)
        self.assertEqual(late.misaligned, 0)

    def test_zero_behind_starts_at_live(self):
        hub = self.make_hub(one_channel(behind=0))
        live = self.read(hub.open("uuid-1", "ip", "ua"))
        self.assertTrue(wait_for(lambda: live.bytes > RATE * 2, 8))
        late = self.read(hub.open("uuid-1", "ip2", "ua"))
        head = live.last_counter
        self.assertTrue(wait_for(lambda: late.first_counter is not None))
        self.assertLess(head - late.first_counter, 300)

    def test_young_buffer_starts_at_oldest_data(self):
        hub = self.make_hub(one_channel(behind=30))
        live = self.read(hub.open("uuid-1", "ip", "ua"))
        self.assertTrue(wait_for(lambda: live.bytes > RATE, 8))
        late = self.read(hub.open("uuid-1", "ip2", "ua"))
        self.assertTrue(wait_for(lambda: late.first_counter is not None))
        self.assertEqual(late.first_counter, 0)

    def test_ring_is_bounded_by_bitrate(self):
        hub = self.make_hub(one_channel(behind=1), ring_margin_seconds=0.5, ring_min_bytes=0)
        reader = self.read(hub.open("uuid-1", "ip", "ua"))
        self.assertTrue(wait_for(lambda: reader.bytes > RATE * 3, 8))
        ring = hub._channels["uuid-1"].ring_bytes
        self.assertLess(ring, RATE * 1.5 * 1.5)
        self.assertGreater(ring, RATE * 0.8)


class TestSlowClient(HubTestCase):
    def test_slow_client_dropped_fast_client_continues(self):
        hub = self.make_hub(one_channel(), ring_min_bytes=0, ring_max_bytes=150_000)
        fast = self.read(hub.open("uuid-1", "fast", "ua"))
        slow = self.read(hub.open("uuid-1", "slow", "ua"), delay=30)  # takes one chunk, then stalls
        self.assertTrue(wait_for(lambda: hub._channels["uuid-1"].clients == [fast.session], 6))
        self.assertTrue(slow.session.dropped)
        before = fast.bytes
        self.assertTrue(wait_for(lambda: fast.bytes > before + RATE, 5))
        self.assertFalse(fast.finished.is_set())
        self.assertEqual(fast.misaligned, 0)
        # The stalled reader ends on its next read.
        self.assertIsNone(hub._read(slow.session))


class TestRestart(HubTestCase):
    engine_flags = ("--exit-after", "0.4")

    def test_engine_crash_restarts_without_dropping_clients(self):
        hub = self.make_hub(one_channel())
        a = self.read(hub.open("uuid-1", "a", "ua"))
        b = self.read(hub.open("uuid-1", "b", "ua"))
        self.assertTrue(wait_for(lambda: self.engine.count() >= 3, 6))
        before = a.bytes
        self.assertTrue(wait_for(lambda: a.bytes > before + 20000, 3))
        self.assertFalse(a.finished.is_set())
        self.assertFalse(b.finished.is_set())
        self.assertEqual(self.used("p1", "l1"), 1)
        self.assertEqual(a.misaligned + b.misaligned, 0)
        self.assertGreaterEqual(hub.status()[0]["restarts"], 2)

    def test_restart_limit_ends_channel_and_releases_slot(self):
        hub = self.make_hub(one_channel(), restart_limit=2, restart_window=60)
        reader = self.read(hub.open("uuid-1", "a", "ua"))
        self.assertTrue(reader.finished.wait(8))
        self.assertEqual(self.engine.count(), 3)  # first run plus two restarts
        self.assertTrue(wait_for(lambda: not hub._channels))
        self.assertEqual(hub._slots, {})

    def test_engine_exit_with_no_clients_during_delay_releases_once(self):
        self.cfg = None
        hub = self.make_hub(one_channel(delay=5))
        reader = self.read(hub.open("uuid-1", "a", "ua"))
        self.assertTrue(wait_for(lambda: reader.bytes > 0))
        reader.stop()
        self.assertTrue(wait_for(lambda: not hub._channels, 5))
        self.assertEqual(self.engine.count(), 1)
        self.assertEqual(hub._slots, {})


class TestShutdownDelay(HubTestCase):
    def test_engine_kept_for_delay_and_reused(self):
        hub = self.make_hub(one_channel(delay=0.8))
        first = self.read(hub.open("uuid-1", "a", "ua"))
        self.assertTrue(wait_for(lambda: first.bytes > 0))
        first.stop()
        time.sleep(0.4)
        self.assertIn("uuid-1", hub._channels)
        self.assertEqual(self.used("p1", "l1"), 1)
        second = self.read(hub.open("uuid-1", "b", "ua"))  # within the delay: same engine
        time.sleep(0.8)
        self.assertEqual(self.engine.count(), 1)
        self.assertIn("uuid-1", hub._channels)
        second.stop()
        self.assertTrue(wait_for(lambda: "uuid-1" not in hub._channels, 3))
        self.assertEqual(hub._slots, {})
        self.assertEqual(self.engine.count(), 1)

    def test_zero_delay_stops_immediately(self):
        hub = self.make_hub(one_channel(delay=0))
        reader = self.read(hub.open("uuid-1", "a", "ua"))
        self.assertTrue(wait_for(lambda: reader.bytes > 0))
        reader.stop()
        self.assertTrue(wait_for(lambda: not hub._channels, 2))

    def test_reopen_while_stopping_waits_and_gets_new_engine(self):
        hub = self.make_hub(one_channel(delay=0))
        reader = self.read(hub.open("uuid-1", "a", "ua"))
        self.assertTrue(wait_for(lambda: reader.bytes > 0))
        reader.stop()
        session = hub.open("uuid-1", "b", "ua")
        self.assertIsNotNone(session)
        again = self.read(session)
        self.assertTrue(wait_for(lambda: again.bytes > 0))
        self.assertEqual(self.engine.count(), 2)
        self.assertEqual(self.used("p1", "l1"), 1)


class TestReconnect(HubTestCase):
    """The common case: Channels DVR is the only client and reconnects or retunes."""

    def test_reconnect_within_delay_joins_behind_with_burst(self):
        hub = self.make_hub(one_channel(behind=1, delay=3))
        old = self.read(hub.open("uuid-1", "dvr", "ua"))
        self.assertTrue(wait_for(lambda: old.bytes > RATE * 3, 8))
        old.stop()
        self.assertTrue(old.finished.wait(2))
        self.assertEqual(hub._channels["uuid-1"].clients, [])
        time.sleep(0.3)
        start = time.monotonic()
        new = self.read(hub.open("uuid-1", "dvr", "ua"))
        # About one second of backlog arrives at once, far faster than real time.
        self.assertTrue(wait_for(lambda: new.bytes >= RATE * 0.6, 0.4))
        self.assertLess(time.monotonic() - start, 0.4)
        self.assertGreater(old.last_counter, new.first_counter)  # overlaps what the old one saw
        self.assertEqual(new.misaligned, 0)
        self.assertEqual(self.engine.count(), 1)
        self.assertEqual(self.used("p1", "l1"), 1)
        self.assertEqual([c["ip"] for c in hub.status()[0]["clients"]], ["dvr"])

    def test_overlapping_retune_keeps_engine_and_single_slot(self):
        hub = self.make_hub(one_channel(behind=1, delay=0))
        old = self.read(hub.open("uuid-1", "dvr", "ua"))
        self.assertTrue(wait_for(lambda: old.bytes > RATE * 2, 8))
        new = self.read(hub.open("uuid-1", "dvr", "ua"))  # new connection before the old one closes
        old.stop()  # delay is 0, but a client remains, so the engine must stay
        self.assertTrue(old.finished.wait(2))
        before = new.bytes
        self.assertTrue(wait_for(lambda: new.bytes > before + RATE, 5))
        self.assertEqual(self.engine.count(), 1)
        self.assertEqual(self.used("p1", "l1"), 1)
        self.assertEqual(len(hub.status()[0]["clients"]), 1)
        new.stop()
        self.assertTrue(wait_for(lambda: not hub._channels, 3))
        self.assertEqual(hub._slots, {})

    def test_repeated_reconnects_never_double_count(self):
        hub = self.make_hub(config([provider("p1", 1)], [channel(1, [("p1", "1")]), channel(2, [("p1", "2")])],
                                   behind=1, delay=0.5))
        for _ in range(5):
            reader = self.read(hub.open("uuid-1", "dvr", "ua"))
            self.assertTrue(wait_for(lambda: reader.bytes > 0))
            reader.stop()
            self.assertTrue(reader.finished.wait(2))
            self.assertEqual(self.used("p1", "l1"), 1)
            self.assertIsNone(hub.open("uuid-2", "dvr", "ua"))  # slot still held during the delay
        self.assertEqual(self.engine.count(), 1)
        self.assertTrue(wait_for(lambda: not hub._channels, 3))
        self.assertEqual(hub._slots, {})
        self.assertIsNotNone(hub.open("uuid-2", "dvr", "ua"))


class TestStubbornEngine(HubTestCase):
    engine_flags = ("--ignore-term",)

    def test_engine_ignoring_sigterm_is_killed(self):
        hub = self.make_hub(one_channel(delay=0), stop_timeout=0.5)
        reader = self.read(hub.open("uuid-1", "a", "ua"))
        self.assertTrue(wait_for(lambda: reader.bytes > 0))
        proc = hub._channels["uuid-1"].proc
        reader.stop()
        self.assertTrue(wait_for(lambda: not hub._channels, 5))
        self.assertIsNotNone(proc.poll())


if __name__ == "__main__":
    unittest.main()
