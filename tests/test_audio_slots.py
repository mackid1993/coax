"""The transcoder's audio inputs (app/engine/aslots.py, aframes.py): one input per exact format, the provider's
track cut into frames and sent to the input of its format, silence everywhere else, never a timestamp that goes
back. End-to-end runs through ffmpeg are offline tests (see CLAUDE.md); these pin the byte-level rules."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app", "engine"))
import aframes  # noqa: E402
import aslots  # noqa: E402


def ts(pid, payload, start=True, cc=0):
    head = bytes([0x47, (0x40 if start else 0) | pid >> 8, pid & 0xFF, 0x10 | (cc & 15)])
    return (head + payload + b"\xff" * 184)[:188]


def pes(sid, pts, data):
    p = pts % (1 << 33)
    n = 8 + len(data)
    return bytes([0, 0, 1, sid, n >> 8, n & 0xFF, 0x80, 0x80, 5, 0x21 | (p >> 29) & 0x0E, (p >> 22) & 0xFF,
                  0x01 | (p >> 14) & 0xFE, (p >> 7) & 0xFF, 0x01 | (p << 1) & 0xFE]) + data


def packets(pid, payload, cc=[0]):  # noqa: B006 - one counter per test module is enough
    out = b""
    for i in range(0, len(payload), 184):
        chunk = payload[i:i + 184]
        if len(chunk) < 184:
            st = 184 - len(chunk)
            out += bytes([0x47, (0x40 if i == 0 else 0) | pid >> 8, pid & 0xFF, 0x30 | cc[0], st - 1]) + \
                (b"\x00" + b"\xff" * (st - 2) if st > 1 else b"") + chunk
        else:
            out += bytes([0x47, (0x40 if i == 0 else 0) | pid >> 8, pid & 0xFF, 0x10 | cc[0]]) + chunk
        cc[0] = (cc[0] + 1) & 15
    return out


def program(streams, pmt=0x100):
    """PAT (program 1 on PID 0x100) and a PMT: [(stream_type, pid, descriptors)] after a video stream on 0x101."""
    pat = b"\x00" + b"\x00\xb0\x0d\x00\x01\xc1\x00\x00" + bytes([0, 1, 0xE0 | pmt >> 8, pmt & 0xFF])
    pat += aslots._crc(pat[1:]).to_bytes(4, "big")
    es = bytes([0x1B, 0xE1, 0x01, 0xF0, 0]) + b"".join(
        bytes([st, 0xE0 | pid >> 8, pid & 0xFF, 0xF0, len(d)]) + d for st, pid, d in streams)
    n = 9 + len(es) + 4
    sec = bytes([0x02, 0xB0 | n >> 8, n & 0xFF]) + b"\x00\x01\xc1\x00\x00\xe1\x01\xf0\x00" + es
    sec += aslots._crc(sec).to_bytes(4, "big")
    return ts(0, pat) + ts(pmt, b"\x00" + sec)


def lang(code, audio_type=0):
    return bytes([0x0A, 4]) + code.encode() + bytes([audio_type])


def slot_pts(out):
    """[(input index, PTS)] of every PES the feeder sent to an input."""
    res = []
    for i in range(0, len(out) - 187, 188):
        p = out[i:i + 188]
        pid = (p[1] & 31) << 8 | p[2]
        if pid in aslots.PIDS and p[1] & 0x40:
            o = 5 + p[4] if (p[3] >> 4 & 3) == 3 else 4
            q = p[o + 9:o + 14]
            res.append((aslots.PIDS.index(pid),
                        ((q[0] >> 1) & 7) << 30 | q[1] << 22 | (q[2] >> 1) << 15 | q[3] << 7 | q[4] >> 1))
    return res


AC3_51 = aslots.SLOTS[1]["frame"]
AC3_20 = aslots.SLOTS[0]["frame"]


class FrameTests(unittest.TestCase):
    def test_every_input_has_a_frame_of_its_own_format(self):
        keys = [s["key"] for s in aslots.SLOTS]
        self.assertEqual(len(set(keys)), len(keys))
        for s in aslots.SLOTS[:aslots.PCM]:
            f = aframes.frame(s["fam"], s["frame"], 0)
            self.assertEqual((f[0], f[1]), (len(s["frame"]), s["key"]), s["name"])
            self.assertGreater(s["dur"], 0, s["name"])

    def test_stereo_and_surround_dolby_go_to_different_inputs(self):
        self.assertEqual(aslots.BYKEY[aframes.frame("ac3", AC3_20, 0)[1]], 0)
        self.assertEqual(aslots.BYKEY[aframes.frame("ac3", AC3_51, 0)[1]], 1)
        self.assertEqual((aslots.chans(aslots.SLOTS[0]["key"]), aslots.chans(aslots.SLOTS[1]["key"])), (2, 6))


class LanguageTests(unittest.TestCase):
    def test_codes(self):
        self.assertEqual([aslots.canon(c) for c in ("es", "SPA", "fre", "ger", "uz", "fil", "und", "qab", "", "xx")],
                         ["spa", "spa", "fra", "deu", "uzb", "tgl", "", "", "", ""])

    def test_a_descriptor_cut_short_is_ignored(self):
        self.assertEqual(aslots.describe(bytes([0x0A, 4]) + b"eng"), ("", True))
        for tag in (0x7F, 0x6A, 0x7A, 0x81):
            self.assertEqual(aslots.describe(bytes([tag, 5, 6])), ("", True))
        f = aslots.Feeder("")
        f.psi()
        f.feed(program([(0x81, 0x102, bytes([0x0A, 4]) + b"eng")]))   # a damaged PMT never stops the feeder
        self.assertEqual(f.fam, {0x102: "ac3"})

    def test_extra_tracks(self):
        self.assertEqual(aslots.describe(lang("eng")), ("eng", True))
        self.assertEqual(aslots.describe(lang("eng", 3)), ("eng", False))          # audio description
        self.assertEqual(aslots.describe(bytes([0x81, 3, 0, 0, 0x40])), ("", False))   # ATSC bsmod 2


class ChoiceTests(unittest.TestCase):
    def feeder(self, pref, streams):
        logs = []
        f = aslots.Feeder(pref, logs.append)
        f.feed(program(streams))
        return f

    def test_main_language_first_then_preference(self):
        streams = [(0x0F, 0x102, lang("eng")), (0x81, 0x103, lang("spa"))]
        self.assertEqual(self.feeder("", streams).pend, 0x102)
        self.assertEqual(self.feeder("es", streams).pend, 0x103)
        self.assertEqual(self.feeder("deu", streams).pend, 0x102)    # not carried: the main track

    def test_audio_description_is_not_the_main_track(self):
        streams = [(0x0F, 0x102, lang("eng", 3)), (0x81, 0x103, lang("eng"))]
        self.assertEqual(self.feeder("", streams).pend, 0x103)

    def test_untagged_tracks_count_as_the_main_language(self):
        streams = [(0x0F, 0x102, b""), (0x81, 0x103, b""), (0x81, 0x104, lang("spa"))]
        self.assertEqual(self.feeder("", streams).pend, 0x103)       # Dolby wins the tie, Spanish stays out


class StreamTests(unittest.TestCase):
    def run_feed(self, chunks):
        f = aslots.Feeder("")
        out = f.psi()
        for c in chunks:
            out += f.feed(c)
        return f, out

    def test_flip_moves_to_another_input_and_time_never_goes_back(self):
        head = program([(0x81, 0x102, b"")])
        body = b""
        t = 900000
        for k in range(40):   # 5.1, then stereo (an ad), then 5.1 again, with pictures in between
            frame = AC3_20 if 10 <= k < 25 else AC3_51
            body += packets(0x101, pes(0xE0, t, b"\x00" * 100)) + packets(0x102, pes(0xBD, t, frame))
            t += 2880
        f, out = self.run_feed([head + body])
        sent = slot_pts(out)
        last = {}
        for s, p in sent:
            self.assertGreaterEqual(p, last.get(s, 0) - 9000, "input %d went back" % s)
            last[s] = p
        real = [p for s, p in sent if s in (0, 1)]
        self.assertTrue(real)
        self.assertEqual(f.cur, 0x102)

    def test_a_stale_frame_is_never_sent(self):
        head = program([(0x81, 0x102, b"")])
        body = b""
        t = 900000
        for _ in range(20):
            body += packets(0x101, pes(0xE0, t, b"\x00" * 100)) + packets(0x102, pes(0xBD, t, AC3_51))
            t += 2880
        body += packets(0x102, pes(0xBD, 900000 - 90000 * 30, AC3_51))   # a frame from 30 s ago
        _, out = self.run_feed([head + body])
        self.assertTrue(all(p >= 900000 - 9000 for _, p in slot_pts(out)))

    def test_every_input_starts_at_once_even_without_audio(self):
        head = program([(0x81, 0x102, b"")])
        body = b"".join(packets(0x101, pes(0xE0, 900000 + k * 3003, b"\x00" * 100)) for k in range(30))
        f, out = self.run_feed([head + body])
        self.assertEqual({s for s, _ in slot_pts(out)}, set(f.active))
        self.assertEqual(f.active, aslots.CORE)


class FailoverTests(unittest.TestCase):
    def pmt_pid_of(self, pat):
        o = 4 + 1 + pat[4]
        return (pat[o + 10] & 31) << 8 | pat[o + 11]

    def old_stream(self):
        f = aslots.Feeder("")
        f.psi()
        f.feed(program([(0x81, 0x102, b"")]) + packets(0x102, pes(0xBD, 900000, AC3_51)))
        f.reset()
        return f

    def test_a_backup_on_another_pmt_pid_gets_its_own_tables(self):
        f = self.old_stream()
        f.learn(program([(0x81, 0x102, b"")], pmt=0x200))
        f.plan()
        out = f.psi()
        self.assertEqual(self.pmt_pid_of(out[:188]), 0x200)
        self.assertEqual((out[189] & 31) << 8 | out[190], 0x200)

    def test_a_backup_whose_tables_were_not_seen_gets_none_of_the_old_ones(self):
        f = self.old_stream()
        f.plan()
        self.assertEqual(f.psi(), b"")


if __name__ == "__main__":
    unittest.main()
