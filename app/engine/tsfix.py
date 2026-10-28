"""Keeps the picture continuous across encoder restarts (transcode).

Every restart (speed change, new stream timeline, switch) starts a new ffmpeg whose output timestamps are set
from an estimate. If the estimate is off, the new encoder's first frame lands earlier or later than where the
old encoder stopped, and players turn that jump into repeated or skipped video and a lasting sound/picture
offset. Cont watches the timestamps actually sent and shifts every timestamp of the new encoder's output (PTS,
DTS and PCR, audio and video together, so their relation inside it is untouched) so its first frame follows the
previous encoder's last frame.
"""
WRAP = 1 << 33
TOL = 0.1          # seconds; smaller differences are left alone
MAXFIX = 30.0      # seconds; anything larger is not a restart error, so it is left alone
HOLD_S = 2.5       # how long the new encoder's output may be held while waiting for its first frame
HOLD_BYTES = 4000000


def dif(a, b):
    w = WRAP / 90000.0
    return (a - b + w / 2) % w - w / 2


def _rd(a, i):
    return (a[i] >> 1 & 7) << 30 | a[i + 1] << 22 | a[i + 2] >> 1 << 15 | a[i + 3] << 7 | a[i + 4] >> 1


def _wr(a, i, v):
    v %= WRAP
    a[i] = (a[i] & 0xF0) | (v >> 29 & 0x0E) | 1
    a[i + 1] = v >> 22 & 0xFF
    a[i + 2] = (v >> 14 & 0xFE) | 1
    a[i + 3] = v >> 7 & 0xFF
    a[i + 4] = (v << 1 & 0xFE) | 1


def _pes(a, j):
    af = a[j + 3] >> 4 & 3
    if not a[j + 1] & 64 or not af & 1:
        return None
    o = j + (5 + a[j + 4] if af == 3 else 4)
    if o + 14 > j + 188 or a[o] or a[o + 1] or a[o + 2] != 1 or not a[o + 7] & 128:
        return None
    return o


def first_video(b):
    for j in range(0, len(b) - 187, 188):
        if b[j] == 0x47:
            o = _pes(b, j)
            if o is not None and 224 <= b[o + 3] <= 239:
                return _rd(b, o + 9) / 90000.0
    return None


class Cont:
    def __init__(self, fps):
        self.fd0 = 1.0 / fps if fps else 1.0 / 30
        self.prev = None   # (last video timestamp, frame duration) of the previous encoder's output
        self.st = {}

    def feed(self, key, b, now):
        """Bytes read from encoder `key` -> (bytes to send now, correction in seconds or None)."""
        s = self.st.get(key)
        if s is None:
            s = self.st[key] = {"rem": b"", "lv": None, "fd": None, "dt": 0,
                                "hold": [] if self.prev else None, "t": now}
        s["now"] = now
        b = s["rem"] + b
        n = len(b) - len(b) % 188
        s["rem"] = b[n:]
        b = b[:n]
        d = None
        if s["hold"] is not None:
            s["hold"].append(b)
            h = b"".join(s["hold"])
            fv = first_video(h)
            if fv is None and len(h) < HOLD_BYTES and now - s["t"] < HOLD_S:
                s["hold"] = [h]
                return b"", None
            s["hold"] = None
            b = h
            if fv is not None:
                lv, fd = self.prev
                g = dif(lv + (fd or self.fd0), fv)
                if TOL < abs(g) < MAXFIX:
                    s["dt"] = int(round(g * 90000))
                    d = g
        return self._shift(b, s), d

    def silent(self, key, now):
        """True once encoder `key` has sent video but no audio for 10 s (at most once per encoder)."""
        s = self.st.get(key)
        if not s or s.get("told") or s.get("vv") is None or now - s["vv"] > 2 or now - s["t"] < 15:
            return False
        if now - (s.get("aa") or s["t"]) > 10:
            s["told"] = True
            return True
        return False

    def end(self, key):
        """Encoder `key` finished: what is left of its output; it becomes the one the next encoder follows."""
        s = self.st.pop(key, None)
        if s is None:
            return b""
        out = self._shift(b"".join(s["hold"]), s) if s["hold"] else b""
        self.prev = (s["lv"], s["fd"]) if s["lv"] is not None else None
        return out + s["rem"]

    def _shift(self, b, s):
        dt = s["dt"]
        a = bytearray(b)
        for j in range(0, len(a) - 187, 188):
            if a[j] != 0x47:
                continue
            if dt:
                af = a[j + 3] >> 4 & 3
                if af & 2 and a[j + 4] >= 7 and a[j + 5] & 0x10:
                    k = j + 6
                    base = (a[k] << 25 | a[k + 1] << 17 | a[k + 2] << 9 | a[k + 3] << 1 | a[k + 4] >> 7)
                    base = (base + dt) % WRAP
                    a[k] = base >> 25 & 255
                    a[k + 1] = base >> 17 & 255
                    a[k + 2] = base >> 9 & 255
                    a[k + 3] = base >> 1 & 255
                    a[k + 4] = (base & 1) << 7 | (a[k + 4] & 127)
            o = _pes(a, j)
            if o is None:
                continue
            if dt:
                _wr(a, o + 9, _rd(a, o + 9) + dt)
                if a[o + 7] & 192 == 192 and o + 19 <= j + 188:
                    _wr(a, o + 14, _rd(a, o + 14) + dt)
            if 192 <= a[o + 3] <= 223 or a[o + 3] == 189:
                s["aa"] = s.get("now")
            if 224 <= a[o + 3] <= 239:
                s["vv"] = s.get("now")
                v = _rd(a, o + 9) / 90000.0
                lv = s["lv"]
                if lv is None:
                    s["lv"] = v
                else:
                    g = dif(v, lv)
                    if g > 0:
                        if g < 0.2 and (s["fd"] is None or g < s["fd"]):
                            s["fd"] = g
                        s["lv"] = v
        return bytes(a)
