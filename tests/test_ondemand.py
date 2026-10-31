"""The On demand page: Add all and hand-picks in the plan, the catalog search, the draft, Save and Add all."""

import os
import shutil
import tempfile
import unittest

from app import ondemand
from app import vod as vodlib
from app.providers import vod as vodcat

VOD = {
    "movies": [
        {"id": "1", "name": "EN - Up (2009)", "ext": "mkv", "groups": ["Action"], "added": 300},
        {"id": "2", "name": "4K: Up (2009)", "ext": "mkv", "groups": ["4K"], "added": 200},
        {"id": "3", "name": "Heat (1995)", "ext": "mp4", "groups": ["Action"], "added": 100},
        {"id": "4", "name": "Amélie (2001)", "ext": "mp4", "groups": ["Comedy"], "added": 400},
        {"id": "5", "name": "The Thing (1982)", "ext": "mp4", "groups": ["Hidden"], "added": 50},
    ],
    "series": [{"id": "s1", "name": "Breaking Bad (2008)", "groups": ["Drama"], "modified": 10},
               {"id": "s2", "name": "Lost (2004)", "groups": ["Drama"], "modified": 20}],
}
EPISODES = {"s1": [{"id": "e1", "season": 1, "episode": 1, "ext": "mkv"}],
            "s2": [{"id": "e9", "season": 1, "episode": 1, "ext": "mkv"}]}


def provider(**vod):
    base = {"movies": ["Action", "4K", "Comedy"], "series": ["Drama"]}
    base.update(vod)
    return {"id": "p2", "name": "TPS", "type": "xtream", "lines": [], "vod": base}


class FakeCatalogs:
    """ondemand.Catalogs over a fixed list (no cache files)."""

    def __init__(self, vod):
        self.vod = vod

    def get(self, pid):
        titles = []
        for m in self.vod["movies"]:
            name, year = vodcat.title_year(m["name"], m.get("year"))
            titles.append(ondemand.Title("m", m, name, year, m.get("added") or 0))
        for s in self.vod["series"]:
            name, year = vodcat.title_year(s["name"], s.get("year") or "")
            titles.append(ondemand.Title("s", s, name, year, s.get("modified") or 0))
        titles.sort(key=lambda t: -ondemand._num(t.added))
        return titles, {t.key: t for t in titles}


class ChoiceTests(unittest.TestCase):
    def test_older_config_counts_every_available_group_as_add_all(self):
        p = provider()
        self.assertEqual(vodlib.add_all(p), (["Action", "4K", "Comedy"], ["Drama"]))
        self.assertEqual(vodlib.picks(p), (set(), set()))
        vod = vodlib.materialize(dict(p["vod"], library={"movies": []}))
        self.assertEqual(vod["all"], {"movies": ["Action", "4K", "Comedy"], "series": ["Drama"]})
        self.assertNotIn("library", vod)

    def test_add_all_never_reaches_beyond_the_available_groups(self):
        p = provider(all={"movies": ["Action", "Gone"], "series": []})
        self.assertEqual(vodlib.add_all(p), (["Action"], []))

    def test_quality_from_the_providers_labels(self):
        self.assertEqual(vodlib.quality("4K: Up (2009)"), "4K")
        self.assertEqual(vodlib.quality("Up", ["Movies | Dolby Vision"]), "DV")
        self.assertEqual(vodlib.quality("Up [UHD]"), "4K")
        self.assertEqual(vodlib.quality("Up (2009)", ["Action"]), "HD")
        self.assertEqual(vodlib.quality("Dvorak"), "HD")   # a word that starts with DV is no label


