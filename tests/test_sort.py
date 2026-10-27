import unittest

from app.guide import output


class SortTests(unittest.TestCase):
    def setUp(self):
        self.chs = {"1": {"id": 1, "name": "Zebra TV", "_pnum": "30"},
                    "2": {"id": 2, "name": "alpha one", "_pnum": "4"},
                    "3": {"id": 3, "name": "Mid Channel", "_pnum": ""}}

    def test_modes(self):
        l = {"channels": [1, 2, 3]}
        self.assertEqual(output.ordered_ids(dict(l, sort="manual"), self.chs), [1, 2, 3])
        self.assertEqual(output.ordered_ids(dict(l, sort="alpha"), self.chs), [2, 3, 1])
        self.assertEqual(output.ordered_ids(dict(l, sort="provider"), self.chs), [2, 1, 3])


if __name__ == "__main__":
    unittest.main()
