import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app", "engine"))
import fmtprobe  # noqa: E402


class SummarizeTest(unittest.TestCase):
    def test_he_aac_1080p60(self):
        st = [{"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080, "avg_frame_rate": "60000/1001"},
              {"codec_type": "audio", "codec_name": "aac", "profile": "HE-AAC", "sample_rate": "48000", "channels": 2,
               "bit_rate": "96083"}]
        f = fmtprobe.summarize(st, 5250000, 10.0)
        self.assertEqual(f["video"], "H.264 · 1920x1080 · 59.94 fps · 4.1 Mbps")
        self.assertEqual(f["audio"], "HE-AAC · 48 kHz stereo · 96 kbps")


if __name__ == "__main__":
    unittest.main()
