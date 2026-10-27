"""Backup stream suggestions: other feeds of a channel across every provider's catalog.

Ported from reference/dispatcharr_backup_streams.py (find_backups and its helpers) with the same matching
rules. Nothing here is specific to one provider or one channel:

* Provider prefixes such as "US|" and quality words such as HD, FHD, 4K and RAW are ignored, so
  "US| CNN FHD" matches "CNN HD".
* East and West/Pacific/Mountain/Central feeds are never mixed. A name without a region counts as East.
* A channel with a call sign in its name, such as "(WNBC)", only matches streams with that call sign.
* Strong evidence ("sure"): the same name, the same call sign, or the same provider guide ID (tvg-id).
  Similar names are offered as weaker suggestions.

Entry point: suggest(channel, catalogs).
"""
import difflib
import re
import unicodedata

MAX_SUGGESTIONS = 10

QUALITY = {"HD", "FHD", "UHD", "4K", "8K", "SD", "RAW", "HEVC", "H265", "H264", "HDR", "1080P", "720P",
           "2160P", "1080", "720", "60FPS", "50FPS", "30FPS", "FPS", "BACKUP", "ALT", "VIP", "LIVE",
           "FEED", "MULTI", "AUDIO", "SDR"}
REGIONS = {"EAST": "EAST", "EASTERN": "EAST", "WEST": "WEST", "WESTERN": "WEST", "PACIFIC": "WEST",
           "MOUNTAIN": "MOUNTAIN", "CENTRAL": "CENTRAL"}
# Words that don't change which channel it is ("FOX NEWS CHANNEL" = "FOX NEWS").
FILLER = {"CHANNEL", "NETWORK", "TV", "THE"}
PREFIX = re.compile(r"^(?:[^|]{0,20}\||\s*[A-Za-z]{2,4}(?:\.[A-Za-z])?\s*:)\s*")
PAREN_CODE = re.compile(r"\(([A-Z][A-Z0-9]{2,7})(?:-[A-Z0-9]{1,4})?\)")
SIMILAR = 0.82        # name similarity needed to offer a weaker candidate
SIMILAR_SHORT = 0.9   # stricter for short names (CNN vs CNNI)

# Scores, 0..1. Sure matches are always above every similar-name match.
SCORE_SAME_NAME = 1.0
SCORE_CALL_SIGN = 0.97
SCORE_SAME_TVG = 0.95
SCORE_TVG_OTHER_NUMBER = 0.6
SCORE_SIMILAR_MAX = 0.85
QUALITY_STEP = 0.005  # tiny penalty per quality rank so better feeds sort first within a reason


def clean(name):
    """Upper-case name without a provider prefix like "US|"; folds superscripts (ᴿᴬᵂ -> RAW)."""
    s = unicodedata.normalize("NFKC", name or "")
    return PREFIX.sub("", s).upper()


def describe(name):
    s = clean(name)
    code = None
    for m in PAREN_CODE.finditer(s):
        if m.group(1) not in QUALITY and m.group(1) not in REGIONS:
            code = m.group(1)
            break
    words = re.findall(r"[A-Z0-9]+", s.replace("&", " AND ").replace("+", " PLUS "))
    region, key = "EAST", []
    for w in words:
        if w in REGIONS:
            region = REGIONS[w]
        elif w not in QUALITY:
            key.append(w)
    core = [w for w in key if w not in FILLER] or key
    q = 3
    if "FHD" in words or "1080P" in words or "1080" in words:
        q = 0
    elif "HD" in words:
        q = 1
    elif "UHD" in words or "4K" in words:
        q = 2
    if "RAW" in words:
        q = 4
    if "SD" in words:
        q = 5
    compact = "".join(core)
    return dict(key=tuple(sorted(core)), compact=compact, region=region, code=code, q=q,
                words=set(words), nums=tuple(re.findall(r"\d+", compact)))


def tvg_key(t):
    """Provider guide ID without country tags and punctuation: "CNN.us", "US-CNN" -> "cnn"."""
    t = unicodedata.normalize("NFKC", t or "").strip().lower()
    t = re.sub(r"\.[a-z]{2,3}$", "", t)
    t = re.sub(r"^[a-z]{2,3}\s*[-|:]\s*", "", t)
    t = re.sub(r"[^a-z0-9]", "", t)
    return t if len(t) >= 3 else ""


def similarity(a, b):
    """Best of character similarity and word overlap between two described names."""
    if not a["compact"] or not b["compact"]:
        return 0.0
    sm = difflib.SequenceMatcher(None, a["compact"], b["compact"])
    r = sm.ratio() if sm.real_quick_ratio() >= SIMILAR and sm.quick_ratio() >= SIMILAR else 0.0
    ka, kb = set(a["key"]), set(b["key"])
    j = len(ka & kb) / len(ka | kb) if ka | kb else 0.0
    return max(r, j)


