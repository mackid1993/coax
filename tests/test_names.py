import unittest

from app.names import clean_name


class CleanNameTests(unittest.TestCase):
    def test_prefixes(self):
        cases = {
            "US| COMEDY CENTRAL HD": "COMEDY CENTRAL HD",
            "U.S| A&E HD": "A&E HD",
            "TV| COMEDY CENTRAL \u1d3f\u1d2c\u1d42": "COMEDY CENTRAL \u1d3f\u1d2c\u1d42",
            "NFL TEAMS| Chiefs": "Chiefs",
            "NFL | Chiefs": "Chiefs",
            "[US] CNN": "CNN",
            "UK: BBC One": "BBC One",
            "US| NBC 41 KANSAS CITY MO (KSHB)": "NBC 41 KANSAS CITY MO (KSHB)",
            "US| TV \u1d3f\u1d2c\u1d42": "TV \u1d3f\u1d2c\u1d42",
        }
        for raw, want in cases.items():
            self.assertEqual(clean_name(raw), want, raw)

    def test_leaves_normal_names(self):
        for name in ("CNN", "ESPN2 HD", "A&E", "MLB: Yankees vs Mets", "E! Entertainment", "BBC One"):
            self.assertEqual(clean_name(name), name)

    def test_never_empty(self):
        self.assertEqual(clean_name("US|"), "US|")
        self.assertEqual(clean_name(""), "")
        self.assertEqual(clean_name(None), "")


if __name__ == "__main__":
    unittest.main()
