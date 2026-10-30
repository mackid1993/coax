"""Video on demand catalogs: a provider's movies and series, kept apart from its live channels.

Xtream panels list them through their own API calls (get_vod_streams, get_series, one get_series_info per series
for its episodes). In an M3U playlist they sit among the live channels and are told apart by their address: Xtream
style /movie/ and /series/ paths, or a video file (.mkv, .mp4, ...) where a live channel is a stream (.ts, .m3u8).

Cache files:
  {data_dir}/cache/vod-<provider_id>.json
    {"fetched_at", "movie_groups": [...], "series_groups": [...],
     "movies": [{"id", "name", "ext", "groups", "year", "tmdb", "logo", "added", "url"?}],
     "series": [{"id", "name", "groups", "year", "tmdb", "logo", "modified"}],
     "episodes": {series id: [...]}  (M3U only: its episodes come with the playlist)}
  {data_dir}/cache/vod-<provider_id>-episodes.json
    {series id: {"modified": int, "episodes": [{"id", "season", "episode", "title", "ext"}]}}  (Xtream)
"""

import logging
import os
import re
import threading
import time
from urllib.parse import urlsplit

from . import catalog, xtream
from .net import ProviderError

log = logging.getLogger("coax.providers")

EPISODE_PAUSE = 0.25        # seconds between get_series_info calls
EPISODE_FAILURES = 5        # failed calls in a row that end a run (a throttling panel); the next sync carries on

_locks_guard = threading.Lock()
_locks = {}

# A video file, not a live stream.
VIDEO_EXT = ("mkv", "mp4", "m4v", "avi", "mov", "wmv", "mpg", "mpeg", "webm", "flv", "divx", "xvid", "3gp", "ogv")
_XTREAM_VOD = re.compile(r"/(movie|movies|series)/[^/]+/[^/]+/([0-9]+)\.([A-Za-z0-9]{2,5})$", re.IGNORECASE)
_EPISODE = re.compile(r"^(?P<show>.*?)[\s._-]*\bS(?P<s>\d{1,3})\s*E(?P<e>\d{1,4})\b(?P<rest>.*)$", re.IGNORECASE)
_EPISODE_X = re.compile(r"^(?P<show>.*?)[\s._-]+(?P<s>\d{1,2})x(?P<e>\d{1,3})\b(?P<rest>.*)$", re.IGNORECASE)

# Tags providers put in front of a title: "EN - ", "SE-4K - ", "4K: ", "4KDV: ", "|EN| ", "[EN] ", "EN| ", "NF - ".
_LEAD_TAG = re.compile(
    r"^\s*(?:"
    r"[|│｜]\s*[A-Za-z0-9+ ]{1,10}\s*[|│｜]"            # |EN|
    r"|\[[A-Za-z0-9+ ]{1,10}\]"                                          # [EN]
    r"|[A-Z]{2,4}(?:[-+][A-Z0-9]{1,5})?\s*[|│｜]"               # EN|  SE-4K|
    r"|[A-Z]{2,4}(?:[-+][A-Z0-9]{1,5})?\s+[-–]\s"                  # EN -  SE-4K -  NF -
    r"|(?:4K|UHD|FHD|HD|SD|HDR|DV|4KDV|4K-DV|HEVC|3D|MULTI|VOST|SUB|DUB)\s*(?::|-|–)\s"   # 4K: / 4K -
    r")\s*")
# Tags providers put after it: "(US)", "[MULTI]", "(4K)", "- 4K".
_TRAIL_TAG = re.compile(
    r"\s*(?:\((?:[A-Z]{2,3}|4K|UHD|FHD|HD|SD|HDR|DV|MULTI|SUB|DUB|VOST|VOSTFR)\)|\[[^\]]{1,12}\]"
    r"|[-–]\s*(?:4K|UHD|FHD|HD|SD|HDR|MULTI)\b)\s*$")
