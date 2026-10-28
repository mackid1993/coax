"""Is a feed's sound offset from its picture, compared with another feed of the same channel?

Two feeds of one channel carry the same program. Their picture and their sound should each line up with
the other feed by the same amount. If the sound lines up 12 s later than the picture does, the new feed's
sound is 12 s off its picture.

offset(ref_path, cand_path) aligns the sound with ffmpeg's chromaprint audio fingerprint (within MAXLAG), then
aligns a low-resolution picture-change signal at RATE Hz, searched within PICWIN of where the sound matched.
It returns a dict with "offset" (seconds, cand sound minus cand picture relative to ref; None when not
confident) and the evidence. Only stdlib and ffmpeg; no network.
"""
import array
import math
import subprocess

RATE = 20          # signal samples per second
MAXLAG = 90.0      # how far apart the two feeds may run, seconds
MIN_OVERLAP = 12.0  # seconds of shared content needed for a verdict
OUT_OF_SYNC = 2.0  # seconds; a smaller offset is left alone
PICWIN = 40.0      # seconds; the picture is searched this far either side of where the sound matched


def _starts(ff, path):
    probe = ff.replace("ffmpeg", "ffprobe") if ff.endswith("ffmpeg") else "ffprobe"
    o = subprocess.run([probe, "-v", "quiet", "-show_entries", "stream=codec_type,start_time", "-of", "csv=p=0",
                        path], capture_output=True, text=True, timeout=30).stdout
    v = a = None
    for row in o.splitlines():
        f = row.split(",")
        if len(f) >= 2 and f[1] not in ("", "N/A"):
            if f[0] == "video" and v is None:
                v = float(f[1])
            elif f[0] == "audio" and a is None:
                a = float(f[1])
    return v, a


def _picture(ff, path):
    raw = subprocess.run([ff, "-v", "quiet", "-i", path, "-map", "0:v:0", "-vf",
                          "fps=%d,scale=32:18,format=gray" % RATE, "-f", "rawvideo", "-"],
                         capture_output=True, timeout=120).stdout
    n = 32 * 18
    frames = [raw[i:i + n] for i in range(0, len(raw) - n + 1, n)]
    out = [0.0]
    for p, q in zip(frames, frames[1:]):
        out.append(sum(abs(x - y) for x, y in zip(p, q)) / n)
    return out


HOP = 4096 / 3 / 11025.0  # seconds per chromaprint item (ffmpeg chromaprint muxer, raw format)


def _prints(ff, path):
    raw = subprocess.run([ff, "-v", "quiet", "-i", path, "-map", "0:a:0", "-f", "chromaprint", "-fp_format", "raw", "-"],
                         capture_output=True, timeout=120).stdout
    a = array.array("I")
    a.frombytes(raw[:len(raw) // 4 * 4])
    return list(a)


def _audio_lag(ref, cand, maxlag):
    """Audio fingerprint alignment: (seconds t with cand content at ref time + t, bit error, runner-up error)."""
    need = int(MIN_OVERLAP / HOP)
    span = int(maxlag / HOP)
    res = []
    for k in range(-span, span + 1):
        lo, hi = max(0, -k), min(len(cand), len(ref) - k)
        if hi - lo < need:
            continue
        e = 0
        for i in range(lo, hi):
            e += bin(ref[i + k] ^ cand[i]).count("1")
        res.append((e / (32.0 * (hi - lo)), k))
    if not res:
        return None, 1.0, 1.0
    res.sort()
    best = res[0]
    second = next((x for x, k in res if abs(k - best[1]) * HOP > 1.0), 1.0)
    return -best[1] * HOP, best[0], second


def _xcorr(a, b, maxlag, center=0):
    """Best k with a[i] ~ b[i + k]: (k, peak, runner-up at least 1 s away, overlap samples)."""
    def z(x):
        m = sum(x) / len(x)
        sd = math.sqrt(sum((v - m) ** 2 for v in x) / len(x)) or 1.0
        return [(v - m) / sd for v in x]
    if len(a) < 2 or len(b) < 2:
        return 0, -1.0, -1.0, 0
    a, b = z(a), z(b)
    res = []
    for k in range(center - maxlag, center + maxlag + 1):
        lo, hi = max(0, -k), min(len(a), len(b) - k)
        n = hi - lo
        if n < MIN_OVERLAP * RATE:
            continue
        s = 0.0
        for i in range(lo, hi):
            s += a[i] * b[i + k]
        res.append((s / n, k, n))
    if not res:
        return 0, -1.0, -1.0, 0
    res.sort(reverse=True)
    best = res[0]
    second = next((c for c, k, _ in res if abs(k - best[1]) > RATE), -1.0)
    return best[1], best[0], second, best[2]


def offset(ref_path, cand_path, ff="ffmpeg"):
    rv, ra = _starts(ff, ref_path)
    cv, ca = _starts(ff, cand_path)
    out = {"offset": None, "why": ""}
    if None in (rv, ra, cv, ca):
        out["why"] = "missing audio or video"
        return out
    # Sound first, by audio fingerprint (chromaprint): it pins where the same program sits in both feeds.
    t, err, err2 = _audio_lag(_prints(ff, ref_path), _prints(ff, cand_path), MAXLAG)
    if t is None or err > 0.30 or err2 - err < 0.08:
        out["why"] = "sound does not match clearly"
        out["sound"] = (None, round(err, 2), round(err2, 2))
        return out
    snd = (ca - ra) - t
    # Then the picture, searched only near where the sound says the program is, so an unrelated scene that
    # happens to look alike far away cannot win.
    exp = int(round((t - (ca - ra) + (cv - rv)) * RATE))   # picture lag that would mean "in sync"
    kv, pv, sv, nv = _xcorr(_picture(ff, ref_path), _picture(ff, cand_path), int(PICWIN * RATE), exp)
    pic = (cv - rv) - kv / RATE   # how much later the cand picture is stamped for the same content
    out.update(picture=(round(pic, 2), round(pv, 2), round(sv, 2)), sound=(round(snd, 2), round(err, 2), round(err2, 2)),
               overlap=round(nv / RATE, 1))
    if pv < 0.45 or pv - sv < 0.15:
        out["why"] = "pictures do not match clearly"
        return out
    out["offset"] = round(snd - pic, 1)
    return out


if __name__ == "__main__":
    import json
    import sys
    print(json.dumps(offset(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "ffmpeg")))
