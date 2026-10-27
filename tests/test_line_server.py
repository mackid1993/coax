import unittest

from app.providers import xtream


class LineServerTests(unittest.TestCase):
    def setUp(self):
        self.p = {"id": "p1", "type": "xtream", "server": "http://main.example:80",
                  "lines": [{"id": "l1", "username": "a", "password": "x", "server": ""},
                            {"id": "l2", "username": "b", "password": "y", "server": "http://other.example:8080/"}]}

    def test_inherits_provider_server(self):
        self.assertEqual(xtream.stream_url(self.p, self.p["lines"][0], "7"), "http://main.example:80/live/a/x/7.ts")

    def test_line_override(self):
        self.assertEqual(xtream.stream_url(self.p, self.p["lines"][1], "7"), "http://other.example:8080/live/b/y/7.ts")

    def test_missing_key_inherits(self):
        line = {"id": "l3", "username": "c", "password": "z"}
        self.assertTrue(xtream.stream_url(self.p, line, "1").startswith("http://main.example:80/"))


if __name__ == "__main__":
    unittest.main()
