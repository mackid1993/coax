"""Feed health: block bad streams, fix the order once on trouble, never rubber-band, never delete."""

import copy
import tempfile
import unittest

from app import feedhealth as fh

H = fh.HOUR


class Store:
    def __init__(self, cfg):
        self.cfg = cfg

    def get(self):
        return self.cfg

    def update(self, mutate):
        c = copy.deepcopy(self.cfg)
        r = mutate(c)
        self.cfg = c
        return r


def ids(store):
    return [s["stream_id"] for s in store.cfg["channels"][0]["streams"]]


def K(s):
    return ("p1", s)


class FeedHealthTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, *sids):
        st = Store({"channels": [{"id": 5, "name": "Bravo", "streams": [{"provider": "p1", "stream_id": s} for s in sids]}]})
        return st, fh.FeedHealth(st, self.tmp.name)

    def test_desync_backup_blocked_24h_and_moved_last_never_deleted(self):
        st, h = self.make("main", "b2", "b3")
        h.problem(5, K("b3"), "desync", "12 seconds ahead of its picture", now=1000)
        self.assertEqual(ids(st), ["main", "b2", "b3"])
        self.assertEqual(h.blocked_until(5, K("b3"), now=1000), 1000 + fh.DAY)
        n = h.list()["items"][0]
        self.assertEqual(n["kind"], "blocked")
        self.assertIn("24 hours", n["text"])

    def test_bad_main_hands_over_to_proven_backup(self):
        st, h = self.make("main", "b2", "b3")
        h.good(5, K("b3"), 60, now=500)
        h.problem(5, K("main"), "placeholder", now=1000)
        self.assertEqual(ids(st), ["b3", "b2", "main"])
        self.assertEqual(h.list()["items"][0]["kind"], "promoted")
        self.assertIn("12 hours", h.list()["items"][0]["text"])

    def test_untested_backup_ranks_below_proven_one(self):
        st, h = self.make("main", "untested", "proven")
        h.good(5, K("proven"), 10, now=500)
        h.problem(5, K("main"), "desync", now=1000)
        self.assertEqual(ids(st)[0], "proven")

    def test_no_rubber_band_when_block_ends(self):
        st, h = self.make("main", "b2")
        h.problem(5, K("main"), "placeholder", now=1000)
        self.assertEqual(ids(st), ["b2", "main"])
        later = 1000 + 13 * H
        self.assertFalse(h.is_blocked(5, K("main"), now=later))
        h.good(5, K("main"), 500, now=later)
        self.assertEqual(ids(st), ["b2", "main"])          # nothing moves without trouble

    def test_absorbed_problems_ignored(self):
        st, h = self.make("main", "b2")
        for i, kind in enumerate(("stall", "keepalive", "avsplit", "stall")):
            h.problem(5, K("main"), kind, now=1000 + i * 600)
        self.assertEqual(ids(st), ["main", "b2"])
        self.assertFalse(h.is_blocked(5, K("main"), now=4000))

    def test_frozen_three_times_in_an_hour_blocks(self):
        st, h = self.make("main", "b2")
        h.problem(5, K("main"), "frozen", now=1000)
        h.problem(5, K("main"), "frozen", now=1000 + 600)
        self.assertEqual(ids(st), ["main", "b2"])
        h.problem(5, K("main"), "frozen", now=1000 + 1200)
        self.assertEqual(ids(st), ["b2", "main"])

    def test_dead_blocked_only_when_another_stream_takes_over(self):
        st, h = self.make("main", "b2")
        h.problem(5, K("main"), "dead", now=1000)
        self.assertEqual(ids(st), ["main", "b2"])           # could be the network
        h.switched(5, K("main"), K("b2"), now=1030)
        self.assertEqual(ids(st), ["b2", "main"])
        self.assertTrue(h.is_blocked(5, K("main"), now=1030))

    def test_repeat_offender_block_grows(self):
        st, h = self.make("main", "b2")
        h.problem(5, K("b2"), "placeholder", now=1000)
        self.assertEqual(h.blocked_until(5, K("b2"), now=1000), 1000 + 12 * H)
        t = 1000 + 13 * H
        h.problem(5, K("b2"), "placeholder", now=t)
        self.assertEqual(h.blocked_until(5, K("b2"), now=t), t + fh.DAY)

    def test_single_stream_channel_untouched(self):
        st, h = self.make("main")
        h.problem(5, K("main"), "desync", now=1000)
        self.assertFalse(h.is_blocked(5, K("main"), now=1000))
        self.assertEqual(h.list()["items"], [])

    def test_undo_restores_order_and_lifts_block(self):
        st, h = self.make("main", "b2")
        h.problem(5, K("main"), "placeholder", now=1000)
        h.undo(h.list()["items"][0]["id"])
        self.assertEqual(ids(st), ["main", "b2"])
        self.assertFalse(h.is_blocked(5, K("main")))

    def test_state_persists(self):
        st, h = self.make("main", "b2")
        h.problem(5, K("b2"), "desync", now=1000)
        self.assertTrue(fh.FeedHealth(st, self.tmp.name).is_blocked(5, K("b2"), now=1000))
