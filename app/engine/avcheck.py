"""Is a feed's sound offset from its picture, compared with another feed of the same channel?

Two feeds of one channel carry the same program. Their picture and their sound should each line up with
the other feed by the same amount. If the sound lines up 12 s later than the picture does, the new feed's
sound is 12 s off its picture.

offset(ref_path, cand_path) decodes both files at low resolution (video) and 8 kHz mono (audio), builds a
picture-change signal and a sound-energy signal at RATE Hz, and cross-correlates each pair within MAXLAG.
It returns a dict with "offset" (seconds, cand sound minus cand picture relative to ref; None when not
confident) and the evidence. Only stdlib and ffmpeg; no network.
"""
import array
import math
import subprocess

RATE = 20          # signal samples per second
MAXLAG = 40.0      # how far apart the two feeds may run, seconds
MIN_OVERLAP = 12.0  # seconds of shared content needed for a verdict
OUT_OF_SYNC = 2.0  # seconds; a smaller offset is left alone


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


def _sound(ff, path):
    raw = subprocess.run([ff, "-v", "quiet", "-i", path, "-map", "0:a:0", "-ac", "1", "-ar", "8000",
                          "-f", "s16le", "-"], capture_output=True, timeout=120).stdout
    a = array.array("h")
    a.frombytes(raw[:len(raw) // 2 * 2])
    step = 8000 // RATE
    env = []
    for i in range(0, len(a) - step + 1, step):
        blk = a[i:i + step]
        env.append(math.log(1.0 + math.sqrt(sum(x * x for x in blk) / step)))
    # remove the slow level so only the rhythm of the sound (words, cuts, music beats) is compared
    w = RATE
    out = []
    for i in range(len(env)):
        lo, hi = max(0, i - w), min(len(env), i + w + 1)
        out.append(env[i] - sum(env[lo:hi]) / (hi - lo))
    return out


def _xcorr(a, b, maxlag):
    """Best k with a[i] ~ b[i + k]: (k, peak, runner-up at least 1 s away, overlap samples)."""
    def z(x):
        m = sum(x) / len(x)
        sd = math.sqrt(sum((v - m) ** 2 for v in x) / len(x)) or 1.0
        return [(v - m) / sd for v in x]
    if len(a) < 2 or len(b) < 2:
        return 0, -1.0, -1.0, 0
    a, b = z(a), z(b)
    res = []
    for k in range(-maxlag, maxlag + 1):
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
    lag = int(MAXLAG * RATE)
    kv, pv, sv, nv = _xcorr(_picture(ff, ref_path), _picture(ff, cand_path), lag)
    ka, pa, sa, na = _xcorr(_sound(ff, ref_path), _sound(ff, cand_path), lag)
    pic = (cv - rv) - kv / RATE   # how much later the cand picture is stamped for the same content
    snd = (ca - ra) - ka / RATE
    out.update(picture=(round(pic, 2), round(pv, 2), round(sv, 2)), sound=(round(snd, 2), round(pa, 2), round(sa, 2)),
               overlap=round(min(nv, na) / RATE, 1))
    if pv < 0.45 or pv - sv < 0.15:
        out["why"] = "pictures do not match clearly"
        return out
    if pa < 0.3 or pa - sa < 0.08:
        out["why"] = "sound does not match clearly"
        return out
    out["offset"] = round(snd - pic, 1)
    return out


if __name__ == "__main__":
    import json
    import sys
    print(json.dumps(offset(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "ffmpeg")))
