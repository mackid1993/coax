import unittest

from app import hub as hubmod
from tests.fixtures.hub.support import EngineCommand, channel, config, fake_stream_url, provider


class IndicatorTests(unittest.TestCase):
    def test_engine_lines_drive_state(self):
        cfg = config([provider("p1", 1)], [channel(1, [("p1", "a")])])
        cfg["transcode"] = "1080p60"
        h = hubmod.Hub(lambda: cfg, stream_url=fake_stream_url, engine_command=EngineCommand())
        s = h.open("uuid-1", "ip", "ua")
        s._watch("[hlsloop stream] transcoding with Intel/AMD graphics (VAAPI)")
        s._watch("[hlsloop stream] status: main stream, reserve 55s, client lead 9.0s, viewers 1")
        st = h.status()[0]
        self.assertTrue(st["transcode"]["on"])
        self.assertEqual(st["transcode"]["profile"], "1080p60")
        self.assertEqual((st["speed"]["slow"], st["speed"]["reserve"]), (False, 55))
        s._watch("[hlsloop stream] restarting remuxer: reserve down to 29s; playing 3 percent slower, pitch unchanged")
        self.assertEqual((h.status()[0]["speed"]["slow"], h.status()[0]["speed"]["reserve"]), (True, 29))
        s._watch("[hlsloop stream] reserve: empty, video now arrives only as fast as the provider publishes it")
        self.assertTrue(h.status()[0]["speed"]["empty"])
        s._watch("[hlsloop stream] restarting remuxer: reserve back to 50s; back to real speed")
        st = h.status()[0]["speed"]
        self.assertEqual((st["slow"], st["empty"], st["reserve"]), (False, False, 50))
        s.close()


if __name__ == "__main__":
    unittest.main()
