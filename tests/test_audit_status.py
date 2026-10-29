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


if __name__ == "__main__":
    unittest.main()
