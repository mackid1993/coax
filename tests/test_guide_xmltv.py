"""Tests for app.guide.xmltv and app.guide.output (fixture files, no network)."""

import gzip
import json
import os
import shutil
import tempfile
import unittest
import xml.etree.ElementTree as ET
from unittest import mock

from app import guide
from app.guide import output, xmltv

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "guide")
ORIGINAL_PROVIDER_XMLTV_URL = xmltv.provider_xmltv_url


def make_config():
    return {
        "version": 1,
        "server": {"port": 7780, "base_url": "http://192.168.1.50:7780/"},
        "providers": [
            {"id": "p1", "name": "One", "type": "xtream", "xmltv_url": "http://p1.test/xmltv",
             "user_agent": "UA1"},
            {"id": "p2", "name": "Two", "type": "m3u", "xmltv_url": "http://p2.test/xmltv.gz"},
        ],
        "channels": [
            {"id": 1, "uuid": "uuid-1", "number": "101", "name": 'CNN "Live"', "group": "News",
             "logo": "http://logo/cnn.png", "enabled": True,
             "streams": [{"provider": "p1", "stream_id": "1"}],
             "guide": {"mode": "xmltv", "station_id": "10142", "xmltv_id": "cnn.us",
                       "xmltv_provider": "p1"}},
            {"id": 2, "uuid": "uuid-2", "number": "102", "name": "ESPN", "group": "Sports",
             "logo": "", "enabled": True,
             "streams": [{"provider": "p1", "stream_id": "2"}],
             "guide": {"mode": "both", "station_id": "10179", "xmltv_id": "espn.us",
                       "xmltv_provider": ""}},
            {"id": 3, "uuid": "uuid-3", "number": "103", "name": "Disabled", "enabled": False,
             "streams": [{"provider": "p1", "stream_id": "3"}],
             "guide": {"mode": "xmltv", "xmltv_id": "unused.us", "xmltv_provider": "p1"}},
            {"id": 4, "uuid": "uuid-4", "number": "104", "name": "BBC One", "group": "UK",
             "enabled": True, "streams": [{"provider": "p2", "stream_id": "x"}],
             "guide": {"mode": "gracenote", "station_id": "50000", "xmltv_id": "bbc1.uk",
                       "xmltv_provider": "p2"}},
            {"id": 5, "uuid": "uuid-5", "number": "105", "name": "BBC One Copy", "group": "UK",
             "enabled": True, "streams": [{"provider": "p2", "stream_id": "x"}],
             "guide": {"mode": "xmltv", "station_id": "", "xmltv_id": "bbc1.uk",
                       "xmltv_provider": "p2"}},
        ],
        "lineups": [
            {"id": "main", "name": "Main", "channels": [2, 1, 3, 4]},
            {"id": "uk", "name": "UK", "channels": [5]},
        ],
    }


class FakeDownloads:
    """Serves fixture files for provider URLs; a URL mapped to an exception raises it."""

    def __init__(self, mapping):
        self.mapping = mapping
        self.calls = []

    def __call__(self, url, dest_path, user_agent=""):
        self.calls.append((url, user_agent))
        source = self.mapping[url]
        if isinstance(source, Exception):
            raise source
        if isinstance(source, bytes):
            with open(dest_path, "wb") as f:
                f.write(source)
            return
        shutil.copyfile(source, dest_path)


def gzipped(path):
    with open(path, "rb") as f:
        return gzip.compress(f.read())


class XmltvTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(guide, "CACHE_DIR", self.tmp.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        # Isolate from app.providers: use the configured xmltv_url.
        patcher = mock.patch.object(xmltv, "provider_xmltv_url", lambda p: p.get("xmltv_url", ""))
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_refresh(self, mapping, config=None):
        fake = FakeDownloads(mapping)
        with mock.patch.object(xmltv, "download", fake):
            summary = xmltv.refresh(config or make_config())
        return summary, fake

    def default_mapping(self):
        return {"http://p1.test/xmltv": os.path.join(FIXTURES, "provider1.xml"),
                "http://p2.test/xmltv.gz": gzipped(os.path.join(FIXTURES, "provider2.xml"))}

    def leftovers(self):
        return [n for n in os.listdir(self.tmp.name) if n.startswith(".tmp")]


class RefreshTests(XmltvTestBase):
    def test_filters_and_renames(self):
        summary, fake = self.run_refresh(self.default_mapping())
        self.assertTrue(summary["lineups"]["main"]["ok"], summary)
        self.assertIn(("http://p1.test/xmltv", "TiviMate/5.1.6 (Android 12)"), fake.calls)
        tree = ET.parse(os.path.join(self.tmp.name, "xmltv-main.xml"))
        root = tree.getroot()
        self.assertEqual(root.tag, "tv")
        channel_ids = sorted(c.get("id") for c in root.findall("channel"))
        # Channel 4 is gracenote-only, channel 3 is disabled: neither is included.
        self.assertEqual(channel_ids, ["coax-1", "coax-2"])
        programmes = {p.get("channel"): p for p in root.findall("programme")}
        self.assertEqual(sorted(programmes), ["coax-1", "coax-2"])
        self.assertEqual(programmes["coax-1"].findtext("desc"), "News <live>")
        names = [n.text for n in root.find("channel[@id='coax-2']").findall("display-name")]
        self.assertEqual(names, ["ESPN & More"])
        self.assertEqual(self.leftovers(), [])

    def test_gzip_source_detected_by_magic_and_shared_xmltv_id(self):
        self.run_refresh(self.default_mapping())
        root = ET.parse(os.path.join(self.tmp.name, "xmltv-uk.xml")).getroot()
        self.assertEqual([c.get("id") for c in root.findall("channel")], ["coax-5"])
        self.assertEqual([p.get("channel") for p in root.findall("programme")], ["coax-5"])

    def test_gzip_with_plain_url_and_plain_with_gz_url(self):
        mapping = {"http://p1.test/xmltv": gzipped(os.path.join(FIXTURES, "provider1.xml")),
                   "http://p2.test/xmltv.gz": os.path.join(FIXTURES, "provider2.xml")}
        summary, _ = self.run_refresh(mapping)
        self.assertTrue(summary["lineups"]["main"]["ok"])
        self.assertTrue(summary["lineups"]["uk"]["ok"])

    def test_ids_cache_for_picker(self):
        summary, _ = self.run_refresh(self.default_mapping())
        with open(os.path.join(self.tmp.name, "xmltv-ids-p1.json"), encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual([c["id"] for c in data["channels"]], ["cnn.us", "espn.us", "unused.us"])
        cnn = data["channels"][0]
        self.assertEqual(cnn["names"], ["CNN", "Cable News Network"])
        self.assertEqual(cnn["icon"], "http://example.invalid/cnn.png")
        self.assertEqual(xmltv.load_ids("p2")["channels"][0]["names"], ["BBC One"])
        self.assertEqual(summary["providers"]["p1"]["channels"], 3)

    def test_failure_keeps_old_files(self):
        self.run_refresh(self.default_mapping())
        main = os.path.join(self.tmp.name, "xmltv-main.xml")
        ids = os.path.join(self.tmp.name, "xmltv-ids-p1.json")
        with open(main, "rb") as f:
            old_main = f.read()
        with open(ids, "rb") as f:
            old_ids = f.read()

        mapping = self.default_mapping()
        mapping["http://p1.test/xmltv"] = OSError("connection refused")
        summary, _ = self.run_refresh(mapping)
        self.assertFalse(summary["providers"]["p1"]["ok"])
        self.assertFalse(summary["lineups"]["main"]["ok"])
        self.assertTrue(summary["lineups"]["uk"]["ok"])   # independent lineup still refreshed
        with open(main, "rb") as f:
            self.assertEqual(f.read(), old_main)
        with open(ids, "rb") as f:
            self.assertEqual(f.read(), old_ids)
        self.assertEqual(self.leftovers(), [])

    def test_broken_xml_keeps_old_file(self):
        self.run_refresh(self.default_mapping())
        main = os.path.join(self.tmp.name, "xmltv-main.xml")
        with open(main, "rb") as f:
            old_main = f.read()
        mapping = self.default_mapping()
        mapping["http://p1.test/xmltv"] = b"<tv><channel id='cnn.us'><display-name>x</tv>"
        summary, _ = self.run_refresh(mapping)
        self.assertFalse(summary["lineups"]["main"]["ok"])
        with open(main, "rb") as f:
            self.assertEqual(f.read(), old_main)
        self.assertEqual(self.leftovers(), [])

    def test_html_error_page_is_rejected(self):
        mapping = self.default_mapping()
        mapping["http://p1.test/xmltv"] = b"<html><body>Login</body></html>"
        summary, _ = self.run_refresh(mapping)
        self.assertFalse(summary["providers"]["p1"]["ok"])
        self.assertIn("not an XMLTV", summary["providers"]["p1"]["error"])
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "xmltv-main.xml")))

    def test_missing_ids_are_reported(self):
        config = make_config()
        config["channels"][0]["guide"]["xmltv_id"] = "nope.us"
        summary, _ = self.run_refresh(self.default_mapping(), config)
        self.assertEqual(summary["providers"]["p1"]["missing"], ["nope.us"])
        root = ET.parse(os.path.join(self.tmp.name, "xmltv-main.xml")).getroot()
        self.assertEqual([c.get("id") for c in root.findall("channel")], ["coax-2"])

    def test_provider_without_url_needed_fails_lineup(self):
        config = make_config()
        config["providers"][1]["xmltv_url"] = ""
        summary, _ = self.run_refresh(self.default_mapping(), config)
        self.assertFalse(summary["lineups"]["uk"]["ok"])
        self.assertTrue(summary["lineups"]["main"]["ok"])

    def test_lineup_without_xmltv_channels_gets_empty_guide(self):
        config = make_config()
        config["lineups"].append({"id": "empty", "name": "Empty", "channels": [4]})
        self.run_refresh(self.default_mapping(), config)
        root = ET.parse(os.path.join(self.tmp.name, "xmltv-empty.xml")).getroot()
        self.assertEqual(len(root), 0)

    def test_provider_url_seam_uses_providers_module_when_present(self):
        import app
        fake = mock.Mock()
        fake.xmltv_url.return_value = "http://from-providers"
        with mock.patch.dict("sys.modules", {"app.providers": fake}), \
                mock.patch.object(app, "providers", fake, create=True):
            url = ORIGINAL_PROVIDER_XMLTV_URL({"id": "p1", "xmltv_url": "x"})
        self.assertEqual(url, "http://from-providers")

    def test_provider_url_seam_falls_back_without_providers_function(self):
        import app
        empty = mock.Mock(spec=[])
        with mock.patch.dict("sys.modules", {"app.providers": empty}), \
                mock.patch.object(app, "providers", empty, create=True):
            url = ORIGINAL_PROVIDER_XMLTV_URL({"id": "p1", "xmltv_url": " http://x "})
        self.assertEqual(url, "http://x")

    def test_streaming_parse_clears_elements(self):
        """Programmes for unwanted channels are not kept in the tree."""
        big = ['<?xml version="1.0"?><tv>', '<channel id="a"><display-name>A</display-name>'
               '</channel>']
        for i in range(2000):
            big.append(f'<programme start="1" channel="z{i % 5}"><title>T{i}</title></programme>')
        big.append("</tv>")
        path = os.path.join(self.tmp.name, "big.xml")
        with open(path, "w", encoding="utf-8") as f:
            f.write("".join(big))
        with xmltv.open_source(path) as stream:
            available, found = xmltv.parse_source(stream, {"a": [9]}, {})
        self.assertEqual([c["id"] for c in available], ["a"])
        self.assertEqual(found, {"a"})


