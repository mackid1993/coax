"""Feed health: problems reorder streams at once; long-bad streams are removed; notes and Undo."""

import copy
import tempfile
import unittest

from app import feedhealth as fh


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


class FeedHealthTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, *sids):
        st = Store({"channels": [{"id": 5, "name": "Ch", "streams": [{"provider": "p1", "stream_id": s} for s in sids]}]})
        return st, fh.FeedHealth(st, self.tmp.name)

    def test_desync_on_backup_moves_it_last_at_once_and_notes(self):
        st, h = self.make("a", "b", "c")
        h.problem(5, ("p1", "b"), "desync", "12 seconds ahead of its picture", now=1000)
        self.assertEqual(ids(st), ["a", "c", "b"])
        n = h.list()["items"][0]
        self.assertEqual(n["kind"], "reordered")
        self.assertIn("sound was off its picture", n["text"])
        self.assertIn("off its picture", h.channel_notes()["5"][0]["text"])

    def test_dead_main_stream_hands_over_to_best_backup_immediately(self):
        st, h = self.make("a", "b", "c")
        h.problem(5, ("p1", "b"), "stall", now=900)
        h.problem(5, ("p1", "a"), "dead", now=1000)
        self.assertEqual(ids(st), ["c", "b", "a"])
        self.assertEqual(h.list()["items"][0]["kind"], "promoted")

    def test_one_hiccup_does_not_reshuffle_main(self):
        st, h = self.make("a", "b")
        h.problem(5, ("p1", "a"), "stall", now=1000)
        self.assertEqual(ids(st), ["a", "b"])

    def test_steady_minor_problems_on_main_promote_a_clean_backup(self):
        st, h = self.make("a", "b")
        for i in range(4):
            h.problem(5, ("p1", "a"), "frozen", now=1000 + i * 600)
        self.assertEqual(ids(st), ["b", "a"])

    def test_recovered_stream_earns_its_place_back(self):
        st, h = self.make("a", "b")
        h.problem(5, ("p1", "a"), "dead", now=1000)
        self.assertEqual(ids(st), ["b", "a"])
        t = 1000 + 2 * 3600
        for i in range(6):
            h.problem(5, ("p1", "b"), "frozen", now=t + i * 600)
        self.assertEqual(ids(st), ["a", "b"])

    def test_second_desync_removes_stream_and_undo_restores(self):
        st, h = self.make("a", "b", "c")
        h.problem(5, ("p1", "c"), "desync", now=1000)
        h.problem(5, ("p1", "c"), "desync", now=1000 + 600)
        self.assertEqual(ids(st), ["a", "b"])
        n = h.list()["items"][0]
        self.assertEqual(n["kind"], "removed")
        h.undo(n["id"])
        self.assertIn("c", ids(st))

    def test_long_bad_backup_removed(self):
        st, h = self.make("a", "b")
        h.good(5, ("p1", "a"), 300, now=500)
        for hour in range(fh.REMOVE_HOURS):
            for j in range(3):
                h.problem(5, ("p1", "b"), "dead", now=1000 + hour * 3600 + j * 300)
        self.assertEqual(ids(st), ["a"])

    def test_same_problem_within_two_minutes_counts_once(self):
        st, h = self.make("a", "b")
        for i in range(10):
            h.problem(5, ("p1", "b"), "stall", now=1000 + i)
        self.assertEqual(len(h.feeds["5|p1|b"]["ev"]), 1)

    def test_single_stream_never_changed(self):
        st, h = self.make("a")
        for i in range(5):
            h.problem(5, ("p1", "a"), "desync", now=1000 + i * 600)
        self.assertEqual(ids(st), ["a"])
        self.assertEqual(h.list()["items"], [])

    def test_bad_stream_already_last_is_still_noted(self):
        st, h = self.make("a", "b")
        h.problem(5, ("p1", "b"), "dead", now=1000)
        self.assertEqual(ids(st), ["a", "b"])
        n = h.list()["items"][0]
        self.assertEqual(n["kind"], "flagged")
        self.assertIsNone(n["undo"])

    def test_mark_read_and_persist(self):
        st, h = self.make("a", "b")
        h.problem(5, ("p1", "b"), "desync", now=1000)
        self.assertEqual(h.mark_read()["unread"], 0)
        h2 = fh.FeedHealth(st, self.tmp.name)
        self.assertTrue(h2.channel_notes()["5"])
