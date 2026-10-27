import unittest

from app import backups


def catalog(*streams):
    return {"groups": sorted({s[2] for s in streams}),
            "streams": [{"stream_id": sid, "name": name, "group": group, "tvg_id": tvg, "logo": ""}
                        for sid, name, group, tvg in streams]}


CATALOGS = {
    "p1": catalog(
        ("1", "US| CNN HD", "News", "CNN.us"),
        ("2", "US| FOX NEWS CHANNEL FHD", "News", "FoxNews.us"),
        ("3", "US| HBO EAST", "Movies", "HBO.us"),
        ("4", "US| HBO WEST", "Movies", "HBOWest.us"),
        ("5", "US| NBC 4 NEW YORK (WNBC)", "Locals", "WNBC.us"),
        ("6", "US| NBC 4 LOS ANGELES (KNBC)", "Locals", "KNBC.us"),
        ("7", "US| MS NOW", "News", "MSNBC.us"),
        ("8", "US| CNNI", "News", "CNNI.us"),
    ),
    "p2": catalog(
        ("a", "CNN FHD", "News", "US-CNN"),
        ("b", "FOX NEWS RAW", "News", ""),
        ("c", "HBO", "Movies", ""),
        ("d", "WNBC NBC NEW YORK", "Locals", ""),
        ("e", "MSNBC", "News", "msnbc.us"),
        ("f", "Discovery Channel", "Docs", ""),
    ),
}


def channel(name, *refs, **extra):
    ch = {"id": 1, "name": name, "streams": [{"provider": p, "stream_id": s} for p, s in refs]}
    ch.update(extra)
    return ch


def keys(results):
    return [(r["provider"], r["stream_id"]) for r in results]


class HelperTests(unittest.TestCase):
    def test_clean_strips_prefix_and_folds(self):
        self.assertEqual(backups.clean("US| cnn ᴿᴬᵂ"), "CNN RAW")

    def test_describe_ignores_quality_and_reads_region(self):
        self.assertEqual(backups.describe("US| CNN FHD")["key"], backups.describe("CNN HD")["key"])
        self.assertEqual(backups.describe("HBO Pacific")["region"], "WEST")
        self.assertEqual(backups.describe("NBC (WNBC)")["code"], "WNBC")

    def test_tvg_key(self):
        self.assertEqual(backups.tvg_key("CNN.us"), "cnn")
        self.assertEqual(backups.tvg_key("US-CNN"), "cnn")
        self.assertEqual(backups.tvg_key("ab"), "")

    def test_similarity_bounds(self):
        a, b = backups.describe("Discovery Channel"), backups.describe("Discovery")
        self.assertEqual(backups.similarity(a, b), 1.0)
        self.assertEqual(backups.similarity(a, backups.describe("")), 0.0)


class SuggestTests(unittest.TestCase):
    def test_same_name_across_providers_excludes_current(self):
        out = backups.suggest(channel("CNN", ("p1", "1")), CATALOGS)
        self.assertIn(("p2", "a"), keys(out))
        self.assertNotIn(("p1", "1"), keys(out))
        self.assertNotIn(("p1", "8"), keys(out))   # CNNI is a different channel
        top = out[0]
        self.assertEqual(top["reason"], "same name")
        self.assertTrue(top["sure"])
        self.assertEqual(set(top), {"provider", "stream_id", "name", "score", "reason", "sure"})

    def test_quality_and_filler_words_ignored(self):
        out = backups.suggest(channel("Fox News", ("p1", "2")), CATALOGS)
        self.assertEqual(keys(out), [("p2", "b")])

    def test_never_mixes_east_and_west(self):
        out = backups.suggest(channel("HBO", ("p1", "3")), CATALOGS)
        self.assertEqual(keys(out), [("p2", "c")])
        west = backups.suggest(channel("HBO West", ("p1", "4")), CATALOGS)
        self.assertEqual(keys(west), [])

    def test_call_sign_only_matches_same_call_sign(self):
        out = backups.suggest(channel("NBC 4 New York (WNBC)", ("p1", "5")), CATALOGS)
        self.assertEqual(keys(out), [("p2", "d")])
        self.assertEqual(out[0]["reason"], "same call sign WNBC")

    def test_same_guide_id_catches_renamed_channel(self):
        out = backups.suggest(channel("MS NOW", ("p1", "7")), CATALOGS)
        self.assertEqual(keys(out), [("p2", "e")])
        self.assertEqual(out[0]["reason"], "same guide ID")

    def test_similar_name_is_weaker(self):
        out = backups.suggest(channel("Discovery Channel HD"), CATALOGS)
        self.assertEqual(keys(out), [("p2", "f")])
        self.assertEqual(out[0]["reason"], "same name")
        out = backups.suggest(channel("Discovry"), CATALOGS)
        self.assertEqual(keys(out), [("p2", "f")])
        self.assertTrue(out[0]["reason"].startswith("similar name"))
        self.assertFalse(out[0]["sure"])
        self.assertLess(out[0]["score"], backups.SCORE_SAME_TVG)

    def test_scores_sorted_in_range_and_capped(self):
        many = {"p": catalog(*[(str(i), "ESPN %s" % q, "Sports", "")
                               for i, q in enumerate(["HD", "FHD", "SD", "RAW", "4K", "", "HEVC"] * 3)])}
        out = backups.suggest(channel("ESPN"), many)
        self.assertEqual(len(out), backups.MAX_SUGGESTIONS)
        scores = [r["score"] for r in out]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertTrue(all(0.0 <= s <= 1.0 for s in scores))
        self.assertIn("FHD", out[0]["name"])   # best quality first

    def test_empty_inputs(self):
        self.assertEqual(backups.suggest(channel("CNN"), {}), [])
        self.assertEqual(backups.suggest({"name": "", "streams": []}, CATALOGS), [])
        self.assertEqual(backups.suggest(channel("CNN"), {"p": {"groups": [], "streams": None}}), [])


if __name__ == "__main__":
    unittest.main()
