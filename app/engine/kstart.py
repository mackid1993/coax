"""Start a fresh encoder on a video keyframe, with audio trimmed to the same moment.

Provider segments often begin mid-picture. A fresh ffmpeg then gets audio at once but nothing it can decode
until the first keyframe ("non-existing PPS", "no frame"), so it latches onto audio and video at different
moments. Holding back the video before the first keyframe and the audio stamped before it gives the encoder
sound and picture that begin together.
"""
WR = (1 << 33) / 90000.0
LIMIT = 40000      # packets (about 7.5 MB) to look through before giving up and passing the stream as is


def dif(a, b):
    return (a - b + WR / 2) % WR - WR / 2


def pests(p):
    if not p[1] & 64:
        return None
    af = p[3] >> 4 & 3
    if not af & 1:
        return None
    o = 5 + p[4] if af == 3 else 4
    if o + 14 > 188 or p[o:o + 3] != b"\x00\x00\x01":
        return None
    sid = p[o + 3]
    if not (192 <= sid <= 239 or sid == 189):
        return None
    fl = p[o + 7] & 192
    if not fl & 128:
        return None
    b = p[o + 14:o + 19] if fl == 192 and o + 19 <= 188 else p[o + 9:o + 14]
    return (sid, ((b[0] >> 1 & 7) << 30 | b[1] << 22 | b[2] >> 1 << 15 | b[3] << 7 | b[4] >> 1) / 90000.0)


def iskey(p):
    af = p[3] >> 4 & 3
    if af == 3 and p[4] and p[5] & 64:
        return True
    o = 5 + p[4] if af == 3 else 4
    b = bytes(p[o + 9 + p[o + 8]:])
    i = b.find(b"\x00\x00\x01")
    while 0 <= i < len(b) - 3:
        n = b[i + 3]
        if not n & 128 and (i + 4 < len(b) and b[i + 4] == 1 and (n >> 1 & 63 in (19, 20, 21, 32, 33)) or n & 31 in (5, 7)):
            return True
        i = b.find(b"\x00\x00\x01", i + 3)
    return False


class Start:
    def __init__(self):
        self.on = False

    def arm(self):
        self.on = True
        self.k = None
        self.vp, self.ap, self.ka = set(), set(), set()
        self.drop = {}
        self.dv = self.da = self.n = 0

    def filter(self, b):
        """-> (bytes to send, log message or None)."""
        if not self.on:
            return b, None
        out = []
        msg = None
        n = len(b) - len(b) % 188
        for i in range(0, n, 188):
            if not self.on:
                out.append(b[i:n])
                break
            p = b[i:i + 188]
            self.n += 1
            if p[0] != 71:
                continue
            pid = (p[1] & 31) << 8 | p[2]
            q = pests(p)
            if q:
                if 224 <= q[0] <= 239:
                    self.vp.add(pid)
                    if self.k is None and iskey(p):
                        self.k = q[1]
                else:
                    self.ap.add(pid)
                    d = self.k is None or dif(q[1], self.k) < 0
                    self.drop[pid] = d
                    if not d:
                        self.ka.add(pid)
            if pid in self.vp:
                if self.k is None:
                    self.dv += 1
                    continue
            elif pid in self.ap and self.drop.get(pid, True):
                self.da += 1
                continue
            out.append(p)
            if self.k is not None and self.ap and self.ka >= self.ap:
                self.on = False
                msg = "start: picture and sound begin together on a keyframe (%d video and %d audio packets before it held back)" % (self.dv, self.da)
        if self.on and self.n > LIMIT:
            self.on = False
            msg = "start: no keyframe found to start on; passing the stream through as is"
        return b"".join(out) + b[n:], msg
