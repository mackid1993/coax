"""Smoke-test follow-ups: password redaction in engine logs, /api/network."""

import unittest

from app import hub


class RedactTests(unittest.TestCase):
    def test_xtream_path_and_query(self):
        line = ("playlist error (... url: /live/u/secret/123.m3u8 ...) "
                "http://h/player_api.php?username=u&password=hunter2&action=x")
        out = hub._redact(line)
        self.assertNotIn("secret", out)
        self.assertNotIn("hunter2", out)
        self.assertIn("/live/u/********/123.m3u8", out)
        self.assertIn("username=u", out)

    def test_plain_line_unchanged(self):
        self.assertEqual(hub._redact("reserve: refilled to 37s"), "reserve: refilled to 37s")


if __name__ == "__main__":
    unittest.main()