def find_backups(ref_names, ref_tvgs, current_ids, streams):
    """[(stream, reason, sure, best)] for other feeds of this channel, best first.

    ref_names:   the channel's name and the names of its current streams (channel name first).
    ref_tvgs:    normalized guide IDs of the channel and its current streams.
    current_ids: ids of streams already on the channel (skipped).
    streams:     dicts with "id", "name", "_d" (describe(name)) and "_tvg" (tvg_key(tvg_id)).
    best is the name similarity used for a similar-name match, else None.
    """
    refs = [describe(n) for n in ref_names if n]
    if not refs:
        return []
    region = refs[0]["region"]
    codes = {r["code"] for r in refs if r["code"]}
    nums = {r["nums"] for r in refs}
    out = []
    for st in streams:
        if st["id"] in current_ids:
            continue
        d = st["_d"]
        if d["region"] != region:          # never mix East and West feeds
            continue
        same_nums = d["nums"] in nums
        why, sure, best = None, False, None
        if codes:
            # channels with a call sign in their name only match that call sign
            hit = next((c for c in sorted(codes) if c in d["words"]), None)
            if hit:
                why, sure = f"same call sign {hit}", True
        elif d["key"] and any(d["key"] == r["key"] or d["compact"] == r["compact"] for r in refs):
            why, sure = "same name", True
        elif st["_tvg"] and st["_tvg"] in ref_tvgs:
            why, sure = ("same guide ID", True) if same_nums else ("same guide ID, different number", False)
        elif same_nums:
            best = max(similarity(r, d) for r in refs)
            need = SIMILAR_SHORT if min(len(d["compact"]), len(refs[0]["compact"])) < 6 else SIMILAR
            if best >= need:
                why = f"similar name ({int(best * 100)}%)"
            else:
                best = None
        if why:
            out.append((not sure, d["q"], st["name"], st, why, sure, best))
    out.sort(key=lambda x: x[:3])
    return [(st, why, sure, best) for _, _, _, st, why, sure, best in out]


def _score(why, best, q):
    if why == "same name":
        base = SCORE_SAME_NAME
    elif why.startswith("same call sign"):
        base = SCORE_CALL_SIGN
    elif why == "same guide ID":
        base = SCORE_SAME_TVG
    elif why.startswith("same guide ID"):
        base = SCORE_TVG_OTHER_NUMBER
    else:
        base = SCORE_SIMILAR_MAX * (best or 0.0)
    return round(max(0.0, min(1.0, base - QUALITY_STEP * q)), 3)


def suggest(channel, catalogs, limit=MAX_SUGGESTIONS):
    """Backup stream suggestions for one channel, best first.

    channel:  a coax channel dict (name, streams: [{provider, stream_id}], optional guide.xmltv_id).
    catalogs: {provider_id: catalog} as cached by app.providers.catalog
              ({"groups": [...], "streams": [{"stream_id", "name", "group", "tvg_id", ...}]}).
    Returns at most `limit` dicts {provider, stream_id, name, score, reason, sure}; streams already on the
    channel are left out.
    """
    channel = channel or {}
    streams, by_key = [], {}
    for provider_id, catalog in (catalogs or {}).items():
        for s in (catalog or {}).get("streams") or []:
            if not isinstance(s, dict) or s.get("stream_id") in (None, ""):
                continue
            name = s.get("name") or ""
            key = (str(provider_id), str(s["stream_id"]))
            st = {"id": key, "name": name, "provider": key[0], "stream_id": key[1],
                  "_d": describe(name), "_tvg": tvg_key(s.get("tvg_id"))}
            by_key[key] = st
            streams.append(st)

    current = set()
    for ref in channel.get("streams") or []:
        if isinstance(ref, dict) and ref.get("provider") is not None and ref.get("stream_id") is not None:
            current.add((str(ref["provider"]), str(ref["stream_id"])))

    ref_names = [channel.get("name") or ""] + [by_key[k]["name"] for k in sorted(current) if k in by_key]
    if not ref_names[0]:
        # No channel name: fall back to the first current stream's name as the main reference.
        ref_names = ref_names[1:]
    guide = channel.get("guide") if isinstance(channel.get("guide"), dict) else {}
    ref_tvgs = {t for t in [tvg_key(channel.get("tvg_id")), tvg_key(guide.get("xmltv_id"))]
                + [by_key[k]["_tvg"] for k in current if k in by_key] if t}

    found = find_backups(ref_names, ref_tvgs, current, streams)
    results = [{"provider": st["provider"], "stream_id": st["stream_id"], "name": st["name"],
                "score": _score(why, best, st["_d"]["q"]), "reason": why, "sure": sure}
               for st, why, sure, best in found]
    results.sort(key=lambda r: -r["score"])   # stable: keeps find_backups order on ties
    return results[:max(0, int(limit))]
