"""Status card and Smart Standby fixes from the Oct 29 targeted audit (1.0.6-7)."""
import tempfile
import unittest

from app import feedhealth as fh
from app import hub as hubmod
from tests.fixtures.hub.support import EngineCommand, channel, config, fake_stream_url, provider


class Store:
    def __init__(self, cfg):
        self.cfg = cfg

    def get(self):
        return self.cfg

    def update(self, mutate):
        return mutate(self.cfg)


class StatusCardFixes(unittest.TestCase):
    def setUp(self):
        self._delay, hubmod.RESTART_DELAY = hubmod.RESTART_DELAY, 0.05
        self.cfg = config([provider("p1", 2)], [channel(1, [("p1", "A"), ("p1", "B"), ("p1", "C")])])
        self.hub = hubmod.Hub(lambda: self.cfg, stream_url=fake_stream_url, engine_command=EngineCommand())
        self.health = fh.FeedHealth(Store(self.cfg), tempfile.mkdtemp())
        self.hub.health = self.health
        self.health.problem(1, ("p1", "A"), "placeholder")   # A blocked; nothing proven, so the order stays
        self.session = self.hub.open(self.cfg["channels"][0]["uuid"], "ip", "ua")
        self.addCleanup(self.session.close)

    def tearDown(self):
        hubmod.RESTART_DELAY = self._delay

    def card(self):
        return next(x for x in self.hub.status() if x["channel_id"] == self.session.channel_id)

    def test_disabling_the_playing_stream_never_switches_to_a_blocked_one(self):
        self.assertEqual(self.session.stream_id, "B")
        self.cfg["channels"][0]["streams"][1]["disabled"] = True
        self.hub.feed_disabled(1, "p1", "B")
        self.assertEqual(tuple(self.session.cur_key), ("p1", "C"))

    def test_the_badge_counts_the_stream_playing_now_in_the_current_order(self):
        self.assertEqual(self.card()["stream_no"], 2)        # the tune skipped blocked A: not "Main stream"
        streams = self.cfg["channels"][0]["streams"]
        streams.insert(0, streams.pop(1))                    # B dragged to the top
        self.assertEqual(self.card()["stream_no"], 1)


class SmartStandbyUndo(unittest.TestCase):
    def setUp(self):
        self.cfg = config([provider("p1", 2)], [channel(1, [("p1", "A"), ("p1", "B"), ("p1", "C")])])
        self.health = fh.FeedHealth(Store(self.cfg), tempfile.mkdtemp())

    def order(self):
        return [s["stream_id"] for s in self.cfg["channels"][0]["streams"]]

    def test_undo_never_restores_an_old_order_over_a_newer_drag(self):
        self.health.problem(1, ("p1", "B"), "placeholder")
        note = self.health.list()["items"][0]
        streams = self.cfg["channels"][0]["streams"]
        streams.insert(0, streams.pop(2))                     # the user drags C to the top
        before = self.order()
        with self.assertRaises(ValueError):
            self.health.undo(note["id"])
        self.assertEqual(self.order(), before)

    def test_undo_still_works_when_nothing_moved_since(self):
        self.health.problem(1, ("p1", "B"), "placeholder")
        self.health.undo(self.health.list()["items"][0]["id"])
        self.assertEqual(self.order(), ["A", "B", "C"])

    def test_an_unblocked_stream_is_not_blocked_again_at_its_next_failure(self):
        import time
        t = time.time() - 5 * 3600
        for i in range(fh.DEAD_REPEAT):                       # dies three times while another takes over
            now = t + i * (fh.DEAD_CONFIRM + 60)
            self.health.problem(1, ("p1", "A"), "dead", now=now)
            self.health.switched(1, ("p1", "A"), ("p1", "B"), now=now + 5)
        self.assertTrue(self.health.is_blocked(1, ("p1", "A")))
        self.health.unblock(1, ("p1", "A"))
        now = time.time()
        self.health.problem(1, ("p1", "A"), "dead", now=now)
        self.health.switched(1, ("p1", "A"), ("p1", "B"), now=now + 5)
        self.assertFalse(self.health.is_blocked(1, ("p1", "A"), now=now + 10))


if __name__ == "__main__":
    unittest.main()