class PlanTests(unittest.TestCase):
    def files(self, p):
        return vodlib.plan(p, VOD, EPISODES, "http://coax")

    def test_add_all_works_as_before_and_one_copy_stands_for_a_title(self):
        pl = self.files(provider())
        movies = sorted(r for r in pl.files if r.startswith("Movies/"))
        self.assertEqual(movies, ["Movies/.coax/p2/Amélie (2001)/Amélie (2001).strm",
                                  "Movies/.coax/p2/Heat (1995)/Heat (1995).strm",
                                  "Movies/.coax/p2/Up (2009)/Up (2009).strm"])
        self.assertEqual(pl.picked, set())
        self.assertEqual(pl.present["m:2"], pl.present["m:1"])   # the 4K copy is filed as the one Up

    def test_a_hand_pick_only_brings_that_title_into_its_groups_collection(self):
        p = provider(all={"movies": [], "series": []}, picks={"movies": ["3"], "series": ["s2"]})
        pl = self.files(p)
        self.assertEqual(sorted(pl.files), ["Movies/.coax/p2/Heat (1995)/Heat (1995).strm",
                                            "TV/.coax/p2/Lost (2004)/Season 01/Lost (2004) - S01E01.strm"])
        self.assertEqual(pl.members[("movies", "Action")], ["Movies/.coax/p2/Heat (1995)/Heat (1995).strm"])
        self.assertEqual(pl.members[("series", "Drama")], ["coax-p2-s2"])
        self.assertEqual(pl.picked, set(pl.files) | {"coax-p2-s2"})
        self.assertEqual(pl.picked_keys, {"m:3", "s:s2"})

    def test_a_pick_from_a_group_that_is_not_available_is_ignored(self):
        p = provider(all={"movies": [], "series": []}, picks={"movies": ["5"], "series": []})
        self.assertEqual(self.files(p).files, {})

    def test_a_pick_inside_an_add_all_group_is_that_groups(self):
        p = provider(all={"movies": ["Action"], "series": []}, picks={"movies": ["3"], "series": []})
        pl = self.files(p)
        self.assertEqual(pl.picked, set())
        self.assertEqual(len([r for r in pl.files if "Heat" in r]), 1)

    def test_picking_another_copy_of_a_title_adds_it_beside_the_first(self):
        p = provider(all={"movies": ["Action"], "series": []}, picks={"movies": ["2"], "series": []})
        pl = self.files(p)
        ups = sorted(r for r in pl.files if "Up (2009)" in r)
        self.assertEqual(ups, ["Movies/.coax/p2/Up (2009) - 4K/Up (2009) - 4K.strm",
                               "Movies/.coax/p2/Up (2009)/Up (2009).strm"])
        self.assertEqual(pl.files[ups[0]], "http://coax/vod/p2/m/2.mkv\n")
        self.assertEqual(pl.meta[ups[0]]["title"], "Up")   # Channels DVR still knows it as Up
        self.assertEqual(pl.members[("movies", "4K")], [ups[0]])
        self.assertIn(ups[0], pl.picked)


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.titles, self.by_key = FakeCatalogs(VOD).get("p2")

    def find(self, text, kind="", group=""):
        words = ondemand.query_words(text)
        return [t.key for t in self.titles if ondemand.matches(t, kind, group, words)]

    def test_case_accents_articles_and_year(self):
        self.assertEqual(self.find("AMELIE"), ["m:4"])
        self.assertEqual(self.find("heat 1995"), ["m:3"])
        self.assertEqual(self.find("the thing"), ["m:5"])
        self.assertEqual(self.find("thing"), ["m:5"])
        self.assertEqual(self.find("heat 1996"), [])
        self.assertEqual(self.find("", kind="s"), ["s:s2", "s:s1"])   # newest first
        self.assertEqual(self.find("up", group="4K"), ["m:2"])

    def test_rows_tag_their_state(self):
        p = provider(all={"movies": ["Action"], "series": []}, picks={"movies": ["2", "4"], "series": []})
        choices = ondemand.Choices(p)
        counts = ondemand.wanted_counts(self.titles, choices)
        present = {"m:1", "m:3", "m:4"}
        row = {k: ondemand.row("p2", self.by_key[k], choices, present, counts, {})
               for k in ("m:1", "m:2", "m:3", "m:4", "s:s1")}
        self.assertEqual((row["m:1"]["via"], row["m:1"]["in_channels"], row["m:1"]["duplicate"]), ("all", True, True))
        self.assertEqual((row["m:2"]["via"], row["m:2"]["pending"], row["m:2"]["quality"]), ("pick", True, "4K"))
        self.assertTrue(row["m:2"]["duplicate"])
        self.assertEqual((row["m:3"]["duplicate"], row["m:4"]["via"], row["m:4"]["in_channels"]), (False, "pick", True))
        self.assertEqual((row["s:s1"]["via"], row["s:s1"]["in_channels"]), ("", False))

    def test_an_unsaved_add_of_another_copy_is_tagged_duplicate(self):
        p = provider(all={"movies": ["Action"], "series": []})
        choices = ondemand.Choices(p)
        extra = [self.by_key["m:2"]]
        counts = ondemand.wanted_counts(self.titles, choices, extra)
        r = ondemand.row("p2", self.by_key["m:2"], choices, set(), counts, {"m:2": "add"})
        self.assertEqual((r["draft"], r["duplicate"]), ("add", True))


