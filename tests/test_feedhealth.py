"""Feed health: out-of-sync and constantly failing streams are removed, a reliable backup takes over."""

import copy
import os
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


def cfg(*sids):
    return {"channels": [{"id": 5, "name": "Ch", "streams": [{"provider": "p1", "stream_id": s} for s in sids]}]}


def ids(store):
    return [s["stream_id"] for s in store.cfg["channels"][0]["streams"]]


class FeedHealthTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, *sids):
        st = Store(cfg(*sids))
        return st, fh.FeedHealth(st, self.tmp.name)

    def test_desync_removes_stream_notifies_and_undo_restores(self):
        st, h = self.make("a", "b", "c")
        h.desync(5, ("p1", "c"), 11.8, True)
        self.assertEqual(ids(st), ["a", "b"])
        n = h.list()
        self.assertEqual(n["unread"], 1)
        self.assertIn("ahead of its picture", n["items"][0]["text"])
        h.undo(n["items"][0]["id"])
        self.assertEqual(ids(st), ["a", "b", "c"])
        self.assertTrue(h.list()["items"][0]["undone"])
        self.assertTrue(os.path.exists(os.path.join(self.tmp.name, "notifications.json")))

    def test_single_stream_channel_is_never_changed(self):
        st, h = self.make("a")
        h.desync(5, ("p1", "a"), 12, True)
        for i in range(10):
            h.fail(5, ("p1", "a"), now=1000 + i * 3600)
        self.assertEqual(ids(st), ["a"])
        self.assertEqual(h.list()["items"], [])

    def test_constantly_failing_backup_removed_only_when_another_plays_well(self):
        st, h = self.make("a", "b")
        t = 100000
        for i in range(fh.REMOVE_FAILS):
            h.fail(5, ("p1", "b"), now=t + i * fh.FAIL_WINDOW)
        self.assertEqual(ids(st), ["a", "b"])            # nothing played well yet: maybe a provider outage
        h.good(5, ("p1", "a"), fh.OTHER_GOOD, now=t + 7 * fh.FAIL_WINDOW)
        self.assertEqual(ids(st), ["a"])
        self.assertEqual(h.list()["items"][0]["kind"], "removed")

    def test_failures_in_one_half_hour_count_once(self):
        st, h = self.make("a", "b")
        h.good(5, ("p1", "a"), 600, now=1000)
        for i in range(20):
            h.fail(5, ("p1", "b"), now=1000 + i * 60)
        self.assertEqual(ids(st), ["a", "b"])

    def test_reliable_backup_becomes_main_once_a_day(self):
        st, h = self.make("a", "b", "c")
        t = 200000
        h.good(5, ("p1", "c"), fh.PROMOTE_GOOD, now=t)
        for i in range(fh.PROMOTE_FAILS):
            h.fail(5, ("p1", "a"), now=t + i * fh.FAIL_WINDOW)
        self.assertEqual(ids(st), ["c", "a", "b"])
        note = h.list()["items"][0]
        self.assertEqual(note["kind"], "promoted")
        # the new main stream failing right away does not flip it back the same day
        for i in range(fh.PROMOTE_FAILS):
            h.fail(5, ("p1", "c"), now=t + 3600 + i * fh.FAIL_WINDOW)
        self.assertEqual(ids(st)[0], "c")
        h.undo(note["id"])
        self.assertEqual(ids(st), ["a", "b", "c"])

    def test_backup_with_a_failure_is_not_promoted(self):
        st, h = self.make("a", "b")
        h.good(5, ("p1", "b"), 600, now=1000)
        h.fail(5, ("p1", "b"), now=1100)
        for i in range(fh.PROMOTE_FAILS):
            h.fail(5, ("p1", "a"), now=2000 + i * fh.FAIL_WINDOW)
        self.assertEqual(ids(st), ["a", "b"])

    def test_mark_read(self):
        st, h = self.make("a", "b")
        h.desync(5, ("p1", "b"), 5, False)
        self.assertEqual(h.mark_read()["unread"], 0)
