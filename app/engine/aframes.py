"""Audio frame headers: where each compressed audio frame ends, how long it plays and its exact format.

Used by aslots to cut the provider's audio into frames and send each frame to the transcoder input that decodes
that exact format, so ffmpeg never sees a format change (see aslots). Pure byte parsing, no ffmpeg.

frame(fam, b, i) -> (size, key, samples, rate) for the frame starting at b[i], or None when b[i] does not start a
frame of that family (resync needed), or (0, ...) when the frame is not complete yet. key names the decoded
format: two frames with the same key decode to the same sample rate and channel layout.
"""

AC3_RATES = (48000, 44100, 32000)
AC3_KBPS = (32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384, 448, 512, 576, 640)
AAC_RATES = (96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050, 16000, 12000, 11025, 8000, 7350)
MPA_KBPS = {(3, 3): (0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448),   # MPEG-1 layer I
            (3, 2): (0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384),      # MPEG-1 layer II
            (3, 1): (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),       # MPEG-1 layer III
            (2, 3): (0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256),      # MPEG-2/2.5 layer I
            (2, 2): (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),           # MPEG-2/2.5 II, III
            (2, 1): (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160)}
MPA_RATES = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000), 0: (11025, 12000, 8000)}
DTS_RATES = (0, 8000, 16000, 32000, 0, 0, 11025, 22050, 44100, 0, 0, 12000, 24000, 48000, 0, 0)

SYNC = {"ac3": b"\x0b\x77", "eac3": b"\x0b\x77", "aac": b"\xff", "latm": b"\x56", "mp2": b"\xff",
        "dts": b"\x7f\xfe\x80\x01"}
HEAD = {"ac3": 8, "eac3": 8, "aac": 7, "latm": 3, "mp2": 4, "dts": 11}   # eac3: 8, an AC-3 frame can appear there


def frame(fam, b, i):
    n = len(b) - i
    if n < HEAD[fam]:
        return (0, None, 0, 0) if b[i:i + 1] == SYNC[fam][:1] or n == 0 else None
    if fam in ("ac3", "eac3"):
        if b[i] != 0x0B or b[i + 1] != 0x77:
            return None
        bsid = b[i + 5] >> 3
        if bsid <= 10:                                   # AC-3
            fscod, code = b[i + 4] >> 6, b[i + 4] & 63
            if fscod == 3 or code > 37:
                return None
            kbps = AC3_KBPS[code >> 1]
            words = 2 * kbps if fscod == 0 else 3 * kbps if fscod == 2 else 320 * kbps // 147 + (code & 1)
            acmod = b[i + 6] >> 5
            bit = 3 + (2 if acmod & 1 and acmod != 1 else 0) + (2 if acmod & 4 else 0) + (2 if acmod == 2 else 0)
            lfe = (int.from_bytes(b[i + 6:i + 8], "big") >> (15 - bit)) & 1
            size, key, samples, rate = 2 * words, ("ac3", acmod, lfe, fscod), 1536, AC3_RATES[fscod]
        elif bsid <= 16:                                 # E-AC-3
            strmtyp = b[i + 2] >> 6
            size = (((b[i + 2] & 7) << 8 | b[i + 3]) + 1) * 2
            fscod = b[i + 4] >> 6
            if fscod == 3:
                rate, blocks = (24000, 22050, 16000, 0)[(b[i + 4] >> 4) & 3], 6
            else:
                rate, blocks = AC3_RATES[fscod], (1, 2, 3, 6)[(b[i + 4] >> 4) & 3]
            if not rate:
                return None
            if strmtyp == 1:                             # a dependent substream belongs to the frame before it
                key, samples = None, 0
            else:
                key, samples = ("eac3", (b[i + 4] >> 1) & 7, b[i + 4] & 1, rate), blocks * 256
        else:
            return None
    elif fam == "aac":                                   # ADTS
        if b[i] != 0xFF or b[i + 1] & 0xF6 != 0xF0:
            return None
        sf = (b[i + 2] >> 2) & 15
        if sf >= len(AAC_RATES):
            return None
        size = (b[i + 3] & 3) << 11 | b[i + 4] << 3 | b[i + 5] >> 5
        if size < 7:
            return None
        cc = (b[i + 2] & 1) << 2 | b[i + 3] >> 6
        key, samples, rate = ("aac", b[i + 2] >> 6, sf, cc), 1024 * ((b[i + 6] & 3) + 1), AAC_RATES[sf]
    elif fam == "latm":                                  # LOAS: the format is inside, one input for all of it
        if b[i] != 0x56 or b[i + 1] & 0xE0 != 0xE0:
            return None
        size, key, samples, rate = ((b[i + 1] & 31) << 8 | b[i + 2]) + 3, ("latm",), 1024, 48000
    elif fam == "mp2":                                   # MPEG audio layers I to III
        if b[i] != 0xFF or b[i + 1] & 0xE0 != 0xE0:
            return None
        ver, layer, bi, si = (b[i + 1] >> 3) & 3, (b[i + 1] >> 1) & 3, b[i + 2] >> 4, (b[i + 2] >> 2) & 3
        if ver == 1 or layer == 0 or bi in (0, 15) or si == 3:
            return None
        kbps = MPA_KBPS[(3 if ver == 3 else 2, layer)][bi] * 1000
        rate = MPA_RATES[ver][si]
        pad = (b[i + 2] >> 1) & 1
        if layer == 3:
            size, samples = (12 * kbps // rate + pad) * 4, 384
        elif layer == 2 or ver == 3:
            size, samples = 144 * kbps // rate + pad, 1152
        else:
            size, samples = 72 * kbps // rate + pad, 576
        key = ("mp2", ver, layer, si, b[i + 3] >> 6 == 3)
    else:                                                # DTS core
        if b[i:i + 4] != b"\x7f\xfe\x80\x01":
            return None
        size = ((b[i + 5] & 3) << 12 | b[i + 6] << 4 | b[i + 7] >> 4) + 1
        samples = (((b[i + 4] & 1) << 6 | b[i + 5] >> 2) + 1) * 32
        rate = DTS_RATES[(b[i + 8] >> 2) & 15]
        if size < 96 or not rate:
            return None
        key = ("dts", (b[i + 7] & 15) << 2 | b[i + 8] >> 6, rate)
    if size > n:
        return (0, key, samples, rate)
    return (size, key, samples, rate)


def frames(fam, b):
    """Every whole frame in b as (offset, size, key, samples, rate). For the silent frames and the tests."""
    out, i = [], 0
    while i < len(b):
        f = frame(fam, b, i)
        if not f or not f[0]:
            break
        out.append((i,) + f)
        i += f[0]
    return out
