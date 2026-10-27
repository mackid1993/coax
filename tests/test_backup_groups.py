import os as _os

_os.environ.setdefault("COAX_SKIP_URL_CHECK", "1")

import unittest

from app import backups


class SelectedGroupsTests(unittest.TestCase):
    def test_only_selected_groups_are_candidates(self):
        streams = [{"stream_id": "1", "name": "PL| COMEDY CENTRAL", "group": "PL| ENTERTAINMENT"},
                   {"stream_id": "2", "name": "US| COMEDY CENTRAL HD", "group": "US| ENTERTAINMENT"}]
        selected = {"US| ENTERTAINMENT"}
        cat = {"p1": {"streams": [s for s in streams if s["group"] in selected]}}
        ids = {r["stream_id"] for r in backups.suggest({"name": "COMEDY CENTRAL", "streams": []}, cat)}
        self.assertEqual(ids, {"2"})


if __name__ == "__main__":
    unittest.main()
