"""Audio slots: the provider's audio fed to the transcoder so that ffmpeg never has to change decoders.

ffmpeg picks its audio decoder once, when it starts. A provider that switches a channel's audio codec mid-stream
(an AAC ad in a Dolby show), moves the audio to another track, or drops it for a while would leave that decoder
reading nothing or the wrong thing: silence, or a restart. So the engine rewrites the stream it feeds ffmpeg:

- The program table (PMT) lists one fixed audio track per codec family (Dolby Digital, Dolby Digital Plus, AAC,
  AAC-LATM, MPEG audio, DTS) on fixed PIDs, in place of the provider's audio tracks. ffmpeg opens a decoder for
  each and mixes them (amix, normalize=0) into the one output format.
- The provider track that should play (choose()) goes into the slot of its codec; every other track is dropped.
- Every other slot gets silent frames on the same clock, so each decoder always has input and the mix never
  waits. When no provider audio is playing at all, every slot gets silence on the video's clock.

A codec switch, a track moving, a stereo ad in a 5.1 show, a failover to a feed with other audio: the audio
simply arrives in another slot, with no restart and no change in what leaves coax.
"""
import base64
import json
import os

FAMILIES = ("ac3", "eac3", "aac", "latm", "mp2", "dts")
PIDS = (0x1FE0, 0x1FE1, 0x1FE2, 0x1FE3, 0x1FE4, 0x1FE5)
STYPE = (0x81, 0x87, 0x0F, 0x11, 0x03, 0x82)
SID = (0xBD, 0xBD, 0xC0, 0xC0, 0xC0, 0xBD)
FDUR = (2880, 2880, 1920, 1920, 2160, 960)   # 90 kHz ticks per silent frame (48 kHz)
DOLBY = (0, 1)
WRAP = 1 << 33
LAG = 45000            # silence on the video clock trails the picture by 0.5 s
GAP = 90000            # no playing audio for 1 s of video: every slot gets silence
JUMP = 90000 * 10      # a clock jump (provider restart, wrap) restarts a slot's silence there
LIVE = 90000 * 3       # a track counts as delivering if it sent audio in the last 3 s

SILENT = tuple(base64.b64decode(x) for x in (
    "C3fvBwxAQ+EG8AEICAghAQEEVx8+fPnz58+fPnz7/nV8+fPnz58+fPj/nV8+fPnz58+fPjL/Hs2bIAAAAAAAHju7u7u7u4AAAAAAAAAAAAAA"
    "AA1rWtaAAAAAAHd3d3d3dwAAAAAAAAAAAAAAAAwAAAAAAAADx3d3d3d3cAAAAAAAAAAAAAAAAa1rWtAAAAAADu7u7u7u4AAAAAAAAAAAAAAA"
    "AYAAAAAAAAB47u7u7u7uAAAAAAAAAAAAAAAANa1rWgAAAAAB3d3d3d3cAAAAAAAAAAAAAAAAMAAAAAAAAA8d3d3d3d3AAAAAAAAAAAAAAAAG"
    "ta1rQAAAAAA7u7u7u7uAAAAAAAAAAAAAAAAGAAAAAAAAAeO7u7u7u7gAAAAAAAAAAAAAAADWta1oAAAAAAd3d3d3d3AAAAAAAAAAAAAAAADA"
    "AAAAAAAAPHd3d3d3dwAAAAAAAAAAAAAAABrWta0AAAAAAO7u7u7u7gAAAAAAAAAAAAAAAAAAAAAAAFK2",
    "C3cAvzSHwAAgAAAARwAABAQEAQEBAY+fPnz58+fPnz59/zq+fPnz58+fPnx/zq+fPnz58+fPnwAAAAAAB3jx48d3d3d3AAAAAAAAAAAADWta"
    "1rWta+fAAAAAA7x48eO7u7u7gAAAAAAAAAAAAAAAAAADvHjx47u7u7uAAAAAAAAAAAAGta1rWta18+AAAAAB3jx48d3d3d3AAAAAAAAAAAAA"
    "AAAAAAHePHjx3d3d3cAAAAAAAAAAAANa1rWta1r58AAAAADvHjx47u7u7uAAAAAAAAAAAAAAAAAAAO8ePHju7u7u4AAAAAAAAAAAAa1rWta1"
    "rXz4AAAAAHePHjx3d3d3cAAAAAAAAAAAAAAAAAAAd48ePHd3d3dwAAAAAAAAAAAA1rWta1rWvnwAAAAAO8ePHju7u7u4AAAAAAAAAAAAAAAA"
    "AAA7x48eO7u7u7gAAAAAAAAAAABrWta1rWtfPgAAAAAd48ePHd3d3dwAAAAAAAAAAAAAAAAAAAAAABqv",
    "//FMgAG//CEQBGCMHA==",
    "VuAeIAARkB/gvvAQAmMLsxmxkXGRwXGJgZACEQBGCMHA",
    "//1EBFUzbRISJAqqqqvvvvvvvvvvvvvnve971sWxbFsfNa+fNa973veti2LYtj5rXz5rXve971sWxbFsfNa+fNa973veti2LYtj5rXz5rXve"
    "971sWxbFsfNa+fNa973veti2LYtj5rXz5rXve971sWxbFsfNa+fNa973veti2LYtj5rXz5rXve971sWxbFsfNa+fNa973veti2LYtj5rXz5r"
    "Xve971sWxbFsfNa+fNa973veti2LYtj5rXz5rQAA",
    "f/6AAfw8P/C14AE4AAPvf+AG2x/h7f7bYAAAACAAAAAAAAAAAc574Pe5zmta1rGMWpKDECEIQhCFznvg97nOa1rWsYxakoMQIQhCEIQAAAAA"
    "AAAAAKFChQ0UKFChQePHjx4kSJBAgAAAAAAAAAAAAAChQoUNFChQoUHjx48eJEiQQIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAACIiIiIkkkkVVUAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAiIiIiJJ"
    "JJFVVAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAIiIiIiSSSRVVQAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACIiIiIkkkkVVUAAAAAAAAAAAAA//8AA==",
))


