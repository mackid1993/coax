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
        self.assertIn("/live/********/********/123.m3u8", out)
        self.assertIn("username=********", out)

    def test_guide_error_line(self):
        from app import main
        import logging
        rec = logging.LogRecord("coax.guide.xmltv", logging.WARNING, __file__, 1, "XMLTV refresh failed for provider %s: %s",
                                ("p1", "Max retries exceeded with url: /xmltv.php?username=alice9&password=hunter2"), None)
        main._Redact().filter(rec)
        self.assertNotIn("alice9", rec.getMessage())
        self.assertNotIn("hunter2", rec.getMessage())

    def test_signed_session_tokens(self):
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJsaW5lIjo0MiwiaXAiOiIxLjIuMy40In0.c2lnbmF0dXJlX2J5dGVz"
        for line, kept in (("GET https://cdn.example/play/hls/%s/index.m3u8 403" % jwt, "/index.m3u8 403"),
                           ("GET https://cdn.example/play/hls/%s.m3u8 403" % jwt, ".m3u8 403"),
                           ("https://cdn.example/live/%s.ts: 403" % jwt, ".ts: 403"),
                           ("url /play/%s, retry" % jwt, ", retry"),
                           ("https://cdn.example/seg.ts?jwt=%s&x=1" % jwt, "&x=1"),
                           ("https://cdn.example/seg.ts?a=%s&b=1" % jwt, "&b=1"),
                           ("https://cdn.example/play#%s" % jwt, ""),
                           ("url /play/%s" % jwt, "")):
            out = hub._redact(line)
            self.assertNotIn("eyJ", out, line)
            self.assertTrue(out.endswith(kept), out)
        self.assertEqual(hub._redact("/hls/2f9c0a1b3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8.ts"),
                         "/hls/2f9c0a1b3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8.ts")

    def test_plain_line_unchanged(self):
        self.assertEqual(hub._redact("reserve: refilled to 37s"), "reserve: refilled to 37s")


if __name__ == "__main__":
    unittest.main()
