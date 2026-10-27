import os as _os

_os.environ.setdefault("COAX_SKIP_URL_CHECK", "1")

import unittest

from app.guide import gracenote


class ProgressTests(unittest.TestCase):
    def test_bulk_suggest_reports_progress(self):
        seen = []
        chans = [{"id": i, "name": "Channel %d" % i} for i in range(1, 5)]
        gracenote.bulk_suggest(chans, cdvr_url="", search_fn=lambda u, q: [], progress=seen.append)
        self.assertEqual(seen, [1, 2, 3, 4])


if __name__ == "__main__":
    unittest.main()