def dif(a, b):
    """a - b in 90 kHz ticks across the 33-bit wrap."""
    return (a - b + WRAP // 2) % WRAP - WRAP // 2


def _languages():
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "languages.json"), encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    out = {}
    for lang in data.get("languages") or []:
        for c in [lang["code"]] + lang.get("aliases", []):
            out[c.lower()] = lang["code"]
    return out


ALIASES = _languages()
SAME = {"fil": "tgl", "nob": "nor", "nno": "nor"}   # codes providers use for the same spoken language


def canon(code):
    """A language tag as its ISO 639-2 code ("spa" for "es", "fra" for "fre"), or "" when it names no language."""
    c = (code or "").strip().lower()
    c = ALIASES.get(c, c if len(c) == 3 and c.isalpha() else "")
    if c in ("und", "mul", "mis", "zxx") or "qaa" <= c <= "qtz":
        return ""
    return SAME.get(c, c)


def family(st, desc):
    """Slot index for an elementary stream (stream type and descriptors), or None when it is not audio we take."""
    tags = {}
    k = 0
    while k + 2 <= len(desc):
        tags.setdefault(desc[k], desc[k + 2:k + 2 + desc[k + 1]])
        k += 2 + desc[k + 1]
    reg = tags.get(5, b"")[:4]
    if st == 0x81 or (st == 6 and (0x6A in tags or reg == b"AC-3")):
        return 0
    if st == 0x87 or (st == 6 and (0x7A in tags or reg == b"EAC3")):
        return 1
    if st == 0x0F:
        return 2
    if st == 0x11:
        return 3
    if st in (0x03, 0x04):
        return 4
    if st in (0x82, 0x85, 0x86) or (st == 6 and (0x7B in tags or reg in (b"DTS1", b"DTS2", b"DTS3"))):
        return 5
    return None


def describe(desc):
    """(language, main) from an audio track's descriptors. main is False for audio description, hearing impaired,
    commentary and similar extra tracks."""
    lang, main = "", True
    k = 0
    while k + 2 <= len(desc):
        tag, n = desc[k], desc[k + 1]
        b = desc[k + 2:k + 2 + n]
        if tag == 0x0A and n >= 4:                       # ISO 639 language: code + audio_type
            lang = lang or b[:3].decode("latin-1")
            if b[3] in (2, 3):
                main = False
        elif tag == 0x7F and n >= 2 and b[0] == 6:       # DVB supplementary audio: editorial classification
            if (b[1] >> 2) & 31:
                main = False
            if b[1] & 1 and n >= 5:
                lang = b[2:5].decode("latin-1")
        elif tag in (0x6A, 0x7A) and n >= 2 and b[0] & 0x80:   # DVB AC-3 / E-AC-3: component type
            if (b[1] >> 3) & 7:
                main = False
        elif tag == 0x81 and n >= 3:                     # ATSC AC-3: bit stream mode
            if b[2] >> 5:
                main = False
        k += 2 + n
    return canon(lang), main