class OutputTests(unittest.TestCase):
    def test_m3u_entries(self):
        text = output.m3u(make_config(), "main")
        lines = text.splitlines()
        self.assertEqual(lines[0], "#EXTM3U")
        # Lineup order, disabled channel 3 skipped.
        self.assertEqual(len(lines), 1 + 3 * 2)
        self.assertEqual(
            lines[1],
            '#EXTINF:-1 channel-id="coax-2" tvg-id="coax-2" tvg-name="ESPN" tvg-logo="" '
            'group-title="Sports" channel-number="102" tvc-guide-stationid="10179",ESPN')
        self.assertEqual(lines[2], "http://192.168.1.50:7780/stream/uuid-2.ts")
        # Mode xmltv: no station id; quotes in attributes escaped.
        self.assertEqual(
            lines[3],
            '#EXTINF:-1 channel-id="coax-1" tvg-id="coax-1" tvg-name="CNN \'Live\'" '
            'tvg-logo="http://logo/cnn.png" group-title="News" channel-number="101",CNN \'Live\'')
        self.assertEqual(lines[4], "http://192.168.1.50:7780/stream/uuid-1.ts")
        self.assertIn('tvc-guide-stationid="50000",BBC One', lines[5])
        self.assertTrue(text.endswith("\n"))

    def test_m3u_no_station_id_when_empty_or_none_mode(self):
        config = make_config()
        config["channels"][1]["guide"]["mode"] = "none"
        config["channels"][3]["guide"]["station_id"] = ""
        text = output.m3u(config, "main")
        self.assertNotIn("tvc-guide-stationid", text)

    def test_m3u_newlines_in_names_cannot_break_entries(self):
        config = make_config()
        config["channels"][1]["name"] = "ESPN\r\n#EXTINF:-1,evil"
        lines = output.m3u(config, "main").splitlines()
        self.assertEqual(len(lines), 7)
        self.assertTrue(lines[1].endswith(",ESPN #EXTINF:-1,evil"))

    def test_m3u_unknown_lineup(self):
        with self.assertRaises(KeyError):
            output.m3u(make_config(), "nope")

    def test_stream_url_contains_uuid_for_engine_matching(self):
        text = output.m3u(make_config(), "uk")
        self.assertIn("/stream/uuid-5.ts", text)

    def test_xmltv_path(self):
        with mock.patch.object(guide, "CACHE_DIR", "/data/cache"):
            self.assertEqual(output.xmltv_path("main"), "/data/cache/xmltv-main.xml")
        for bad in ("../etc", "a/b", "", ".hidden"):
            with self.assertRaises(ValueError):
                output.xmltv_path(bad)


if __name__ == "__main__":
    unittest.main()