class DraftRuleTests(unittest.TestCase):
    def setUp(self):
        self.cats = FakeCatalogs(VOD)

    def test_tidy_drops_what_can_no_longer_be_saved_and_says_so(self):
        cfg = {"providers": [provider(all={"movies": ["Action"], "series": []}, picks={"movies": ["4"], "series": []})]}
        pending = {"p2": {"m:2": "add", "m:3": "add", "m:404": "add", "m:5": "add", "m:4": "remove", "m:2x": "add"},
                   "gone": {"m:1": "add"}}
        kept, notes = ondemand.tidy(pending, cfg, self.cats)
        self.assertEqual(kept, {"p2": {"m:2": "add", "m:4": "remove"}})
        self.assertEqual(len(notes), 2)
        self.assertIn("no longer offers", notes[0])
        self.assertTrue(notes[0].startswith("4 unsaved changes were dropped"))

    def test_apply_writes_hand_picks(self):
        cfg = {"providers": [provider(all={"movies": [], "series": []}, picks={"movies": ["4"], "series": []})]}
        result = ondemand.apply(cfg, {"p2": {"m:3": "add", "m:4": "remove", "s:s1": "add"}})
        self.assertEqual(result, {"p2": (2, 1)})
        self.assertEqual(cfg["providers"][0]["vod"]["picks"], {"movies": ["3"], "series": ["s1"]})

    def test_add_all_takes_in_the_groups_picks_and_off_takes_the_group_out(self):
        cfg = {"providers": [provider(all={"movies": [], "series": []}, picks={"movies": ["3", "4"], "series": []})]}
        ondemand.set_add_all(cfg, "p2", "m", "Action", True, self.cats)
        vod = cfg["providers"][0]["vod"]
        self.assertEqual((vod["all"]["movies"], vod["picks"]["movies"]), (["Action"], ["4"]))
        ondemand.set_add_all(cfg, "p2", "m", "Action", False, self.cats)
        self.assertEqual((vod["all"]["movies"], vod["picks"]["movies"]), ([], ["4"]))
        pl = vodlib.plan(cfg["providers"][0], VOD, EPISODES, "http://coax")
        self.assertEqual([r for r in pl.files if "Heat" in r], [])   # Heat's pick went with Add all
        with self.assertRaises(ValueError):
            ondemand.set_add_all(cfg, "p2", "m", "Hidden", True, self.cats)

    def test_old_config_switching_one_group_off_keeps_the_others(self):
        cfg = {"providers": [provider()]}
        ondemand.set_add_all(cfg, "p2", "m", "Comedy", False, self.cats)
        self.assertEqual(cfg["providers"][0]["vod"]["all"], {"movies": ["Action", "4K"], "series": ["Drama"]})

    def test_validation_and_estimate(self):
        self.assertTrue(ondemand.valid_pending({"p2": {"m:1": "add", "s:x_2": "remove"}}))
        for bad in ([], {"p2": {"x:1": "add"}}, {"p2": {"m:1": "keep"}}, {"p/2": {}}, {"p2": []}):
            self.assertFalse(ondemand.valid_pending(bad), bad)
        self.assertEqual(ondemand.estimate(96214), "about 11 hours")
        self.assertEqual(ondemand.estimate(40), "about a minute")
        self.assertEqual(ondemand.estimate(3000), "about 20 minutes")


class CatalogCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.orig = vodcat.cache_path
        vodcat.cache_path = lambda pid, suffix="": os.path.join(self.tmp, "vod-%s%s.json" % (pid, suffix))

    def tearDown(self):
        vodcat.cache_path = self.orig
        shutil.rmtree(self.tmp)

    def test_read_once_and_again_when_the_list_changes(self):
        import json
        cats = ondemand.Catalogs()
        self.assertEqual(cats.get("p2")[0], [])
        with open(vodcat.cache_path("p2"), "w") as handle:
            json.dump(VOD, handle)
        titles, by_key = cats.get("p2")
        self.assertEqual(len(titles), 7)
        self.assertIs(cats.get("p2")[0], titles)   # in memory, not read again
        with open(vodcat.cache_path("p2"), "w") as handle:
            json.dump({"movies": VOD["movies"][:1], "series": []}, handle)
        os.utime(vodcat.cache_path("p2"), (1, 1))
        self.assertEqual([t.key for t in cats.get("p2")[0]], ["m:1"])
        self.assertEqual(by_key["m:4"].name, "Amélie")


class LibraryPickTests(unittest.TestCase):
    """The library with a fake Channels DVR: picks first, a long import yields to new picks, deleted picks stay out."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.imports = os.path.join(self.tmp, "Imports")
        for sub in ("Movies", "TV", "Videos"):
            os.makedirs(os.path.join(self.imports, sub))
        self.orig = vodcat.load, vodcat.episodes_for
        vodcat.load = lambda pid: dict(VOD, fetched_at=vodcat.catalog.now_iso())
        vodcat.episodes_for = lambda provider, series, **kw: {s["id"]: EPISODES.get(s["id"], []) for s in series}
        self.files, self.deleted, self.calls = {}, set(), []
        self.cfg = {"server": {"base_url": "http://coax:7780"}, "channels_dvr": {"url": "http://dvr:8089"},
                    "providers": [provider(all={"movies": ["Action", "Comedy"], "series": []},
                                           picks={"movies": ["2"], "series": ["s2"]})]}
        test = self

        class Store:
            def get(self):
                return test.cfg

            def update(self, mutate):
                mutate(test.cfg)

        self.lib = vodlib.Library(Store(), self.tmp, imports_dir=self.imports, dvr_call=self.dvr)

    def tearDown(self):
        vodcat.load, vodcat.episodes_for = self.orig
        shutil.rmtree(self.tmp)

    def dvr(self, cfg, method, path, body=None):
        self.calls.append((method, path))
        if method == "POST" and path == "/dvr/files":
            fid = str(100 + len(self.files))
            self.files[fid] = body
            if self.on_post:
                self.on_post(body)
            return dict(body, ID=fid)
        if method == "GET" and path.startswith("/dvr/files/"):
            fid = path.rsplit("/", 1)[1]
            return dict(self.files.get(fid) or {}, Deleted=fid in self.deleted)
        if method == "GET" and path.endswith("/files") and path.startswith("/dvr/groups/"):
            gid = path.split("/")[3]
            return [dict(b, ID=f, Deleted=f in self.deleted) for f, b in self.files.items() if b.get("GroupID") == gid]
        if path in ("/dvr/collections/content", "/dvr/custom_sections"):
            return []
        if method == "POST" and path.endswith("/new"):
            return {"ID": "9"}
        if "/matches/tmdb" in path:
            return []
        return {}

    on_post = None

    def paths(self):
        return [b["Path"] for b in self.files.values()]

    def test_picks_go_first_and_the_page_learns_what_is_in_channels(self):
        self.lib.sync("p2")
        self.assertEqual(self.lib.state["p2"].get("error"), "")
        order = self.paths()
        self.assertEqual(order[0], ".coax/p2/Up (2009) - 4K/Up (2009) - 4K.strm")   # the hand-pick before Add all
        self.assertIn(".coax/p2/Lost (2004)/Season 01/Lost (2004) - S01E01.strm", order)
        self.assertNotIn("Breaking Bad", " ".join(order))
        self.assertEqual(self.lib.present("p2"), {"m:1", "m:2", "m:3", "m:4", "s:s2"})
        status = self.lib.status("p2")
        self.assertNotIn("present", status)

    def test_a_long_add_all_import_yields_to_new_picks(self):
        self.cfg["providers"][0]["vod"]["picks"] = {"movies": [], "series": []}
        seen = []

        def on_post(body):
            seen.append(body["Path"])
            if len(seen) == 1:   # a pick is saved while the Add all import runs
                self.cfg["providers"][0]["vod"]["picks"] = {"movies": ["2"], "series": []}
                self.lib.request("p2", urgent=True)
        self.on_post = on_post
        vodlib_create = vodlib.CREATE_WORKERS
        vodlib.CREATE_WORKERS = 1
        try:
            with self.assertRaises(vodlib._Preempted):
                self.lib._sync("p2")
            self.assertEqual(len(seen), 1)            # the import stopped after the title in flight
            self.lib._sync("p2")                      # the run that follows at once
        finally:
            vodlib.CREATE_WORKERS = vodlib_create
        self.assertEqual(seen[1], ".coax/p2/Up (2009) - 4K/Up (2009) - 4K.strm")   # the pick went next
        self.assertEqual(len(seen), 4)

    def test_a_pick_deleted_in_channels_is_dropped_not_added_back(self):
        self.lib.sync("p2")
        mine = next(f for f, b in self.files.items() if "Up (2009) - 4K" in b["Path"])
        lost = [f for f, b in self.files.items() if b.get("GroupID") == "coax-p2-s2"]
        self.deleted.update([mine] + lost)
        posted = len(self.files)
        self.lib.sync("p2")
        self.assertEqual(self.cfg["providers"][0]["vod"]["picks"], {"movies": [], "series": []})
        self.assertEqual(len(self.files), posted)   # nothing added back
        self.assertFalse(os.path.exists(os.path.join(self.imports, "Movies/.coax/p2/Up (2009) - 4K")))
        self.assertEqual(self.lib.present("p2"), {"m:1", "m:3", "m:4"})
        # picking it again later adds it afresh
        self.cfg["providers"][0]["vod"]["picks"] = {"movies": ["2"], "series": []}
        self.lib.sync("p2")
        self.assertEqual(len(self.files), posted + 1)

    def test_an_add_all_title_deleted_in_channels_keeps_the_old_behavior(self):
        self.lib.sync("p2")
        heat = next(f for f, b in self.files.items() if "Heat" in b["Path"])
        self.deleted.add(heat)
        posted = len(self.files)
        self.lib.sync("p2")
        self.assertEqual(len(self.files), posted)
        self.assertEqual(vodlib.add_all(self.cfg["providers"][0])[0], ["Action", "Comedy"])

    def test_nothing_chosen_clears_the_library_but_keeps_the_catalog(self):
        self.lib.sync("p2")
        self.cfg["providers"][0]["vod"].update(all={"movies": [], "series": []}, picks={"movies": [], "series": []})
        self.lib.sync("p2")
        self.assertFalse(os.path.exists(os.path.join(self.imports, "Movies", ".coax", "p2")))
        self.assertEqual(self.lib.present("p2"), set())


class FakeLibrary:
    def __init__(self):
        self.requests = []
        self.in_dvr = set()

    def present(self, pid):
        return set(self.in_dvr)

    def status(self, pid):
        return {"running": None}

    def request(self, pid=None, urgent=False):
        self.requests.append((pid, urgent))


from tests.test_server_api import ApiTestCase  # noqa: E402


class OnDemandApiTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.add_provider()
        self.app.ondemand_titles = FakeCatalogs(VOD)
        self.app.vod = self.lib = FakeLibrary()

        def mutate(cfg):
            cfg["providers"][0]["vod"] = {"movies": ["Action", "4K", "Comedy"], "series": ["Drama"]}   # 1.0.8
        self.store.update(mutate)

    def vod(self):
        return self.store.get()["providers"][0]["vod"]

    def test_frame_counts_and_an_upgrade_keeps_every_group_whole(self):
        status, body, _ = self.request("GET", "/api/ondemand")
        self.assertEqual(status, 200, body)
        p = body["providers"][0]
        self.assertEqual([(g["name"], g["count"], g["add_all"]) for g in p["groups"]["movies"]],
                         [("Action", 2, True), ("4K", 1, True), ("Comedy", 1, True)])
        self.assertEqual(body["per_minute"], 150)

    def test_catalog_and_content_pages(self):
        self.store.update(lambda c: c["providers"][0]["vod"].update(all={"movies": ["Action"], "series": []},
                                                                      picks={"movies": ["4"], "series": []}))
        self.lib.in_dvr = {"m:1", "m:3"}
        status, body, _ = self.request("GET", "/api/ondemand/titles?view=catalog&limit=2")
        self.assertEqual((status, body["total"], len(body["items"])), (200, 6, 2))   # Hidden's title is not offered
        status, body, _ = self.request("GET", "/api/ondemand/titles?view=content")
        self.assertEqual([(i["key"], i["via"], i["in_channels"], i["pending"]) for i in body["items"]],
                         [("m:4", "pick", False, True), ("m:1", "all", True, False), ("m:3", "all", True, False)])
        status, body, _ = self.request("GET", "/api/ondemand/titles?view=content&filter=pending")
        self.assertEqual([i["key"] for i in body["items"]], ["m:4"])
        status, body, _ = self.request("GET", "/api/ondemand/titles?view=catalog&q=heat+1995")
        self.assertEqual([i["key"] for i in body["items"]], ["m:3"])
        status, body, _ = self.request("GET", "/api/ondemand/titles?view=content&keys=1")
        self.assertEqual(body["keys"], [{"provider": "p1", "key": "m:4"}])   # Add all titles are never selectable
        for bad in ("view=x", "kind=x", "filter=x", "limit=0"):
            self.assertEqual(self.request("GET", "/api/ondemand/titles?" + bad)[0], 400, bad)

    def test_draft_is_kept_until_save_and_a_stale_page_cannot_overwrite_it(self):
        self.store.update(lambda c: c["providers"][0]["vod"].update(all={"movies": [], "series": []}))
        status, d, _ = self.request("GET", "/api/ondemand/draft")
        self.assertEqual((status, d["pending"], d["notes"]), (200, {}, []))
        status, put, _ = self.request("PUT", "/api/ondemand/draft",
                                      {"rev": d["rev"], "pending": {"p1": {"m:3": "add", "s:s2": "add"}}})
        self.assertEqual(status, 200, put)
        self.assertEqual(self.request("PUT", "/api/ondemand/draft", {"rev": d["rev"], "pending": {}})[0], 409)
        self.assertEqual(self.vod()["picks"], {"movies": [], "series": []})   # nothing applied before Save
        self.assertEqual(self.lib.requests, [])
        status, body, _ = self.request("GET", "/api/ondemand/titles?view=content")
        self.assertEqual([(i["key"], i["draft"]) for i in body["items"]], [("m:3", "add"), ("s:s2", "add")])   # newest first
        # a reload (or another device) gets the same draft back
        again = ondemand.Draft(self.store.data_dir)
        self.assertEqual(again.data["pending"], {"p1": {"m:3": "add", "s:s2": "add"}})
        self.assertEqual(self.request("POST", "/api/ondemand/save", {"rev": d["rev"]})[0], 409)
        status, saved, _ = self.request("POST", "/api/ondemand/save", {"rev": put["rev"]})
        self.assertEqual((status, saved["added"], saved["removed"]), (200, 2, 0), saved)
        self.assertEqual(self.vod()["picks"], {"movies": ["3"], "series": ["s2"]})
        self.assertEqual(self.lib.requests, [("p1", True)])   # hand-picks go ahead of any Add all import
        self.assertEqual(self.request("GET", "/api/ondemand/draft")[1]["pending"], {})

    def test_a_draft_title_the_provider_dropped_is_dropped_with_a_note(self):
        self.store.update(lambda c: c["providers"][0]["vod"].update(all={"movies": [], "series": []}))
        rev = self.request("GET", "/api/ondemand/draft")[1]["rev"]
        self.request("PUT", "/api/ondemand/draft", {"rev": rev, "pending": {"p1": {"m:3": "add", "m:77": "add"}}})
        status, d, _ = self.request("GET", "/api/ondemand/draft")
        self.assertEqual(d["pending"], {"p1": {"m:3": "add"}})
        self.assertEqual(d["notes"], ["1 unsaved change was dropped: the provider no longer offers that title."])

    def test_add_all_switch_and_unsaved_changes_inside_it(self):
        self.store.update(lambda c: c["providers"][0]["vod"].update(all={"movies": [], "series": []},
                                                                      picks={"movies": ["3"], "series": []}))
        rev = self.request("GET", "/api/ondemand/draft")[1]["rev"]
        self.request("PUT", "/api/ondemand/draft", {"rev": rev, "pending": {"p1": {"m:1": "add", "m:4": "add"}}})
        status, body, _ = self.request("POST", "/api/ondemand/add-all",
                                       {"provider": "p1", "kind": "movies", "group": "Action", "on": True})
        self.assertEqual((status, body["dropped"]), (200, 1), body)
        self.assertEqual((self.vod()["all"]["movies"], self.vod()["picks"]["movies"]), (["Action"], []))
        self.assertEqual(self.request("GET", "/api/ondemand/draft")[1]["pending"], {"p1": {"m:4": "add"}})
        self.assertEqual(self.lib.requests[-1], ("p1", False))
        status, body, _ = self.request("POST", "/api/ondemand/add-all",
                                       {"provider": "p1", "kind": "movies", "group": "Nope", "on": True})
        self.assertEqual(status, 400)
        self.assertEqual(self.request("POST", "/api/ondemand/add-all", {"provider": "p1"})[0], 400)

    def test_making_a_group_unavailable_takes_its_add_all_and_picks_away(self):
        self.store.update(lambda c: c["providers"][0]["vod"].update(all={"movies": ["Action"], "series": ["Drama"]},
                                                                      picks={"movies": ["4"], "series": []}))
        from unittest import mock
        with mock.patch.object(vodcat, "summary", lambda pid: {}):
            status, body, _ = self.request("PUT", "/api/providers/p1/vod", {"movies": ["Action"], "series": []})
            self.assertEqual(status, 200, body)
            self.request("PUT", "/api/providers/p1/vod", {"movies": ["Action", "Comedy"], "series": []})
        vod = self.vod()
        self.assertEqual((vod["movies"], vod["all"], vod["picks"]),
                         (["Action", "Comedy"], {"movies": ["Action"], "series": []}, {"movies": [], "series": []}))
        # a group made available adds nothing by itself
        self.assertEqual(self.vod()["all"]["movies"], ["Action"])

    def test_status_page_lists_video_on_demand_plays(self):
        class Plays:
            def status(self):
                import time
                return [{"provider_id": "p1", "provider_name": "Provider A", "line_id": "l1", "line_name": "Line 1",
                         "client_ip": "10.0.0.5", "name": "Heat (1995)", "kind": "movie",
                         "started": time.time() - 65, "sent": 5000000, "streaming": True}]
        self.app.vod_plays = Plays()
        status, body, _ = self.request("GET", "/api/status")
        self.assertEqual(status, 200)
        play = body["vod_plays"][0]
        self.assertEqual((play["name"], play["line_name"]), ("Heat (1995)", "Line 1"))
        self.assertGreaterEqual(play["seconds"], 64)
        self.assertNotIn("started", play)


if __name__ == "__main__":
    unittest.main()