AC3CH = (2, 1, 2, 3, 3, 4, 4, 5)


def frame_ch(b):
    """Channels of the first audio frame in b (Dolby, ADTS AAC, MPEG audio), or 0 when none is found."""
    for i in range(min(len(b), 64) - 7):
        if b[i] == 0x0B and b[i + 1] == 0x77:
            bsid = b[i + 5] >> 3
            if bsid <= 10:
                acmod = b[i + 6] >> 5
                bit = 3 + (2 if acmod & 1 and acmod != 1 else 0) + (2 if acmod & 4 else 0) + (2 if acmod == 2 else 0)
                return AC3CH[acmod] + ((int.from_bytes(b[i + 6:i + 8], "big") >> (15 - bit)) & 1)
            if 11 <= bsid <= 16:
                return AC3CH[(b[i + 4] >> 1) & 7] + (b[i + 4] & 1)
        if b[i] == 0xFF and b[i + 1] & 0xF6 == 0xF0:
            cc = (b[i + 2] & 1) << 2 | b[i + 3] >> 6
            if 1 <= cc <= 7:
                return (0, 1, 2, 3, 4, 5, 6, 8)[cc]
        if b[i] == 0xFF and b[i + 1] & 0xE0 == 0xE0 and b[i + 1] & 6:
            return 1 if b[i + 3] >> 6 == 3 else 2
    return 0


def _crc(data):
    crc = 0xFFFFFFFF
    for x in data:
        crc ^= x << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7 if crc & 0x80000000 else crc << 1) & 0xFFFFFFFF
    return crc


def _pts(p, o):
    """PTS of the PES starting at offset o of packet p, or None."""
    if o + 14 > 188 or p[o:o + 3] != b"\x00\x00\x01" or not p[o + 7] & 0x80:
        return None
    b = p[o + 9:o + 14]
    return ((b[0] >> 1) & 7) << 30 | b[1] << 22 | (b[2] >> 1) << 15 | b[3] << 7 | b[4] >> 1