_YEAR = re.compile(r"\s*[(\[]\s*((?:19|20)\d\d)\s*[)\]]")
_BARE_YEAR = re.compile(r"\s+[-–]?\s*((?:19|20)\d\d)\s*$")


def cache_path(provider_id, suffix=""):
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(provider_id)) or "_"
    return os.path.join(catalog.data_dir(), "cache", "vod-%s%s.json" % (safe, suffix))


def _lock_for(provider_id):
    with _locks_guard:
        return _locks.setdefault(str(provider_id), threading.Lock())


# -- names ----------------------------------------------------------------------------------------------------------

def title_year(name, year=""):
    """'EN - The King's Speech (2010)' -> ("The King's Speech", "2010"). Lead and trail tags go, the year moves
    out of the title; `year` is the panel's own field, used when the name has none."""
    text = " ".join((name or "").split())
    for _ in range(3):
        stripped = _LEAD_TAG.sub("", text, count=1).strip()
        if not stripped or stripped == text:
            break
        text = stripped
    for _ in range(3):
        stripped = _TRAIL_TAG.sub("", text).strip()
        if not stripped or stripped == text:
            break
        text = stripped
    found = ""
    match = _YEAR.search(text)
    if match:
        found = match.group(1)
        text = (text[:match.start()] + text[match.end():]).strip()
    else:
        match = _BARE_YEAR.search(text)
        if match and len(text[:match.start()].strip()) >= 2 and int(match.group(1)) <= time.gmtime().tm_year + 1:
            found = match.group(1)
            text = text[:match.start()].strip()
    for _ in range(3):
        stripped = _TRAIL_TAG.sub("", text).strip(" -–")
        if not stripped or stripped == text:
            break
        text = stripped
    year = found or (year if re.fullmatch(r"(?:19|20)\d\d", year or "") else "")
    return (text or " ".join((name or "").split()) or "Untitled"), year


def display_name(name, year=""):
    title, year = title_year(name, year)
    return "%s (%s)" % (title, year) if year else title


# -- M3U ------------------------------------------------------------------------------------------------------------

def m3u_kind(url):
    """'movie', 'episode' or None (a live stream) for an M3U entry's address."""
    path = urlsplit((url or "").strip()).path
    match = _XTREAM_VOD.search(path)
    if match:
        return "episode" if match.group(1).lower() == "series" else "movie"
    ext = path.rsplit(".", 1)[-1].lower() if "." in path.rsplit("/", 1)[-1] else ""
    if ext in VIDEO_EXT:
        return "movie"   # m3u_split decides between movie and episode by the name
    return None


def _ext_of(url):
    path = urlsplit(url).path
    last = path.rsplit("/", 1)[-1]
    ext = last.rsplit(".", 1)[-1].lower() if "." in last else ""
    return ext if re.fullmatch(r"[a-z0-9]{2,5}", ext) else "mp4"


def m3u_split(entries, id_for):
    """Split parsed M3U entries into (live entries, VOD catalog). `id_for(entry)` gives a stable id."""
    live = []
    movies, movie_groups = [], []
    shows, series_groups, episodes = {}, [], {}
    seen = set()
    for entry in entries:
        url = (entry.get("url") or "").strip()
        kind = m3u_kind(url)
        if kind is None:
            live.append(entry)
            continue
        sid = id_for(entry)
        if not sid or sid in seen:
            continue
        seen.add(sid)
        group = entry.get("group") or "Uncategorized"
        name = entry.get("name") or ""
        match = _EPISODE.match(name) or _EPISODE_X.match(name)
        if match and match.group("show").strip(" -._"):
            show_name = match.group("show").strip(" -._")
            key = re.sub(r"\W+", " ", title_year(show_name)[0].lower()).strip()
            show = shows.get(key)
            if show is None:
                show = shows[key] = {"id": "s" + catalog_digest(key), "name": show_name, "groups": [],
                                     "year": "", "tmdb": "", "logo": entry.get("logo") or "", "modified": 0}
            if group not in show["groups"]:
                show["groups"].append(group)
            if group not in series_groups:
                series_groups.append(group)
            episodes.setdefault(show["id"], []).append({
                "id": sid, "season": int(match.group("s")), "episode": int(match.group("e")),
                "title": match.group("rest").strip(" -._"), "ext": _ext_of(url), "url": url})
        else:
            if group not in movie_groups:
                movie_groups.append(group)
            movies.append({"id": sid, "name": name, "ext": _ext_of(url), "groups": [group], "year": "",
                           "tmdb": "", "logo": entry.get("logo") or "", "added": 0, "url": url})
    vod = {"movie_groups": movie_groups, "series_groups": series_groups, "movies": movies,
           "series": list(shows.values()), "episodes": episodes}
    return live, vod


