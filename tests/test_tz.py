import unittest

from app import tz


class TzTests(unittest.TestCase):
    def test_effective_order(self):
        old = tz._ENV_TZ
        try:
            tz._ENV_TZ = ""
            self.assertEqual(tz.effective({}), ("UTC", "default"))
            tz._ENV_TZ = "UTC"
            self.assertEqual(tz.effective({}), ("UTC", "environment"))
            self.assertEqual(tz.effective({"timezone": "Not/AZone"})[1], "environment")
        finally:
            tz._ENV_TZ = old

    def test_valid_rejects_paths(self):
        self.assertFalse(tz.valid("../etc/passwd"))
        self.assertTrue(tz.valid("UTC"))


if __name__ == "__main__":
    unittest.main()