class Feeder:
    """Rewrites one engine's transcoder input; see the module docstring. pref: preferred language ("" for the
    provider's main track). Not thread safe: call feed() from the writer only."""

    def __init__(self, pref="", log=None):
        self.pref = canon(pref)
        self.log = log or (lambda m: None)
        self.pat = b""
        self.pmt_pid = None
        self.sec = None             # PMT section being assembled
        self.pmt_out = b""          # our PMT, packetized
        self.pmt_cc = 0
        self.tracks = []            # [{pid, fam, lang, main, order}] in the provider's order
        self.fam = {}               # audio pid -> slot index (None: a codec we do not take)
        self.cc = [0] * len(PIDS)
        self.end = [None] * len(PIDS)   # where each slot's audio ends, 90 kHz
        self.cur = None             # the provider track playing
        self.pend = None            # the track to switch to at its next PES start
        self.seen = {}              # pid -> PTS of its last PES start
        self.ch = {}                # pid -> channels of its last frame
        self.vclk = None
        self.aclk = None            # PTS of the playing track's last PES
        self.va = None              # the video clock when that PES arrived
        self.up = [None, 0]         # a better track in the same group, and how many checks in a row
        self.said = None

    # -- clock -----------------------------------------------------------------------------------------
    def reset(self):
        """A new stream (failover, hand switch): its clock and tracks start over."""
        self.end = [None] * len(PIDS)
        self.seen, self.ch = {}, {}
        self.vclk = self.aclk = self.va = None
        self.cur = self.pend = None
        self.up = [None, 0]

    def psi(self):
        """PAT and our PMT, for the first bytes a new encoder gets (it may start mid-segment, before the next
        table): ffmpeg knows the slots at once."""
        return self.pat + self.pmt_out

    # -- silence ---------------------------------------------------------------------------------------
    def _pes(self, s, pts, n):
        f = SILENT[s] * n
        p = pts % WRAP
        pay = bytes([0, 0, 1, SID[s]]) + (8 + len(f)).to_bytes(2, "big") + bytes([
            0x80, 0x80, 5, 0x21 | (p >> 29) & 0x0E, (p >> 22) & 0xFF, 0x01 | (p >> 14) & 0xFE, (p >> 7) & 0xFF,
            0x01 | (p << 1) & 0xFE]) + f
        return self._packets(PIDS[s], pay, s)

    def _packets(self, pid, pay, s=None, start=True):
        out = bytearray()
        i = 0
        while i < len(pay):
            chunk = pay[i:i + 184]
            i += 184
            if s is None:
                cc = self.pmt_cc
                self.pmt_cc = (cc + 1) & 15
            else:
                cc = self.cc[s]
                self.cc[s] = (cc + 1) & 15
            h = bytes([0x47, (0x40 if start and i == 184 else 0) | pid >> 8, pid & 0xFF])
            if len(chunk) < 184:
                st = 184 - len(chunk)
                out += h + bytes([0x30 | cc, st - 1]) + (bytes([0]) + b"\xff" * (st - 2) if st > 1 else b"") + chunk
            else:
                out += h + bytes([0x10 | cc]) + chunk
        return bytes(out)

    def _fill(self, s, upto):
        """Silent frames in slot s up to upto."""
        e = self.end[s]
        if e is None or abs(dif(upto, e)) > JUMP:
            self.end[s] = upto
            return b""
        n = dif(upto, e) // FDUR[s]
        out = []
        while n > 0:
            k = min(n, 30)
            out.append(self._pes(s, e, k))
            e += k * FDUR[s]
            n -= k
        self.end[s] = e % WRAP
        return b"".join(out)

    # -- program table ---------------------------------------------------------------------------------
    def _pmt(self, sec):
        """Our PMT from the provider's: its video and other tracks, its audio replaced by the slots."""
        if len(sec) < 16 or sec[0] != 2:
            return
        pil = (sec[10] & 15) << 8 | sec[11]
        keep = bytearray()
        tracks, fam = [], {}
        j = 12 + pil
        while j + 5 <= len(sec) - 4:
            st, pid, il = sec[j], (sec[j + 1] & 31) << 8 | sec[j + 2], (sec[j + 3] & 15) << 8 | sec[j + 4]
            desc = bytes(sec[j + 5:j + 5 + il])
            f = family(st, desc)
            if f is not None:
                lang, main = describe(desc)
                tracks.append({"pid": pid, "fam": f, "lang": lang, "main": main, "order": len(tracks)})
                fam[pid] = f
            elif pid not in PIDS:
                keep += sec[j:j + 5 + il]   # video, subtitles, data and audio we cannot take stay as they are
            j += 5 + il
        if [(t["pid"], t["fam"], t["lang"], t["main"]) for t in tracks] != \
                [(t["pid"], t["fam"], t["lang"], t["main"]) for t in self.tracks]:
            self.log("audio tracks: " + (", ".join("%s %s%s" % (t["lang"] or "untagged", FAMILIES[t["fam"]],
                     "" if t["main"] else " (extra)") for t in tracks) or "none"))
        self.tracks, self.fam = tracks, fam
        es = bytes(keep) + b"".join(bytes([STYPE[i], 0xE0 | PIDS[i] >> 8, PIDS[i] & 0xFF, 0xF0, 0])
                                    for i in range(len(PIDS)))
        body = bytes(sec[3:12 + pil]) + es
        n = len(body) + 4
        new = bytes([2, 0xB0 | n >> 8, n & 0xFF]) + body
        new += _crc(new).to_bytes(4, "big")
        self.pmt_out = self._packets(self.pmt_pid, b"\x00" + new)
        self._choose(first=True)

    # -- which track plays -----------------------------------------------------------------------------
    def _group(self, live):
        """The tracks that may play, best group first: the preferred language, else the main track's language
        (untagged tracks count as it), else everything; extra tracks (audio description...) only if nothing else."""
        main_lang = self.tracks[0]["lang"] if self.tracks else ""
        groups = []
        if self.pref:
            groups.append([t for t in live if t["lang"] == self.pref])
        groups.append([t for t in live if t["lang"] in ("", main_lang)])
        groups.append(live)
        for g in groups:
            if g:
                return [t for t in g if t["main"]] or g
        return []

    def now(self):
        """The audio clock now: the playing track's last PES moved on by the video time since (the provider's own
        audio and video timestamps may sit apart), or the video clock before any audio."""
        if self.aclk is not None and self.va is not None and self.vclk is not None:
            return (self.aclk + dif(self.vclk, self.va)) % WRAP
        return self.vclk if self.aclk is None else self.aclk

    def _choose(self, first=False):
        clk = self.now()
        if first or clk is None or not self.seen:
            live = list(self.tracks)            # nothing heard yet: every listed track may deliver
        else:
            live = [t for t in self.tracks if t["pid"] in self.seen and abs(dif(clk, self.seen[t["pid"]])) < LIVE]
            if not live:
                return
        g = self._group(live)
        if not g:
            return
        rank = lambda t: (self.ch.get(t["pid"], 0), t["fam"] in DOLBY, -t["order"])  # noqa: E731
        best = max(g, key=rank)
        cur = next((t for t in g if t["pid"] == self.cur), None)
        want = best["pid"]
        if cur is not None and want != cur["pid"]:
            # Same group and still playing: stay (a 5.1 track that is stereo during an ad is not traded away),
            # unless another track has carried real surround for a few checks in a row while this one is stereo.
            if self.ch.get(want, 0) >= 6 and self.ch.get(cur["pid"], 0) <= 2:
                self.up = [want, self.up[1] + 1 if self.up[0] == want else 1]
                if self.up[1] < 4:
                    want = cur["pid"]
            else:
                self.up = [None, 0]
                want = cur["pid"]
        else:
            self.up = [None, 0]
        if want != self.cur and want != self.pend:
            self.pend = want
            t = next(x for x in self.tracks if x["pid"] == want)
            self.log("audio: playing the %s %s track%s" % (t["lang"] or "untagged", FAMILIES[t["fam"]],
                     "" if self.cur is None else " (was another track)"))

    # -- the stream ------------------------------------------------------------------------------------
    def feed(self, d):
        out = []
        for i in range(0, len(d) - 187, 188):
            p = d[i:i + 188]
            if p[0] != 0x47:
                continue
            pid = (p[1] & 31) << 8 | p[2]
            pusi = p[1] & 0x40
            af = p[3] >> 4 & 3
            o = 5 + p[4] if af == 3 else 4
            if af == 2 or o >= 188:
                if pid != self.pmt_pid and pid not in self.fam:
                    out.append(p)
                continue
            if pid == 0:
                if pusi:
                    q = o + 1 + p[o]
                    if q + 12 <= 188 and p[q] == 0:
                        for j in range(q + 8, min(q + 3 + ((p[q + 1] & 15) << 8 | p[q + 2]) - 4, 184), 4):
                            if p[j] << 8 | p[j + 1]:
                                self.pmt_pid = (p[j + 2] & 31) << 8 | p[j + 3]
                                break
                    self.pat = bytes(p)
                out.append(p)
            elif pid == self.pmt_pid:
                if pusi:
                    q = o + 1 + p[o]
                    self.sec = bytearray(p[q:188]) if q < 188 else None
                elif self.sec is not None:
                    self.sec += p[o:188]
                if self.sec is not None and len(self.sec) >= 3:
                    need = 3 + ((self.sec[1] & 15) << 8 | self.sec[2])
                    if len(self.sec) >= need:
                        sec, self.sec = bytes(self.sec[:need]), None
                        self._pmt(sec)
                        out.append(self.pmt_out)
            elif pid in self.fam:
                s = self.fam[pid]
                t = _pts(p, o) if pusi else None
                if t is not None:
                    self.seen[pid] = t
                    c = frame_ch(p[o + 9 + p[o + 8]:]) if o + 9 < 188 else 0
                    if c:
                        self.ch[pid] = c
                    if pid == self.pend:
                        self.cur, self.pend = pid, None
                    if pid == self.cur:
                        self.aclk, self.va = t, self.vclk
                        for x in range(len(PIDS)):
                            if x != s:
                                out.append(self._fill(x, t))
                        self.end[s] = t
                    if self.said is None or abs(dif(t, self.said)) > 45000:   # look again every half second
                        self.said = t
                        self._choose()
                if pid == self.cur and s is not None:
                    cc = self.cc[s]
                    if p[3] & 0x10:
                        self.cc[s] = (cc + 1) & 15
                    out.append(bytes([0x47, (p[1] & 0xE0) | PIDS[s] >> 8, PIDS[s] & 0xFF, (p[3] & 0xF0) | cc]) + p[4:])
            else:
                if pusi and o + 4 <= 188 and p[o:o + 3] == b"\x00\x00\x01" and 0xE0 <= p[o + 3] <= 0xEF:
                    v = _pts(p, o)
                    if v is not None:
                        self.vclk = v
                        gone = self.aclk is None or self.va is None or dif(v, self.va) > GAP
                        if gone:
                            # No audio playing (none yet, or the track stopped): silence on every slot, moving
                            # with the picture, so the mix and the picture never wait for it.
                            for x in range(len(PIDS)):
                                out.append(self._fill(x, (self.now() - LAG) % WRAP))
                            if self.aclk is not None and (self.said is None or abs(dif(v, self.said)) > 45000):
                                self.said = v
                                self._choose()
                out.append(p)
        return b"".join(out)
