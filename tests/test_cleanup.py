"""Daily cleanup: removes stale engine state and orphaned caches, keeps everything in use."""

import os
import tempfile
import time
import unittest

from app import cleanup


def touch(path, age=0):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("x")
    t = time.time() - age
    os.utime(path, (t, t))


class CleanupTest(unittest.TestCase):
    def test_prunes_stale_and_keeps_live(self):
        old = 2 * cleanup.DAY
        with tempfile.TemporaryDirectory() as d:
            tmp, data = os.path.join(d, "tmp"), os.path.join(d, "data")
            c = os.path.join(data, "cache")
            files = {
                # removed
                os.path.join(tmp, "hlsloop-abc.json"): (old, False),
                os.path.join(tmp, "tsloop-abc~1.json"): (old, False),
                os.path.join(c, "hlsloop-jobs-9.json"): (old, False),
                os.path.join(c, ".tmp-dl-1.xml"): (old, False),
                os.path.join(c, "catalog-gone.json"): (old, False),
                os.path.join(c, "xmltv-ids-gone.json"): (old, False),
                os.path.join(c, "xmltv-oldlineup.xml"): (old, False),
                os.path.join(c, "gracenote-index-USA10954.json"): (40 * cleanup.DAY, False),
                os.path.join(c, "history", "99.jsonl"): (old, False),
                # kept
                os.path.join(tmp, "hlsloop-live.json"): (5, True),
                os.path.join(tmp, "other.txt"): (old, True),
                os.path.join(c, "catalog-p1.json"): (old, True),
                os.path.join(c, "xmltv-ids-p1.json"): (old, True),
                os.path.join(c, "xmltv-ids-x1.json"): (old, True),
                os.path.join(c, "xmltv-main.xml"): (old, True),
                os.path.join(c, "gracenote-index-USA10954x.json"): (old, True),
                os.path.join(c, "history", "7.jsonl"): (old, True),
                os.path.join(c, "catalog-gone2.json"): (60, True),
                os.path.join(data, "config.json"): (old, True),
            }
            for path, (age, _) in files.items():
                touch(path, age)
            cfg = {"providers": [{"id": "p1"}], "xmltv_sources": [{"id": "x1"}],
                   "lineups": [{"id": "main"}], "channels": [{"id": 7}]}
            n = cleanup.run(cfg, data, tmp_dir=tmp)
            self.assertEqual(n, sum(1 for _, keep in files.values() if not keep))
            for path, (_, keep) in files.items():
                self.assertEqual(os.path.exists(path), keep, path)

    def test_missing_folders_are_fine(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(cleanup.run({}, os.path.join(d, "nope"), tmp_dir=os.path.join(d, "nope2")), 0)
