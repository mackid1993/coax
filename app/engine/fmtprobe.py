"""What the provider sends: codecs, resolution, frame rate and bitrates of one segment, in plain words."""
import json
import subprocess

VCODEC = {"h264": "H.264", "hevc": "HEVC", "mpeg2video": "MPEG-2", "av1": "AV1"}
ACODEC = {"ac3": "Dolby Digital", "eac3": "Dolby Digital Plus", "mp2": "MP2", "mp3": "MP3", "opus": "Opus"}
AAC = {"HE-AAC": "HE-AAC", "HE-AACv2": "HE-AAC v2", "LC": "AAC-LC"}
LAYOUT = {1: "mono", 2: "stereo", 6: "5.1", 8: "7.1"}


def _rate(s):
    try:
        n, d = s.split("/")
        return float(n) / float(d) if float(d) else 0.0
    except (ValueError, AttributeError):
        return 0.0


def summarize(streams, nbytes, dur):
    """{"video": "...", "audio": "..."} from ffprobe streams, the segment size and its duration in seconds."""
    v = next((x for x in streams if x.get("codec_type") == "video"), None)
    a = next((x for x in streams if x.get("codec_type") == "audio"), None)
    total = nbytes * 8 / dur if dur and dur > 0 else 0
    abr = int(a.get("bit_rate") or 0) if a else 0
    out = {}
    if v:
        p = [VCODEC.get(v.get("codec_name"), str(v.get("codec_name") or "?").upper())]
        if v.get("width") and v.get("height"):
            p.append("%dx%d" % (v["width"], v["height"]))
        fps = _rate(v.get("avg_frame_rate")) or _rate(v.get("r_frame_rate"))
        if 0 < fps < 200:
            p.append(("%.2f" % fps).rstrip("0").rstrip(".") + " fps")
        if total > abr:
            p.append("%.1f Mbps" % ((total - abr) / 1e6))
        out["video"] = " · ".join(p)
    if a:
        c = a.get("codec_name")
        p = [AAC.get(a.get("profile"), "AAC") if c == "aac" else ACODEC.get(c, str(c or "?").upper())]
        if int(a.get("sample_rate") or 0) > 0 and a.get("channels"):   # a short probe can report 0 for both
            p.append(("%.1f" % (int(a["sample_rate"]) / 1000)).rstrip("0").rstrip(".") + " kHz " +
                     LAYOUT.get(a.get("channels"), "%s ch" % a.get("channels")))
        if abr:
            p.append("%d kbps" % round(abr / 1000))
        out["audio"] = " · ".join(p)
    return out


def describe(ff, data, dur):
    probe = ff[:-6] + "ffprobe" if ff.endswith("ffmpeg") else "ffprobe"
    r = subprocess.run([probe, "-v", "quiet", "-show_streams", "-of", "json", "-i", "pipe:0"], input=data,
                       capture_output=True, timeout=30)
    return summarize(json.loads(r.stdout or b"{}").get("streams") or [], len(data), dur)
