import json
import os
import shutil
import stat
import tempfile
import threading
import unittest

from app import config as configmod


def provider(pid="p1", password="secret"):
    return {"id": pid, "name": "Prov " + pid, "type": "xtream", "server": "http://host:80",
            "lines": [{"id": "l1", "name": "Line 1", "username": "u", "password": password, "max_streams": 1}]}


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "config.json")

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_creates_defaults_with_mode_0600(self):
        store = configmod.ConfigStore(self.path)
        cfg = store.load()
        self.assertEqual(cfg["server"]["port"], 7780)
        self.assertEqual(cfg["version"], configmod.CURRENT_VERSION)
        self.assertEqual(cfg["lineups"][0]["id"], "main")
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

    def test_fills_missing_keys_and_migrates_unversioned(self):
        with open(self.path, "w") as handle:
            json.dump({"server": {"port": 9000}, "providers": [provider()],
                       "channels": [{"id": 5, "name": "A", "streams": [{"provider": "p1", "stream_id": "1"}]}]},
                      handle)
        cfg = configmod.ConfigStore(self.path).load()
        self.assertEqual(cfg["version"], 1)
        self.assertEqual(cfg["server"]["port"], 9000)
        self.assertEqual(cfg["server"]["channel_shutdown_delay"], 10)
        self.assertEqual(cfg["providers"][0]["format"], "hls")
        self.assertEqual(cfg["providers"][0]["lines"][0]["search"], "")
        self.assertTrue(cfg["channels"][0]["uuid"])
        self.assertEqual(cfg["channels"][0]["guide"]["mode"], "none")
        self.assertEqual(cfg["next_channel_id"], 6)
        self.assertEqual(cfg["xmltv_sources"], [])
        self.assertEqual(cfg["lineups"][0]["guide"], "per-channel")

    def test_migrates_lineup_guide_and_sources(self):
        with open(self.path, "w") as handle:
            json.dump({"version": 1, "lineups": [{"id": "cable", "name": "Cable", "channels": []}],
                       "xmltv_sources": [{"id": "x1", "name": "EPG", "url": "http://e/x.xml"}]}, handle)
        cfg = configmod.ConfigStore(self.path).load()
        self.assertEqual(cfg["lineups"][0]["guide"], "per-channel")
        self.assertEqual(cfg["xmltv_sources"][0]["refresh_hours"], 24)

    def test_a_channel_guide_is_one_source(self):
        def ch(cid, **guide):
            return {"id": cid, "name": "C%d" % cid, "streams": [{"provider": "p1", "stream_id": str(cid)}],
                    "guide": dict({"station_id": "", "xmltv_id": "", "xmltv_provider": ""}, **guide)}
        with open(self.path, "w") as handle:
            json.dump({"providers": [provider()], "channels": [
                ch(1, mode="both", station_id="10142", xmltv_id="cnn.us", xmltv_provider="p1"),
                ch(2, mode="xmltv", station_id="10179", call_sign="ESPN", xmltv_id="espn.us"),
                ch(3, mode="gracenote", xmltv_id="hint.us", xmltv_provider="p1"),
                ch(4, mode="none", xmltv_id="tvg.us", xmltv_provider="p1"),
                ch(5, mode="both"),
                ch(7, mode="both", xmltv_id="seven.us", xmltv_provider="p1")]}, handle)
        store = configmod.ConfigStore(self.path)
        g = [c["guide"] for c in store.load()["channels"]]
        self.assertEqual((g[0]["mode"], g[0]["station_id"], g[0]["xmltv_id"], g[0]["xmltv_provider"]),
                         ("gracenote", "10142", "", ""))      # the station wins over a stored "both"
        self.assertEqual((g[1]["mode"], g[1]["station_id"], g[1]["xmltv_id"], "call_sign" in g[1]),
                         ("xmltv", "", "espn.us", False))
        self.assertEqual((g[2]["mode"], g[2]["xmltv_id"]), ("gracenote", "hint.us"))   # Auto-match hint kept
        self.assertEqual((g[3]["mode"], g[3]["xmltv_id"]), ("gracenote", "tvg.us"))   # Gracenote by default
        self.assertEqual(g[4]["mode"], "none")
        self.assertEqual((g[5]["mode"], g[5]["xmltv_id"]), ("xmltv", "seven.us"))   # "both" without a station

        def xmltv_lineup(cfg):   # a "both" channel whose only lineup takes XMLTV keeps its XMLTV id
            cfg["channels"].append(dict(cfg["channels"][4], id=6, uuid="", guide={
                "mode": "both", "station_id": "1", "xmltv_id": "nfl.us", "xmltv_provider": "p1"}))
            cfg["lineups"].append({"id": "nfl", "name": "NFL", "guide": "xmltv", "channels": [6]})
            cfg["lineups"][0]["channels"] = [c for c in cfg["lineups"][0]["channels"] if c != 6]
        store.update(xmltv_lineup)
        self.assertEqual({k: store.get()["channels"][6]["guide"][k] for k in ("mode", "station_id", "xmltv_id")},
                         {"mode": "xmltv", "station_id": "", "xmltv_id": "nfl.us"})

        def pick_station(cfg):
            cfg["channels"][3]["guide"].update(mode="gracenote", station_id="10001")
        store.update(pick_station)
        self.assertEqual(store.get()["channels"][3]["guide"]["xmltv_id"], "")
        with self.assertRaises(configmod.ConfigError):
            configmod.validate(dict(store.snapshot(), channels=[dict(store.get()["channels"][0],
                                                                     guide={"mode": "both"})]))

    def test_invalid_json_and_invalid_values(self):
        with open(self.path, "w") as handle:
            handle.write("{nope")
        with self.assertRaises(configmod.ConfigError):
            configmod.ConfigStore(self.path).load()
        with open(self.path, "w") as handle:
            json.dump({"transcode": "4k"}, handle)
        with self.assertRaisesRegex(configmod.ConfigError, "transcode"):
            configmod.ConfigStore(self.path).load()

    def test_masking_and_preserving_passwords(self):
        store = configmod.ConfigStore(self.path)
        store.load()
        store.update(lambda cfg: cfg["providers"].append(provider()))
        masked = store.masked()
        self.assertEqual(masked["providers"][0]["lines"][0]["password"], configmod.MASK)
        self.assertEqual(store.get()["providers"][0]["lines"][0]["password"], "secret")
        masked["transcode"] = "720p30"
        store.replace(masked)
        self.assertEqual(store.get()["providers"][0]["lines"][0]["password"], "secret")
        with open(self.path) as handle:
            self.assertEqual(json.load(handle)["providers"][0]["lines"][0]["password"], "secret")
        bad = store.masked()
        bad["providers"][0]["lines"][0]["id"] = "l9"
        with self.assertRaises(configmod.ConfigError):
            store.replace(bad)

    def test_backup_and_atomic_save(self):
        store = configmod.ConfigStore(self.path)
        store.load()
        store.update(lambda cfg: cfg.__setitem__("transcode", "1080p60"))
        store.update(lambda cfg: cfg.__setitem__("transcode", "720p60"))
        with open(self.path + ".bak") as handle:
            self.assertEqual(json.load(handle)["transcode"], "1080p60")
        self.assertEqual(stat.S_IMODE(os.stat(self.path + ".bak").st_mode), 0o600)
        self.assertEqual([n for n in os.listdir(self.dir) if n.startswith(".tmp")], [])

    def test_failed_validation_leaves_state_untouched(self):
        store = configmod.ConfigStore(self.path)
        store.load()
        with self.assertRaises(configmod.ConfigError):
            store.update(lambda cfg: cfg["server"].__setitem__("port", 0))
        self.assertEqual(store.get()["server"]["port"], 7780)
        with open(self.path) as handle:
            self.assertEqual(json.load(handle)["server"]["port"], 7780)

    def test_channel_ids_never_reused(self):
        store = configmod.ConfigStore(self.path)
        store.load()

        def add(cfg):
            channel = configmod.new_channel(cfg, {"name": "X"})
            cfg["channels"].append(channel)
            return channel
        first = store.update(add)
        store.update(lambda cfg: cfg.__setitem__("channels", []))
        second = store.update(add)
        self.assertEqual(first["id"], 1)
        self.assertEqual(second["id"], 2)
        self.assertNotEqual(first["uuid"], second["uuid"])
        # replace() cannot move next_channel_id backwards
        cfg = store.masked()
        cfg["next_channel_id"] = 1
        cfg["channels"] = []
        store.replace(cfg)
        self.assertEqual(store.get()["next_channel_id"], 3)

    def test_concurrent_updates_are_serialized(self):
        store = configmod.ConfigStore(self.path)
        store.load()

        def worker():
            for _ in range(10):
                store.update(lambda cfg: cfg["channels"].append(configmod.new_channel(cfg, {"name": "C"})))
        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        ids = [c["id"] for c in store.get()["channels"]]
        self.assertEqual(sorted(ids), list(range(1, 41)))

    def test_validation_messages(self):
        cfg = configmod.fill_defaults(configmod.default_config())
        cfg["providers"].append(dict(configmod.default_provider(), **provider()))
        configmod.fill_defaults(cfg)
        configmod.validate(cfg)
        cfg["providers"][0]["lines"][0]["search"] = "("
        with self.assertRaisesRegex(configmod.ConfigError, "regular expression"):
            configmod.validate(cfg)

    def test_next_id(self):
        self.assertEqual(configmod.next_id("p", {"p1", "p3", "x"}), "p4")
        self.assertEqual(configmod.next_id("l", set()), "l1")


if __name__ == "__main__":
    unittest.main()
