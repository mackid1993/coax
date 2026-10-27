import datetime
import json
import os
import shutil
import tempfile
import threading
import unittest
from unittest import mock

from app.providers import catalog, net, scheduler
from app.providers.net import ProviderError
from tests.test_providers_support import FakeProvider


def xtream_provider(server, **extra):
    provider = {"id": "p1", "type": "xtream", "server": server, "refresh_hours": 24,
                "groups": ["News"], "lines": [{"id": "l1", "username": "u", "password": "pw12",
                                               "max_streams": 1}]}
    provider.update(extra)
    return provider


def streams_json(*items):
    return json.dumps([{"stream_id": sid, "name": name, "category_id": cid, "stream_type": "live"}
                       for sid, name, cid in items]).encode()


CATS = json.dumps([{"category_id": "1", "category_name": "News"},
                   {"category_id": "2", "category_name": "Sports"}]).encode()


class CatalogTestCase(unittest.TestCase):
    def setUp(self):
        self.fake = FakeProvider()
        self.tmp = tempfile.mkdtemp()
        self._data_dir = catalog.DATA_DIR
        catalog.DATA_DIR = self.tmp
        self._sleep = net.sleep
        net.sleep = lambda seconds: None
        self.provider = xtream_provider(self.fake.url)
        self.fake.set("get_live_categories", (200, {}, CATS))

    def tearDown(self):
        catalog.DATA_DIR = self._data_dir
        net.sleep = self._sleep
        self.fake.close()
        shutil.rmtree(self.tmp)

    def set_streams(self, *items):
        self.fake.set("get_live_streams", (200, {}, streams_json(*items)))


class CatalogTests(CatalogTestCase):
    def test_refresh_writes_cache_and_reports_changes(self):
        self.set_streams(("1", "A", "1"), ("2", "B", "2"))
        first = catalog.refresh(self.provider)
        self.assertEqual(sorted(first["added"]), ["1", "2"])
        self.assertEqual(first["added_selected"], ["1"])
        self.assertEqual(first["removed"], [])
        path = os.path.join(self.tmp, "cache", "catalog-p1.json")
        self.assertTrue(os.path.exists(path))
        cached = catalog.load("p1")
        self.assertEqual(set(cached), {"fetched_at", "groups", "streams"})
        self.assertEqual(cached["groups"], ["News", "Sports"])
        self.assertIsNotNone(catalog.parse_iso(cached["fetched_at"]))

        self.set_streams(("1", "A renamed", "1"), ("3", "C", "1"))
        second = catalog.refresh(self.provider)
        self.assertEqual(second["added"], ["3"])
        self.assertEqual(second["added_selected"], ["3"])
        self.assertEqual(second["removed"], ["2"])
        self.assertEqual(second["removed_selected"], [])
        self.assertEqual(second["updated"], 1)
        self.assertEqual(catalog.find_stream("p1", "1")["name"], "A renamed")
        self.assertIsNone(catalog.find_stream("p1", "2"))
        # No temp files left behind.
        self.assertEqual(os.listdir(os.path.join(self.tmp, "cache")), ["catalog-p1.json"])

    def test_failure_keeps_old_cache(self):
        self.set_streams(("1", "A", "1"))
        catalog.refresh(self.provider)
        before = catalog.load("p1")
        self.fake.set("get_live_streams", (500, {}, b"down"))
        with self.assertRaises(ProviderError):
            catalog.refresh(self.provider)
        self.assertEqual(catalog.load("p1"), before)

    def test_load_missing_and_corrupt(self):
        self.assertIsNone(catalog.load("nope"))
        os.makedirs(os.path.join(self.tmp, "cache"))
        with open(catalog.cache_path("bad"), "w") as handle:
            handle.write("{truncated")
        with self.assertLogs("coax.providers", "WARNING"):
            self.assertIsNone(catalog.load("bad"))

    def test_cache_path_is_sanitized(self):
        path = catalog.cache_path("../evil")
        self.assertEqual(os.path.dirname(path), os.path.join(self.tmp, "cache"))

    def test_group_counts_and_selected_streams(self):
        self.set_streams(("1", "A", "1"), ("2", "B", "1"), ("3", "C", "2"))
        result = catalog.refresh(self.provider)
        self.assertEqual(catalog.group_counts(result),
                         [{"name": "News", "count": 2}, {"name": "Sports", "count": 1}])
        self.assertEqual([s["stream_id"] for s in catalog.selected_streams(self.provider)], ["1", "2"])

    def test_concurrent_refresh_is_serialized(self):
        self.set_streams(("1", "A", "1"))
        errors = []

        def run():
            try:
                catalog.refresh(self.provider)
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)

        threads = [threading.Thread(target=run) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(catalog.load("p1")["streams"]), 1)

    def test_unknown_type(self):
        with self.assertRaises(ProviderError):
            catalog.refresh(dict(self.provider, type="ftp"))


