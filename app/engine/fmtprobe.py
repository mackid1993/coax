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
        if a.get("sample_rate"):
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


AC3CH = (2, 1, 2, 3, 3, 4, 4, 5)


def _frame_ch(b):
    """Channels of the first Dolby (AC-3 / E-AC-3) or ADTS AAC frame in b, or None."""
    for i in range(len(b) - 7):
        if b[i] == 0x0B and b[i + 1] == 0x77:
            bsid = b[i + 5] >> 3
            if bsid <= 10:                                   # AC-3: acmod after fscod/frmsizecod/bsid/bsmod
                acmod = b[i + 6] >> 5
                bit = 3
                if acmod & 1 and acmod != 1:
                    bit += 2
                if acmod & 4:
                    bit += 2
                if acmod == 2:
                    bit += 2
                lfe = (int.from_bytes(b[i + 6:i + 8], "big") >> (15 - bit)) & 1
                return AC3CH[acmod] + lfe
            if 11 <= bsid <= 16:                             # E-AC-3: acmod and lfeon right after fscod fields
                acmod = (b[i + 4] >> 1) & 7
                return AC3CH[acmod] + (b[i + 4] & 1)
        if b[i] == 0xFF and b[i + 1] & 0xF6 == 0xF0:         # ADTS AAC
            cc = (b[i + 2] & 1) << 2 | b[i + 3] >> 6
            if 1 <= cc <= 7:
                return (0, 1, 2, 3, 4, 5, 6, 8)[cc]
    return None



DOLBY_TYPES = (0x81, 0x87)        # ATSC AC-3 / E-AC-3 stream types
AUDIO_TYPES = (0x03, 0x04, 0x0F, 0x11) + DOLBY_TYPES   # MPEG audio, AAC (ADTS, LATM), Dolby


def tracks(d):
    """{audio PID: [dolby, channels]} for the audio tracks the program table (PMT) of this MPEG-TS lists, in its
    order. channels comes from the first frame of each track found here (0 when none was). None when no PMT was
    found. Read from the packets themselves (no ffprobe)."""
    pmt, aud, buf = None, None, {}
    for i in range(0, min(len(d), 188 * 4000) - 187, 188):
        p = d[i:i + 188]
        if p[0] != 71:
            continue
        pid = (p[1] & 31) << 8 | p[2]
        af = p[3] >> 4 & 3
        if af == 2:
            continue
        o = 5 + p[4] if af == 3 else 4
        if o >= 188:
            continue
        if pid == 0 and p[1] & 64 and pmt is None:
            o += 1 + p[o]
            if o + 12 <= 188:
                for j in range(o + 8, min(o + 3 + ((p[o + 1] & 15) << 8 | p[o + 2]) - 4, 188) - 3, 4):
                    if p[j] << 8 | p[j + 1]:
                        pmt = (p[j + 2] & 31) << 8 | p[j + 3]
                        break
        elif pid == pmt and p[1] & 64 and aud is None:
            o += 1 + p[o]
            if o + 12 > 188 or p[o] != 2:
                continue
            end = min(o + 3 + ((p[o + 1] & 15) << 8 | p[o + 2]) - 4, 188)
            j = o + 12 + ((p[o + 10] & 15) << 8 | p[o + 11])
            aud = {}
            while j + 5 <= end:
                st, ep, il = p[j], (p[j + 1] & 31) << 8 | p[j + 2], (p[j + 3] & 15) << 8 | p[j + 4]
                desc = bytes(p[j + 5:j + 5 + il])
                # DVB carries Dolby as private data (type 6) with an AC-3 / E-AC-3 descriptor or registration.
                dvb = st == 6 and (desc[:1] in (b"\x6a", b"\x7a") or b"AC-3" in desc or b"EAC3" in desc)
                if st in AUDIO_TYPES or dvb:
                    aud[ep] = [st in DOLBY_TYPES or dvb, 0]
                j += 5 + il
        elif aud and pid in aud and not aud[pid][1]:
            if p[1] & 64:
                pl = p[o:]
                if pl[:3] == b"\x00\x00\x01" and len(pl) > 9:
                    buf[pid] = bytearray(pl[9 + pl[8]:])
            elif pid in buf:
                buf[pid] += p[o:]
            if pid in buf and len(buf[pid]) >= 16:
                c = _frame_ch(bytes(buf[pid]))
                if c:
                    aud[pid][1] = c
                elif len(buf[pid]) > 4000:
                    del buf[pid]
            if all(v[1] for v in aud.values()):
                break
    return aud


def pick(t):
    """The best track of tracks() output: the most channels, then Dolby (so a Dolby 5.1 track that is stereo during
    an ad still wins over a stereo AAC track), then the first listed. None when there is none."""
    if not t:
        return None
    order = list(t)
    return max(order, key=lambda pid: (t[pid][1], t[pid][0], -order.index(pid)))


def best_audio(d):
    """PID of the best audio track on this feed (see pick), or None when unknown."""
    return pick(tracks(d))
