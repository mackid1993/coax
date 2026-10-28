import os as _os

_os.environ.setdefault("COAX_SKIP_URL_CHECK", "1")

import json
import os
import tempfile
import unittest

from app import config as configmod
from app import snapshots
from app.guide import output


class StartNumberTests(unittest.TestCase):
    def test_lineup_numbers_from_start_in_order(self):
        cfg = {"server": {"base_url": "http://h:7780"},
               "channels": [{"id": 1, "uuid": "a", "name": "Zed", "number": "5", "enabled": True, "guide": {}},
                            {"id": 2, "uuid": "b", "name": "Alpha", "number": "9", "enabled": True, "guide": {}}],
               "lineups": [{"id": "m", "name": "M", "channels": [1, 2], "sort": "alpha", "start_number": 1000}]}
        text = output.m3u(cfg, "m")
        nums = [l.split('channel-number="')[1].split('"')[0] for l in text.splitlines() if l.startswith("#EXTINF")]
        names = [l.rsplit(",", 1)[1] for l in text.splitlines() if l.startswith("#EXTINF")]
        self.assertEqual(list(zip(names, nums)), [("Alpha", "1000"), ("Zed", "1001")])
        cfg["lineups"][0]["start_number"] = None
        text = output.m3u(cfg, "m")
        self.assertIn('channel-number="9"', text)


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.store = configmod.ConfigStore(os.path.join(self.dir, "config.json"))
        self.store.load()

    def test_create_list_restore_prune(self):
        self.store.update(lambda c: c["server"].update(base_url="http://one:7780"))
        first = snapshots.create(self.store, "manual")
        self.store.update(lambda c: c["server"].update(base_url="http://two:7780"))
        result = snapshots.restore(self.store, first["name"])
        self.assertEqual(self.store.get()["server"]["base_url"], "http://one:7780")
        kinds = [b["kind"] for b in snapshots.list_backups(self.store)]
        self.assertIn("pre-restore", kinds)
        pre = os.path.join(snapshots.folder(self.store), result["pre_restore"])
        self.assertEqual(snapshots._config_of(pre)["server"]["base_url"], "http://two:7780")
        for _ in range(5):
            snapshots.create(self.store, "auto")
        snapshots.prune(self.store, 2)
        self.assertEqual(sum(1 for b in snapshots.list_backups(self.store) if b["kind"] == "auto"), 2)
        self.assertEqual(oct(os.stat(pre).st_mode & 0o777), "0o600")

    def test_bad_names_rejected(self):
        with self.assertRaises(ValueError):
            snapshots.read(self.store, "../config.json")

    def test_region_and_backup_validation(self):
        def bad_region(c):
            c["gracenote"]["regions"] = [{"country": "US", "postal": ""}]
        with self.assertRaises(configmod.ConfigError):
            self.store.update(bad_region)

        def ok(c):
            c["gracenote"]["regions"] = [{"country": "USA", "postal": "10954"}]
            c["backups"]["time"] = "04:15"
        self.store.update(ok)
        self.assertEqual(self.store.get()["backups"]["time"], "04:15")


if __name__ == "__main__":
    unittest.main()


class FullBackupTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.store = configmod.ConfigStore(os.path.join(self.dir, "config.json"))
        self.store.load()

    def test_backup_includes_data_files_and_restores_them(self):
        os.makedirs(os.path.join(self.dir, "cache", "history"))
        open(os.path.join(self.dir, "cache", "feedhealth.json"), "w").write('{"feeds": {"a": 1}}')
        open(os.path.join(self.dir, "notifications.json"), "w").write('[{"id": "1"}]')
        open(os.path.join(self.dir, "cache", "history", "5.jsonl"), "w").write('{"t": 1}\n')
        b = snapshots.create(self.store, "manual")
        self.assertTrue(b["name"].endswith(".tar.gz"))
        self.assertTrue(b["full"])
        open(os.path.join(self.dir, "cache", "feedhealth.json"), "w").write('{"feeds": {}}')
        os.remove(os.path.join(self.dir, "cache", "history", "5.jsonl"))
        called = []
        r = snapshots.restore(self.store, b["name"], on_data_restored=lambda: called.append(1))
        self.assertIn("history/5.jsonl", r["files"])
        self.assertEqual(json.load(open(os.path.join(self.dir, "cache", "feedhealth.json"))), {"feeds": {"a": 1}})
        self.assertTrue(os.path.exists(os.path.join(self.dir, "cache", "history", "5.jsonl")))
        self.assertEqual(called, [1])

    def test_old_json_backup_still_restores(self):
        self.store.update(lambda c: c["server"].update(base_url="http://old:7780"))
        os.makedirs(snapshots.folder(self.store), exist_ok=True)
        name = "config-20260101-000000-manual.json"
        open(os.path.join(snapshots.folder(self.store), name), "w").write(json.dumps(self.store.snapshot()))
        self.store.update(lambda c: c["server"].update(base_url="http://new:7780"))
        self.assertIn(name, [x["name"] for x in snapshots.list_backups(self.store)])
        snapshots.restore(self.store, name)
        self.assertEqual(self.store.get()["server"]["base_url"], "http://old:7780")