class SchedulerTests(CatalogTestCase):
    def setUp(self):
        super().setUp()
        self.config = {"providers": [self.provider]}
        self.guide_calls = []
        self._guide = mock.patch.object(scheduler, "refresh_guide", side_effect=self.guide_calls.append)
        self._guide.start()

    def tearDown(self):
        self._guide.stop()
        scheduler.stop()
        super().tearDown()

    def test_initial_refresh_when_cache_missing_then_not_again(self):
        self.set_streams(("1", "A", "1"))
        state = {}
        scheduler.run_once(lambda: self.config, state)
        self.assertIsNotNone(catalog.load("p1"))
        self.assertEqual(len(self.guide_calls), 1)
        self.assertTrue(scheduler.last_results()["p1"]["ok"])
        count = len(self.fake.requests)
        scheduler.run_once(lambda: self.config, state)
        self.assertEqual(len(self.fake.requests), count)  # fresh cache, nothing due
        self.assertEqual(len(self.guide_calls), 1)

    def test_stale_cache_triggers_refresh(self):
        self.set_streams(("1", "A", "1"))
        catalog.refresh(self.provider)
        old = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=25)
        data = catalog.load("p1")
        data = dict(data, fetched_at=old.isoformat())
        catalog.atomic_write_json(catalog.cache_path("p1"), data)
        self.set_streams(("1", "A", "1"), ("2", "B", "1"))
        scheduler.run_once(lambda: self.config, {})
        result = scheduler.last_results()["p1"]
        self.assertEqual((result["added_count"], result["added_sample"]), (1, ["2"]))
        self.assertEqual(result["added"], 1)  # selected new streams, like the manual refresh

    def make_stale(self):
        old = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=25)
        catalog.atomic_write_json(catalog.cache_path("p1"), dict(catalog.load("p1"), fetched_at=old.isoformat()))

    def test_manual_only_provider_refreshes_only_without_cache(self):
        self.set_streams(("1", "A", "1"))
        self.provider["refresh_hours"] = 0
        scheduler.run_once(lambda: self.config, {})
        self.assertIsNotNone(catalog.load("p1"))  # initial refresh still happens
        self.make_stale()
        count = len(self.fake.requests)
        scheduler.run_once(lambda: self.config, {})
        self.assertEqual(len(self.fake.requests), count)
        self.assertTrue(scheduler.is_manual(self.provider))
        self.assertFalse(scheduler.is_manual({"refresh_hours": None}))

    def test_disabled_provider_is_not_refreshed(self):
        self.set_streams(("1", "A", "1"))
        self.provider["enabled"] = False
        scheduler.run_once(lambda: self.config, {})
        self.assertEqual(self.fake.requests, [])
        self.assertIsNone(catalog.load("p1"))
        self.provider["enabled"] = True
        scheduler.run_once(lambda: self.config, {})
        self.assertIsNotNone(catalog.load("p1"))

    def test_status_result_is_counts_and_samples(self):
        self.set_streams(*[(str(i), "S%d" % i, "1") for i in range(1, 101)])
        scheduler.run_once(lambda: self.config, {})
        result = scheduler.last_results()["p1"]
        self.assertEqual(result["added_count"], 100)
        self.assertEqual(result["added_selected_count"], 100)
        self.assertEqual(len(result["added_sample"]), 20)
        self.assertEqual(len(result["added_selected_sample"]), 20)
        self.assertFalse(any(isinstance(v, list) and len(v) > 20 for v in result.values()))
        self.make_stale()
        self.set_streams(("1", "S1", "1"))
        scheduler.run_once(lambda: self.config, {})
        result = scheduler.last_results()["p1"]
        self.assertEqual((result["removed_count"], result["vanished_count"]), (99, 99))
        self.assertEqual(len(result["removed_sample"]), 20)

    def test_failure_is_logged_backed_off_and_never_raises(self):
        self.fake.set("get_live_streams", (500, {}, b"down"))
        state = {}
        with self.assertLogs("coax.providers", "ERROR") as logs:
            scheduler.run_once(lambda: self.config, state)
        self.assertFalse(scheduler.last_results()["p1"]["ok"])
        self.assertNotIn("pw12", "\n".join(logs.output))
        self.assertIn("p1", state["next_retry"])
        count = len(self.fake.requests)
        scheduler.run_once(lambda: self.config, state)
        self.assertEqual(len(self.fake.requests), count)  # still in backoff

    def test_bad_config_callable_never_raises(self):
        def broken():
            raise RuntimeError("boom")
        with self.assertLogs("coax.providers", "ERROR"):
            scheduler.run_once(broken, {})

    def test_on_refresh_hook_errors_are_contained(self):
        self.set_streams(("1", "A", "1"))
        seen = []

        def hook(provider, result):
            seen.append(result["added_selected"])
            raise ValueError("hook bug")

        with self.assertLogs("coax.providers", "ERROR"):
            summary = scheduler.refresh_provider(self.provider, hook)
        self.assertTrue(summary["ok"])
        self.assertEqual(seen, [["1"]])

    def test_start_runs_daemon_thread(self):
        self.set_streams(("1", "A", "1"))
        thread = scheduler.start(lambda: self.config)
        self.assertTrue(thread.daemon)
        self.assertIs(scheduler.start(lambda: self.config), thread)
        for _ in range(100):
            if catalog.load("p1"):
                break
            threading.Event().wait(0.05)
        self.assertIsNotNone(catalog.load("p1"))
        scheduler.stop()
        self.assertFalse(thread.is_alive())


class GuideHookTests(unittest.TestCase):
    def test_guide_hook_missing_module_is_ignored(self):
        with mock.patch.dict("sys.modules", {"app.guide.xmltv": None}):
            self.assertFalse(scheduler.refresh_guide({}))

    def test_guide_hook_errors_are_logged(self):
        fake_module = mock.Mock()
        fake_module.refresh.side_effect = RuntimeError("bad xml")
        import app.guide
        with mock.patch.dict("sys.modules", {"app.guide.xmltv": fake_module}), \
                mock.patch.object(app.guide, "xmltv", fake_module, create=True):
            with self.assertLogs("coax.providers", "ERROR"):
                self.assertFalse(scheduler.refresh_guide({"x": 1}))
        fake_module.refresh.assert_called_once_with({"x": 1})


if __name__ == "__main__":
    unittest.main()