def catalog_digest(text):
    import hashlib
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


# -- cache ----------------------------------------------------------------------------------------------------------

def _load_json(path):
    """Parse a cache file. Never kept in memory: a large provider's VOD list is about 200 MB once parsed
    (137,000 movies), so it is read when a sync or a play needs it and let go after."""
    import json
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        log.warning("cannot read %s: %s", os.path.basename(path), exc)
        return None


def load(provider_id):
    """The provider's cached VOD catalog, or None."""
    data = _load_json(cache_path(provider_id))
    return data if isinstance(data, dict) else None


def summary(provider_id):
    """The small file beside the catalog: {"fetched_at", "movies": [{"name", "count"}], "series": [...]}."""
    data = _load_json(cache_path(provider_id, "-groups"))
    if isinstance(data, dict):
        return data
    vod = load(provider_id)   # a catalog from before the summary existed
    if vod is None:
        return None
    data = dict(group_counts(vod), fetched_at=vod.get("fetched_at"))
    catalog.atomic_write_json(cache_path(provider_id, "-groups"), data)
    return data


def save(provider_id, vod):
    """Write a VOD catalog and its summary, unless it is empty and the cached one is not (a panel hiccup)."""
    if not vod.get("movies") and not vod.get("series"):
        previous = summary(provider_id) or {}
        if any(g.get("count") for g in (previous.get("movies") or []) + (previous.get("series") or [])):
            raise ProviderError("provider returned no movies or series; keeping the previous list")
    data = {"fetched_at": catalog.now_iso()}
    data.update(vod)
    catalog.atomic_write_json(cache_path(provider_id), data)
    catalog.atomic_write_json(cache_path(provider_id, "-groups"),
                              dict(group_counts(data), fetched_at=data["fetched_at"]))
    return data


def refresh(provider):
    """Download an Xtream provider's VOD lists and cache them. M3U providers get theirs with the playlist."""
    pid = provider.get("id")
    if (provider.get("type") or "xtream").lower() != "xtream":
        return load(pid)
    with _lock_for(pid):
        started = time.monotonic()
        movie_names = {c["category_id"]: c["category_name"] for c in xtream.get_vod_categories(provider)}
        series_names = {c["category_id"]: c["category_name"] for c in xtream.get_series_categories(provider)}
        movies = xtream.get_vod_streams(provider)
        series = xtream.get_series(provider)

        def named(items, names, order):
            for item in items:
                item["groups"] = [names.get(cid) or xtream.UNCATEGORIZED for cid in item.pop("categories")] \
                    or [xtream.UNCATEGORIZED]
                for group in item["groups"]:
                    if group not in order:
                        order.append(group)
            return items
        movie_groups = list(dict.fromkeys(movie_names.values()))
        series_groups = list(dict.fromkeys(series_names.values()))
        data = save(pid, {"movie_groups": movie_groups, "series_groups": series_groups,
                          "movies": named(movies, movie_names, movie_groups),
                          "series": named(series, series_names, series_groups)})
    log.info("provider %s VOD refreshed in %.0fs: %d movies in %d groups, %d series in %d groups", pid,
             time.monotonic() - started, len(data["movies"]), len(movie_groups), len(data["series"]), len(series_groups))
    return data


