import unittest

from app import hub as hubmod
from tests.fixtures.hub.support import EngineCommand, channel, config, fake_stream_url, provider


class EventTests(unittest.TestCase):
    def test_timeline(self):
        cfg = config([provider("p1", 1)], [channel(1, [("p1", "a")])])
        h = hubmod.Hub(lambda: cfg, stream_url=fake_stream_url, engine_command=EngineCommand())
        s = h.open("uuid-1", "ip", "ua")
        for line in ("[hlsloop stream] provider is serving an offline placeholder; switching to stream 2 of 3",
                     "[hlsloop stream] playlist stale 20s, re-resolving",
                     "[hlsloop stream] restarting remuxer: reserve down to 29s; playing 3 percent slower",
                     "[hlsloop stream] switching to stream 1 of 3"):
            s._watch(line)
        st = h.status()[0]
        kinds = [e["kind"] for e in st["events"]]
        self.assertEqual(kinds, ["failover", "stall", "speed", "failover"])
        self.assertEqual(st["stream_no"], 1)
        self.assertIn("backup stream", st["events"][0]["text"])
        s.close()


if __name__ == "__main__":
    unittest.main()
