"""Tests for the final review fixes: quotes in names, corrupt config fallback,
standalone XMLTV sources, enabled lines, base URL from Host, HEAD on unknown channels."""

import json
import os
import tempfile
import unittest

from app import config as configmod
from app import hub as hubmod
from app.guide import output, xmltv
from tests.fixtures.hub.support import EngineCommand, channel, config, fake_stream_url, provider


class FinalFixTests(unittest.TestCase):
    def test_quote_in_name_matches_engine_name_rule(self):
        cfg = {"server": {"base_url": "http://h:7780"},
               "channels": [{"id": 1, "uuid": "u1", "number": "1", "name": 'CNN "Live"', "enabled": True,
                             "group": "", "logo": "", "guide": {"mode": "none"}}],
               "lineups": [{"id": "main", "name": "Main", "guide": "per-channel", "channels": [1]}]}
        text = output.m3u(cfg, "main")
        extinf = [l for l in text.splitlines() if l.startswith("#EXTINF")][0]
        title = extinf.split(",", 1)[1]
        self.assertNotIn('"', title)
        # The engine's nm() strips "..." spans before reading the title; it must equal tvg-name.
        import re
        engine_title = re.sub('"[^"]*"', "", extinf).split(",", 1)[1].strip()
        self.assertEqual(engine_title, title)

    def test_corrupt_config_uses_backup(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "config.json")
        store = configmod.ConfigStore(p)
        store.load()
        store.update(lambda c: c["server"].update(base_url="http://x:7780"))
        store.update(lambda c: c["server"].update(base_url="http://y:7780"))
        open(p, "w").write("{not json")
        cfg = configmod.ConfigStore(p).load()
        self.assertEqual(cfg["server"]["base_url"], "http://x:7780")

    def test_manual_only_refresh_hours_is_valid(self):
        d = tempfile.mkdtemp()
        store = configmod.ConfigStore(os.path.join(d, "config.json"))
        store.load()

        def add(c):
            c["providers"].append({"id": "p1", "name": "P", "type": "xtream", "server": "http://s",
                                   "refresh_hours": 0, "lines": [{"id": "l1", "name": "L", "username": "u", "password": "p", "max_streams": 1}]})
        store.update(add)
        self.assertEqual(store.get()["providers"][0]["refresh_hours"], 0)

    def test_disabled_line_and_provider_are_skipped(self):
        p1 = provider("p1", 1, 1)
        p1["lines"][0]["enabled"] = False
        p2 = provider("p2", 1)
        p2["enabled"] = False
        cfg = config([p1, p2], [channel(1, [("p2", "a"), ("p1", "b")])])
        h = hubmod.Hub(lambda: cfg, stream_url=fake_stream_url, engine_command=EngineCommand())
        s = h.open("uuid-1", "ip", "ua")
        self.assertEqual((s.provider_id, s.line_id), ("p1", "l2"))
        s.close()

    def test_line_whose_url_fails_falls_to_next_line(self):
        cfg = config([provider("p1", 1, 1)], [channel(1, [("p1", "a")])])

        def url(p, l, sid):
            if l["id"] == "l1":
                raise ValueError("pattern does not match")
            return fake_stream_url(p, l, sid)
        h = hubmod.Hub(lambda: cfg, stream_url=url, engine_command=EngineCommand())
        s = h.open("uuid-1", "ip", "ua")
        self.assertEqual(s.line_id, "l2")
        s.close()

    def test_standalone_xmltv_source_is_fetched(self):
        seen = []
        orig_url, orig_fetch = xmltv.provider_xmltv_url, xmltv._fetch_and_parse
        try:
            xmltv.provider_xmltv_url = lambda p: p.get("xmltv_url") or ""
            xmltv._fetch_and_parse = lambda url, prov, wanted, route, folder: (seen.append(url) or ([], set(wanted)))
            cfg = {"providers": [], "xmltv_sources": [{"id": "x1", "name": "Extra", "url": "http://g/x.xml"}],
                   "channels": [{"id": 1, "uuid": "u", "name": "A", "enabled": True, "streams": [],
                                 "guide": {"mode": "xmltv", "xmltv_id": "a.us", "xmltv_provider": "x1"}}],
                   "lineups": [{"id": "nfl", "name": "NFL", "guide": "xmltv", "channels": [1]}]}
            d = tempfile.mkdtemp()
            import app.guide as guide
            old = guide.CACHE_DIR
            guide.CACHE_DIR = d
            try:
                summary = xmltv.refresh(cfg)
            finally:
                guide.CACHE_DIR = old
            self.assertIn("http://g/x.xml", seen)
            self.assertTrue(summary["providers"]["x1"]["ok"])
        finally:
            xmltv.provider_xmltv_url, xmltv._fetch_and_parse = orig_url, orig_fetch


if __name__ == "__main__":
    unittest.main()