def group_counts(vod):
    """{"movies": [{"name", "count"}], "series": [...]} in the provider's order."""
    out = {}
    for kind, items_key, groups_key in (("movies", "movies", "movie_groups"), ("series", "series", "series_groups")):
        counts = {}
        for item in (vod or {}).get(items_key) or []:
            for group in item.get("groups") or []:
                counts[group] = counts.get(group, 0) + 1
        names = list((vod or {}).get(groups_key) or [])
        names += [g for g in counts if g not in names]
        out[kind] = [{"name": g, "count": counts.get(g, 0)} for g in names]
    return out


def episodes_for(provider, series, deadline=None, progress=None, fetch=True, max_fetch=None, info=None):
    """Episodes of these series ({series id: [episodes]}), from the cache where the panel's last_modified has not
    moved, else asked for one series at a time (Xtream), in the order given. An M3U provider's come with its catalog.

    fetch=False answers from the cache only; max_fetch stops after that many calls. `info`, when given, is filled
    with {"fetched", "left" (series still to ask for), "throttled" (stopped after EPISODE_FAILURES in a row)}."""
    pid = provider.get("id")
    if info is not None:
        info.update(fetched=0, left=0, throttled=False)
    if (provider.get("type") or "xtream").lower() != "xtream":
        stored = (load(pid) or {}).get("episodes") or {}
        return {s["id"]: stored.get(s["id"]) or [] for s in series}
    path = cache_path(pid, "-episodes")
    cache = dict(_load_json(path) or {})
    out, fetched, failed, in_row, left = {}, 0, 0, 0, 0
    for index, show in enumerate(series):
        hit = cache.get(show["id"])
        if isinstance(hit, dict) and hit.get("modified") == show.get("modified") and show.get("modified"):
            out[show["id"]] = hit.get("episodes") or []
            continue
        if not fetch or (deadline is not None and time.monotonic() > deadline) or in_row >= EPISODE_FAILURES \
                or (max_fetch is not None and fetched + failed >= max_fetch):
            out[show["id"]] = (hit or {}).get("episodes") or []   # the next run carries on
            left += 1
            continue
        if fetched or failed:
            time.sleep(EPISODE_PAUSE)   # one account, possibly thousands of calls: never a burst
        try:
            eps = xtream.get_series_episodes(provider, show["id"])
        except ProviderError as exc:
            failed += 1
            in_row += 1
            log.warning("provider %s: episodes of series %s not read: %s", pid, show["id"], exc)
            if in_row == EPISODE_FAILURES:
                log.warning("provider %s: %d episode lists failed in a row; the rest wait for the next sync",
                            pid, in_row)
            out[show["id"]] = (hit or {}).get("episodes") or []
            left += 1
            continue
        in_row = 0
        cache[show["id"]] = {"modified": show.get("modified") or 0, "episodes": eps}
        out[show["id"]] = eps
        fetched += 1
        if progress is not None:
            progress(index + 1, len(series))
        if fetched % 100 == 0:
            catalog.atomic_write_json(path, cache)
    if fetched:
        keep = {s["id"] for s in series}
        catalog.atomic_write_json(path, {k: v for k, v in cache.items() if k in keep})
    if fetched or failed:
        log.info("provider %s: episodes read for %d series (%d failed, %d still to read)", pid, fetched, failed, left)
    if info is not None:
        info.update(fetched=fetched, left=left, throttled=in_row >= EPISODE_FAILURES)
    return out


def find(provider_id, kind, item_id):
    """The cached movie or episode with this id (M3U addresses live there), or None."""
    vod = load(provider_id) or {}
    item_id = str(item_id)
    if kind == "movie":
        return next((m for m in vod.get("movies") or [] if str(m.get("id")) == item_id), None)
    for eps in (vod.get("episodes") or {}).values():
        for ep in eps:
            if str(ep.get("id")) == item_id:
                return ep
    return None
