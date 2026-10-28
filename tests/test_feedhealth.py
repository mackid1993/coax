"""Feed health: block bad streams, only checked streams become main, never rubber-band, bell X / Clear all."""

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

    def test_desync_backup_blocked_24h_and_moved_last(self):
        st, h = self.make("main", "b2", "b3")
        h.problem(5, K("b3"), "desync", "12 seconds ahead of its picture", now=1000)
        self.assertEqual(ids(st), ["main", "b2", "b3"])
        self.assertEqual(h.blocked_until(5, K("b3"), now=1000), 1000 + fh.DAY)
        self.assertIn("24 hours", h.list()["items"][0]["text"])

    def test_bad_main_hands_over_to_checked_backup(self):
        st, h = self.make("main", "b2", "b3")
        h.good(5, K("b3"), 60, now=500)
        h.problem(5, K("main"), "placeholder", now=1000)
        self.assertEqual(ids(st), ["b3", "b2", "main"])
        self.assertEqual(h.list()["items"][0]["kind"], "promoted")

    def test_sync_checked_stream_can_take_over(self):
        st, h = self.make("main", "b2")
        h.verified(5, K("b2"), now=900)
        h.problem(5, K("main"), "placeholder", now=1000)
        self.assertEqual(ids(st), ["b2", "main"])

    def test_unchecked_stream_never_becomes_main(self):
        st, h = self.make("main", "b2", "b3")
        h.problem(5, K("main"), "placeholder", now=1000)
        self.assertEqual(ids(st), ["main", "b2", "b3"])     # nothing checked: order kept
        self.assertTrue(h.is_blocked(5, K("main"), now=1000))  # but main is skipped when tuning
        self.assertEqual(h.list()["items"][0]["kind"], "blocked")

    def test_one_dead_switch_does_not_block(self):
        st, h = self.make("main", "b2")
        h.good(5, K("b2"), 30, now=500)
        h.problem(5, K("main"), "dead", now=1000)
        h.switched(5, K("main"), K("b2"), now=1030)
        self.assertEqual(ids(st), ["b2", "main"])          # stable backup moves up, main is not blocked
        self.assertFalse(h.is_blocked(5, K("main"), now=1030))

    def test_dead_three_times_in_six_hours_blocks(self):
        st, h = self.make("main", "b2")
        h.good(5, K("b2"), 30, now=500)
        for i in range(3):
            t = 1000 + i * H
            h.problem(5, K("main"), "dead", now=t)
            h.switched(5, K("main"), K("b2"), now=t + 30)
        self.assertTrue(h.is_blocked(5, K("main"), now=1000 + 2 * H + 30))
        self.assertEqual(ids(st), ["b2", "main"])

    def test_no_rubber_band_when_block_ends(self):
        st, h = self.make("main", "b2")
        h.good(5, K("b2"), 30, now=500)
        h.problem(5, K("main"), "placeholder", now=1000)
        self.assertEqual(ids(st), ["b2", "main"])
        later = 1000 + 13 * H
        self.assertFalse(h.is_blocked(5, K("main"), now=later))
        h.good(5, K("main"), 500, now=later)
        self.assertEqual(ids(st), ["b2", "main"])

    def test_absorbed_problems_ignored(self):
        st, h = self.make("main", "b2")
        for i, kind in enumerate(("stall", "keepalive", "avsplit", "stall")):
            h.problem(5, K("main"), kind, now=1000 + i * 600)
        self.assertEqual(ids(st), ["main", "b2"])
        self.assertFalse(h.is_blocked(5, K("main"), now=4000))

    def test_frozen_three_times_in_an_hour_blocks(self):
        st, h = self.make("main", "b2")
        h.good(5, K("b2"), 30, now=500)
        for i in range(3):
            h.problem(5, K("main"), "frozen", now=1000 + i * 600)
        self.assertEqual(ids(st), ["b2", "main"])

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
        h.good(5, K("b2"), 30, now=500)
        h.problem(5, K("main"), "placeholder", now=1000)
        h.undo(h.list()["items"][0]["id"])
        self.assertEqual(ids(st), ["main", "b2"])
        self.assertFalse(h.is_blocked(5, K("main")))

    def test_dismiss_and_clear(self):
        st, h = self.make("main", "b2", "b3")
        h.problem(5, K("b2"), "placeholder", now=1000)
        h.problem(5, K("b3"), "desync", now=1100)
        items = h.list()["items"]
        self.assertEqual(len(items), 2)
        self.assertEqual(len(h.dismiss(items[0]["id"])["items"]), 1)
        with self.assertRaises(KeyError):
            h.dismiss("nope")
        self.assertEqual(h.clear()["items"], [])
        self.assertEqual(fh.FeedHealth(st, self.tmp.name).list()["items"], [])

    def test_state_persists(self):
        st, h = self.make("main", "b2")
        h.problem(5, K("b2"), "desync", now=1000)
        h.verified(5, K("main"), now=1000)
        h2 = fh.FeedHealth(st, self.tmp.name)
        self.assertTrue(h2.is_blocked(5, K("b2"), now=1000))
        self.assertTrue(h2._proven(5, K("main"), 1000))


class TroublePromoteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, *sids):
        st = Store({"channels": [{"id": 5, "name": "Bravo", "streams": [{"provider": "p1", "stream_id": s} for s in sids]}]})
        return st, fh.FeedHealth(st, self.tmp.name)

    def test_trouble_then_checked_stream_becomes_main_and_stays(self):
        st, h = self.make("main", "b2", "b3")
        h.verified(5, K("b2"), now=990)                    # pre-switch check passed
        h.problem(5, K("main"), "dead", now=1000)
        h.switched(5, K("main"), K("b2"), now=1010)
        self.assertEqual(ids(st), ["b2", "main", "b3"])
        self.assertEqual(h.list()["items"][0]["kind"], "promoted")
        self.assertFalse(h.is_blocked(5, K("main"), now=1010))
        h.good(5, K("main"), 500, now=5000)               # main fine again later: no rubber band
        self.assertEqual(ids(st), ["b2", "main", "b3"])

    def test_unchecked_stream_promoted_once_it_plays_clean(self):
        st, h = self.make("main", "b2")
        h.problem(5, K("main"), "dead", now=1000)
        h.switched(5, K("main"), K("b2"), now=1010)
        self.assertEqual(ids(st), ["main", "b2"])
        h.good(5, K("b2"), 10, now=1610)
        self.assertEqual(ids(st), ["b2", "main"])

    def test_switch_without_trouble_changes_nothing(self):
        st, h = self.make("main", "b2")
        h.verified(5, K("b2"), now=990)
        h.switched(5, K("main"), K("b2"), now=1000)
        self.assertEqual(ids(st), ["main", "b2"])

    def test_backup_trouble_moves_good_backup_ahead(self):
        st, h = self.make("main", "b2", "b3")
        h.verified(5, K("b3"), now=990)
        h.problem(5, K("b2"), "dead", now=1000)
        h.switched(5, K("b2"), K("b3"), now=1010)
        self.assertEqual(ids(st), ["main", "b3", "b2"])
        self.assertEqual(h.list()["items"][0]["kind"], "reordered")

    def test_pending_expires_after_an_hour(self):
        st, h = self.make("main", "b2")
        h.problem(5, K("main"), "dead", now=1000)
        h.switched(5, K("main"), K("b2"), now=1010)
        h.good(5, K("b2"), 10, now=1010 + 2 * H)
        self.assertEqual(ids(st), ["main", "b2"])
