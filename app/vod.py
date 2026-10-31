"""Video on demand in Channels DVR: the provider's movies and series as library items, created through the DVR's API.

Channels DVR plays a ".strm" file (a text file holding one video address) as a regular library item: listed with the
user's movies and shows, searchable, with watch state and resume. Its folder scanner imports such files slowly: it
looks every title up (Channels' ContentDB and TMDB, one title after another) and reads through the whole folder for
each file, about two seconds a movie, days for a large provider. coax does not wait for it. It writes the files into
a hidden folder inside the DVR's Imports folder (mounted at /imports), which the scanner skips (it never enters a
folder whose name starts with a dot):

  Movies/.coax/<provider id>/<Title (Year)>/<Title (Year)>.strm
  TV/.coax/<provider id>/<Show (Year)>/Season 01/<Show (Year)> - S01E02.strm

(one folder per movie: on every item it adds, the DVR reads through the item's whole folder looking for extras, and a
folder of 100,000 movies would make each one take a second), and creates each item itself with the DVR's own restore call (POST /dvr/files, the one its backups use), carrying the
provider's title, year, artwork, plot and TMDB id. That takes milliseconds an item and needs no lookup, so a title is
in the library as soon as the provider lists it. A show is a DVR group (id coax-<provider>-<series id>) that its first
episode creates; coax names it and gives it its artwork. The files stay on disk because the DVR plays and keeps an item
only while its file exists.

Then, in the background, coax fills in what the provider did not have: each movie and show is matched on TMDB through
the DVR (PUT /dvr/files/<id>/matches/tmdb/movies/<tmdb id>, PUT /dvr/groups/<id>/matches/tmdb/<tmdb id>), by the
provider's TMDB id when it has one, else by a search for the title and year. The DVR then fetches the full metadata
(poster, plot, cast, runtime, rating; every episode's title, plot and runtime). Several run at once; the scanner could
only do one.

Each file holds coax's own address (<Base URL>/vod/<provider>/<m|e>/<id>.<ext>), never the provider's: the login
stays out of the DVR, and every play goes through coax, which holds a line slot for it (app/vodproxy.py).

coax keeps one DVR collection per picked group and one Personal Section per provider showing them. The titles are
always listed in the main Movies and TV Shows too: Channels DVR's search only finds titles there, and a large
provider is unusable without search. Titles an earlier version set to Hidden are shown again on the next sync. Removed titles are deleted from the DVR one by one, never with
a DVR-wide prune. Nothing outside Movies/.coax and TV/.coax is written or removed, except the scanner-imported files
of the earlier layout (Movies/coax, TV/coax), which are taken out of the DVR once.
"""

import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger("coax.vod")

IMPORTS_DIR = "/imports"
SUBDIR = ".coax"             # hidden: Channels DVR's scanner never enters a folder whose name starts with a dot
OLD_SUBDIR = "coax"          # the earlier, scanned layout
TICK_SECONDS = 60
EPISODE_BUDGET = 1800        # seconds of get_series_info calls per sync; the next sync carries on
BATCH_SERIES = 300           # episode lists read before the batch goes to Channels DVR...
BATCH_SECONDS = 600          # ...or after this long, whichever comes first
CONTINUE_AFTER = 900         # a sync that ran out of time carries on this soon, not at the next refresh
THROTTLE_WAIT = 3600         # after EPISODE_FAILURES failed calls in a row, the panel rests this long
CREATE_WORKERS = 6           # items created in the DVR at once
MATCH_WORKERS = 4            # TMDB matches through the DVR at once (each is about a second of the DVR's lookups)
MATCH_RETRY = 600            # a match that failed on an error (not "no match") is tried again after this long
SAVE_EVERY = 30.0            # seconds between state saves during a long run
COLLECTIONS_EVERY = 60.0     # seconds between collection updates while titles are being added
SECTION_ICON = "film"
POSTER = "https://image.tmdb.org/t/p/w780%s"
FANART = "https://image.tmdb.org/t/p/original%s"
_BAD_CHARS = re.compile(r'[\\/*?"<>|\x00-\x1f]')
_GROUP_ID = re.compile(r"[^A-Za-z0-9_-]")
_EP_TAG = re.compile(r"^.*?\bS\d{1,3}\s*E\d{1,4}\b\s*[-–:.]*\s*", re.IGNORECASE)


def imports_ready(path=None):
    """(True, "") when the Channels DVR Imports folder is mounted, else (False, why)."""
    path = path or IMPORTS_DIR
    if not os.path.isdir(path):
        return False, ("Channels DVR's Imports folder is not connected to coax. Mount it at /imports "
                       "(CHANNELS_DVR_IMPORTS in the compose .env), then recreate the container.")
    if not (os.path.isdir(os.path.join(path, "Movies")) or os.path.isdir(os.path.join(path, "TV"))):
        return False, ("The folder mounted at /imports is not Channels DVR's Imports folder (it has no Movies or TV "
                       "folder). Point CHANNELS_DVR_IMPORTS at the Imports folder inside the DVR's storage folder.")
    for kind in ("Movies", "TV"):
        mine = os.path.join(path, kind, SUBDIR)
        folder = mine if os.path.isdir(mine) else os.path.join(path, kind)
        if os.path.isdir(folder) and not os.access(folder, os.W_OK | os.X_OK):
            return False, ("coax cannot write to %s in the Channels DVR Imports folder (uid 1000 needs write access)."
                           % folder.replace(path, "Imports", 1))
    return True, ""


def _makedirs(path):
    """Create a folder and its missing parents open to everyone (777). coax never changes an owner: the folders and
    files it makes must stay usable by whatever user Channels DVR and the host's tools run as."""
    missing = []
    while path and not os.path.isdir(path):
        missing.append(path)
        path = os.path.dirname(path)
    for folder in reversed(missing):
        try:
            os.mkdir(folder)
        except FileExistsError:
            continue
        try:
            os.chmod(folder, 0o777)
        except OSError:
            pass


def safe_name(text, limit=150):
    """A file or folder name every file system takes: no path or reserved characters, ':' as ' -'."""
    text = _BAD_CHARS.sub("", (text or "").replace(":", " -"))
    text = " ".join(text.split()).strip(" .")
    while len(text.encode("utf-8")) > limit:
        text = text[:-1]
    return text.strip(" .") or "Untitled"


def group_id(pid, series_id):
    """The DVR group id of one of coax's shows."""
    return _GROUP_ID.sub("_", "coax-%s-%s" % (pid, series_id))


def vod_hours(provider):
    """Hours between VOD syncs: the provider's refresh schedule, at least 12 (the movie and series lists are tens of
    megabytes), None for a provider refreshed by hand (refresh_hours 0)."""
    try:
        hours = float(provider.get("refresh_hours", 24))
    except (TypeError, ValueError):
        hours = 24.0
    if hours == 0:
        return None
    return max(hours, 12.0) if hours == hours and hours > 0 else 24.0


def _vod_of(provider):
    return provider.get("vod") if isinstance(provider.get("vod"), dict) else {}


def selected(provider):
    """The provider's AVAILABLE groups (the Providers page): (movie groups, series groups). Their titles show in the
    On demand catalog; only Add all groups and hand-picked titles go to Channels DVR."""
    vod = _vod_of(provider)
    return list(vod.get("movies") or []), list(vod.get("series") or [])


def add_all(provider):
    """Groups whose every title goes to Channels DVR, now and later: (movie groups, series groups), each a subset of
    the available groups. A config without `vod.all` (written before the catalog existed, when every picked group
    went to Channels DVR whole) counts every available group as Add all, so an upgrade changes nothing."""
    movies, series = selected(provider)
    chosen = _vod_of(provider).get("all")
    if not isinstance(chosen, dict):
        return movies, series
    return ([g for g in chosen.get("movies") or [] if g in movies],
            [g for g in chosen.get("series") or [] if g in series])


def picks(provider):
    """Hand-picked titles: (movie ids, series ids) as strings."""
    chosen = _vod_of(provider).get("picks")
    chosen = chosen if isinstance(chosen, dict) else {}
    return ({str(i) for i in chosen.get("movies") or []}, {str(i) for i in chosen.get("series") or []})


def materialize(vod):
    """The same choices with `all` and `picks` written out (an older config's implied Add all made explicit), so a
    change to one of them never changes what the others mean."""
    vod = dict(vod or {})
    if not isinstance(vod.get("all"), dict):
        vod["all"] = {"movies": list(vod.get("movies") or []), "series": list(vod.get("series") or [])}
    if not isinstance(vod.get("picks"), dict):
        vod["picks"] = {"movies": [], "series": []}
    vod.pop("library", None)   # the old per-group "show in Movies" choice: every title is listed now
    return vod


def in_library(provider):
    """Groups whose titles show in the main Movies and TV Shows listings: every one. Channels DVR's search only
    finds titles listed there, so hiding them made a large provider unsearchable."""
    movies, series = selected(provider)
    return set(movies), set(series)


_DV = re.compile(r"\b(dolby\s*vision|dovi|dv)\b", re.IGNORECASE)
_UHD = re.compile(r"\b(4k|uhd|2160p?)\b", re.IGNORECASE)


def quality(name, groups=()):
    """'DV', '4K' or 'HD' for one copy of a title, from the provider's own labels (its name and its groups). A copy
    with no label counts as HD: that is all a provider's list tells without opening the file."""
    text = " ".join([name or ""] + [g or "" for g in groups or ()])
    if _DV.search(text):
        return "DV"
    if _UHD.search(text):
        return "4K"
    return "HD"


def _copy_name(name, groups):
    """A second copy's folder and file name: the title with its group, so both copies live side by side."""
    from app.names import clean_name
    tag = clean_name(groups[0]) if groups else "copy"
    return safe_name("%s - %s" % (name, tag))


def group_label(provider, group):
    from app.names import clean_name
    return "%s · %s" % (provider.get("name") or provider.get("id"), clean_name(group))


def _genres(text):
    return [g.strip() for g in re.split(r"[,/|]", text or "") if g.strip()][:5]


def _year(text):
    match = re.search(r"(?:19|20)\d\d", str(text or ""))
    return int(match.group(0)) if match else 0


def episode_title(title, season, number):
    """The episode's own name from a panel title like 'Show - S01E02 - Name' ('' when it is only the show)."""
    text = (title or "").strip()
    stripped = _EP_TAG.sub("", text, count=1).strip(" -–:.")
    if stripped != text:
        return stripped
    return "" if re.search(r"\bS\d{1,3}\s*E\d{1,4}\b", text, re.IGNORECASE) else text


class Plan:
    """What the provider's picks should look like in the DVR."""

    def __init__(self):
        self.files = {}      # relative path under /imports -> file content
        self.meta = {}       # relative path -> what the DVR item carries
        self.shows = {}      # group id -> the show's details
        self.members = {}    # (kind, group) -> movie paths, or show group ids
        self.titles = {}     # (kind, item id) -> name for the log and the Status page
        self.visible = set()         # paths shown in the main listings
        self.visible_shows = set()   # group ids shown in the main listings
        self.picked = set()          # paths and show group ids that are hand-picks (they go to Channels DVR first)
        self.present = {}            # "m:<movie id>" -> path, "s:<series id>" -> show group id
        self.picked_keys = set()     # "m:<id>" / "s:<id>" of the hand-picks the plan carries itself
        self.folders = {}            # show group id -> "TV/.coax/<pid>/<folder>/" (its episodes' path prefix)


def plan(provider, vod, episodes, base):
    """Every file and item the provider's Add all groups and hand-picks should have in Channels DVR.

    Add all groups work as they always have: every title, one copy of a title that two of them list (filed in both
    collections). A hand-pick is one exact copy (a movie or a series id) from an available group; a pick that an Add
    all group already carries is that group's. A pick of another copy of a title that is already there is a second
    copy beside the first (the user picked it on purpose), in a folder named with its group. A pick goes into the
    collection of each available group that lists it."""
    from app.providers import vod as vodcat
    pid = provider["id"]
    out = Plan()
    avail_m, avail_s = (set(g) for g in selected(provider))
    all_m, all_s = add_all(provider)
    pick_m, pick_s = picks(provider)
    out.members = {("movies", g): [] for g in all_m}
    out.members.update({("series", g): [] for g in all_s})
    taken = {}   # lower-case path -> the path of the copy that stands for the title

    def add_movie(movie, groups, picked):
        title, year = vodcat.title_year(movie.get("name"), movie.get("year"))
        name = safe_name("%s (%s)" % (title, year) if year else title)
        rel = "Movies/%s/%s/%s/%s.strm" % (SUBDIR, pid, name, name)   # a folder each: the DVR reads a file's folder
        if rel.lower() in taken:
            if not picked:
                rel = taken[rel.lower()]   # Add all: the same title twice, the first copy stands for both
            else:
                base_name, n = _copy_name(name, groups), 1
                copy = base_name
                while True:
                    rel = "Movies/%s/%s/%s/%s.strm" % (SUBDIR, pid, copy, copy)
                    if rel.lower() not in taken:
                        break
                    n += 1
                    copy = safe_name("%s %d" % (base_name, n))
        if rel.lower() not in taken:
            taken[rel.lower()] = rel
            out.files[rel] = "%s/vod/%s/m/%s.%s\n" % (base, pid, movie["id"], movie.get("ext") or "mp4")
            out.meta[rel] = {"kind": "movie", "title": title, "year": year, "tmdb": movie.get("tmdb") or "",
                             "image": movie.get("logo") or "", "plot": movie.get("plot") or "",
                             "genres": _genres(movie.get("genre")), "added": movie.get("added") or 0}
            out.titles[("movie", str(movie["id"]))] = "%s (%s)" % (title, year) if year else title
            out.visible.add(rel)
            if picked:
                out.picked.add(rel)
                out.picked_keys.add("m:%s" % movie["id"])
        out.present["m:%s" % movie["id"]] = rel
        for group in groups:
            members = out.members.setdefault(("movies", group), [])
            if rel not in members:
                members.append(rel)

    movies = vod.get("movies") or []
    for movie in movies:
        groups = [g for g in movie.get("groups") or [] if g in all_m]
        if groups:
            add_movie(movie, groups, False)
    for movie in movies:
        mid = str(movie.get("id"))
        if mid in pick_m and "m:" + mid not in out.present:
            groups = [g for g in movie.get("groups") or [] if g in avail_m]
            if groups:
                add_movie(movie, groups, True)

    show_dirs = {}

    def add_show(show, groups, picked):
        title, year = vodcat.title_year(show.get("name"), show.get("year") or str(_year(show.get("release")) or ""))
        show_name = "%s (%s)" % (title, year) if year else title
        folder = safe_name(show_name)
        if folder.lower() in show_dirs and show_dirs[folder.lower()] != show["id"]:
            if not picked:
                # Add all: two series with one name, keep the first (the DVR would merge them anyway)
                out.present["s:%s" % show["id"]] = group_id(pid, show_dirs[folder.lower()])
                return
            base_name, n = _copy_name(folder, groups), 1
            folder = base_name
            while folder.lower() in show_dirs:
                n += 1
                folder = safe_name("%s %d" % (base_name, n))
        show_dirs[folder.lower()] = show["id"]
        gid = group_id(pid, show["id"])
        out.folders[gid] = "TV/%s/%s/%s/" % (SUBDIR, pid, folder)
        if picked:
            out.picked_keys.add("s:%s" % show["id"])
        seen = set()
        added = show.get("modified") or 0
        for ep in sorted(episodes.get(show["id"]) or [], key=lambda e: (e.get("season") or 0, e.get("episode") or 0)):
            season, number = int(ep.get("season") or 0), int(ep.get("episode") or 0)
            if number <= 0 or (season, number) in seen:
                continue
            seen.add((season, number))
            rel = "TV/%s/%s/%s/Season %02d/%s - S%02dE%02d.strm" % (SUBDIR, pid, folder, season, folder, season, number)
            out.files[rel] = "%s/vod/%s/e/%s.%s\n" % (base, pid, ep["id"], ep.get("ext") or "mp4")
            out.meta[rel] = {"kind": "episode", "gid": gid, "show": title, "season": season, "episode": number,
                             "title": episode_title(ep.get("title"), season, number), "plot": ep.get("plot") or "",
                             "image": ep.get("image") or "", "air": ep.get("air") or "",
                             "secs": ep.get("secs") or 0, "added": added}
            out.titles[("episode", str(ep["id"]))] = "%s S%02dE%02d" % (show_name, season, number)
            out.visible.add(rel)
            if picked:
                out.picked.add(rel)
        out.present["s:%s" % show["id"]] = gid
        if seen:
            out.shows[gid] = {"name": title, "year": year, "tmdb": show.get("tmdb") or "",
                              "image": show.get("logo") or "", "fanart": show.get("backdrop") or "",
                              "plot": show.get("plot") or "", "genres": _genres(show.get("genre")),
                              "release": show.get("release") or "", "added": added}
            for group in groups:
                out.members.setdefault(("series", group), []).append(gid)   # a show collection holds the show itself
            out.visible_shows.add(gid)
            if picked:
                out.picked.add(gid)

    series = vod.get("series") or []
    for show in series:
        groups = [g for g in show.get("groups") or [] if g in all_s]
        if groups:
            add_show(show, groups, False)
    for show in series:
        sid = str(show.get("id"))
        if sid in pick_s and "s:" + sid not in out.present:
            groups = [g for g in show.get("groups") or [] if g in avail_s]
            if groups:
                add_show(show, groups, True)
    return out


def file_body(rel, content, meta, hidden, now=None):
    """The DVR item for one file: the restore call's JSON."""
    kind, _sub, rest = rel.partition("/")            # "Movies" or "TV", then ".coax/<pid>/..."
    added = int(meta.get("added") or now or time.time())
    airing = {"Source": "coax", "Channel": "coax", "Time": added, "Tags": ["Stream"],
              "Duration": max(1, int(meta.get("secs") or 0))}   # the DVR requires these; a match sets the runtime
    if meta.get("plot"):
        airing["Summary"] = meta["plot"]
    if meta.get("image"):
        airing["Image"] = meta["image"]
    if meta["kind"] == "movie":
        group = "movies"
        airing.update(Title=meta["title"], Categories=["Movie"])
        if meta.get("year"):
            airing["ReleaseYear"] = int(meta["year"])
        if meta.get("genres"):
            airing["Genres"] = meta["genres"]
        if meta.get("tmdb"):
            airing.update(MovieID="tmdb/%s" % meta["tmdb"], ProgramID="tmdb/movie/%s" % meta["tmdb"])
    else:
        group = meta["gid"]
        airing.update(Title=meta["show"], Categories=["Episode", "Series"], SeriesID=group,
                      SeasonNumber=meta["season"], EpisodeNumber=meta["episode"])
        if meta.get("title"):
            airing["EpisodeTitle"] = meta["title"]
        if re.fullmatch(r"\d{4}-\d\d-\d\d", meta.get("air") or ""):
            airing["OriginalDate"] = meta["air"]
    body = {"GroupID": group, "ImportGroup": group, "ImportPath": "Imports/%s" % kind,
            "Path": rest, "VideoURL": content.strip(), "Completed": True, "CreatedAt": added, "Airing": airing}
    if hidden:
        body["VisibilityMode"] = "hidden"
    return body


def show_body(show, tmdb=None):
    """The DVR group fields for a show: the provider's, with TMDB's where the provider has none."""
    tmdb = tmdb or {}
    release = show.get("release") or tmdb.get("first_air_date") or ""
    body = {"Name": show["name"], "Categories": ["Show", "Series"],
            "Summary": show.get("plot") or tmdb.get("overview") or "",
            "Image": show.get("image") or ((POSTER % tmdb["poster_path"]) if tmdb.get("poster_path") else ""),
            "FanArt": show.get("fanart") or ((FANART % tmdb["backdrop_path"]) if tmdb.get("backdrop_path") else ""),
            "Genres": show.get("genres") or [TMDB_GENRES[g] for g in tmdb.get("genre_ids") or []
                                             if g in TMDB_GENRES][:5]}
    year = _year(release) or _year(show.get("year"))
    if year:
        body["ReleaseYear"] = year
    if re.fullmatch(r"\d{4}-\d\d-\d\d", release):
        body["ReleaseDate"] = release
    return body


# TMDB's genre ids (movies and TV), for the genre names a search result leaves out.
TMDB_GENRES = {
    28: "Action", 12: "Adventure", 16: "Animation", 35: "Comedy", 80: "Crime", 99: "Documentary", 18: "Drama",
    10751: "Family", 14: "Fantasy", 36: "History", 27: "Horror", 10402: "Music", 9648: "Mystery", 10749: "Romance",
    878: "Science Fiction", 10770: "TV Movie", 53: "Thriller", 10752: "War", 37: "Western",
    10759: "Action & Adventure", 10762: "Kids", 10763: "News", 10764: "Reality", 10765: "Sci-Fi & Fantasy",
    10766: "Soap", 10767: "Talk", 10768: "War & Politics",
}


def needs_lookup(meta):
    """True when the provider left out something a library item should have: the TMDB id, the poster or the plot."""
    return not (meta.get("tmdb") and meta.get("image") and meta.get("plot"))


def with_tmdb(meta, found):
    """The provider's details, completed from a TMDB search result: what the provider has stays, what it lacks
    comes from TMDB. A result whose id is not the provider's own TMDB id is not used."""
    if not found or (meta.get("tmdb") and str(found.get("id")) != str(meta["tmdb"])):
        return meta
    out = dict(meta)
    out["tmdb"] = str(found["id"])
    if not out.get("plot") and found.get("overview"):
        out["plot"] = found["overview"]
    if not out.get("image") and found.get("poster_path"):
        out["image"] = POSTER % found["poster_path"]
    if not out.get("year"):
        year = _year(found.get("release_date") or found.get("first_air_date"))
        out["year"] = str(year) if year else ""
    if not out.get("genres") and found.get("genre_ids"):
        out["genres"] = [TMDB_GENRES[g] for g in found["genre_ids"] if g in TMDB_GENRES][:5]
    return out


def _norm(text):
    text = (text or "").lower().replace("&", " and ")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def best_match(results, title, year, name_keys=("title", "original_title"), date_key="release_date"):
    """The TMDB search result that is this title, or None: the same name (or one that holds the other, for
    subtitles), the same year give or take one when both are known; the most voted wins a tie."""
    want = _norm(title)
    if not want:
        return None
    year = _year(year)
    best, best_score = None, 0
    for item in results or []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        names = [_norm(item.get(k)) for k in name_keys if item.get(k)]
        if want in names:
            score = 4
        elif any(n and (n.startswith(want) or want.startswith(n)) and min(len(n), len(want)) >= 4 for n in names):
            score = 2
        else:
            continue
        got = _year(item.get(date_key))
        if year and got:
            if abs(got - year) > 1:
                continue
            score += 2 if got == year else 1
        score += min(float(item.get("vote_count") or 0), 100000) / 1e6
        if score > best_score:
            best, best_score = item, score
    return best


class _Preempted(Exception):
    """New hand-picks arrived while a long Add all import ran: the sync starts over with the picks first."""


class Library:
    """Creates the provider's items in the DVR, matches them on TMDB, keeps the collections and sections."""

    def __init__(self, store, data_dir, imports_dir=None, dvr_call=None):
        self.store = store
        self.imports_dir = imports_dir or IMPORTS_DIR
        self.state_path = os.path.join(data_dir, "cache", "vod-state.json")
        self._dvr_call = dvr_call
        self._lock = threading.Lock()          # one sync at a time
        self._state_lock = threading.RLock()
        self._wake = threading.Event()
        self._match_wake = threading.Event()
        self._wanted = set()
        self._urgent = set()                   # providers with new hand-picks: a running import yields to them
        self._stop = threading.Event()
        self._thread = None
        self._matcher = None
        self._titles = {}
        self._titles_read = set()
        self._local = threading.local()
        self._saved_at = 0.0
        self.running = {}                      # provider id -> what the running sync is doing
        self.state = self._load_state()

    # -- state ------------------------------------------------------------------------------------------------------
    def _load_state(self):
        try:
            with open(self.state_path, encoding="utf-8") as handle:
                data = json.load(handle)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_state(self):
        from app.providers.catalog import atomic_write_json
        with self._state_lock:
            atomic_write_json(self.state_path, self.state)
            self._saved_at = time.monotonic()

    def _save_soon(self):
        if time.monotonic() - self._saved_at >= SAVE_EVERY:
            self._save_state()

    def _pstate(self, pid):
        st = self.state.setdefault(str(pid), {"collections": {}, "section": None})
        for key in ("files", "groups", "todo"):
            if not isinstance(st.get(key), dict):
                st[key] = {}
        return st

    def _titles_path(self, pid):
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(pid)) or "_"
        return os.path.join(os.path.dirname(self.state_path), "vod-%s-titles.json" % safe)

    def title(self, kind, item_id, provider_id=None):
        """A title's name for the log and the Status page; after a restart, read once from the last sync's list."""
        name = self._titles.get((kind, str(item_id)))
        if name is None and provider_id is not None and provider_id not in self._titles_read:
            self._titles_read.add(provider_id)
            from app.providers import vod as vodcat
            stored = vodcat._load_json(self._titles_path(provider_id)) or {}
            for key, value in stored.items():
                k, _sep, i = key.partition(":")
                self._titles.setdefault((k, i), value)
            name = self._titles.get((kind, str(item_id)))
        return name

    def status(self, provider_id):
        ok, why = imports_ready(self.imports_dir)
        st = dict(self.state.get(str(provider_id)) or {})
        for key in ("collections", "files", "groups", "todo", "hidden", "hidden_shows", "present", "where", "retry"):
            st.pop(key, None)
        todo = (self.state.get(str(provider_id)) or {}).get("todo") or {}
        st.update(imports_ok=ok, imports_message=why, running=self.running.get(str(provider_id)),
                  matching=len(todo))
        return st

    # -- the DVR ----------------------------------------------------------------------------------------------------
    def _call(self, cfg, method, path, body=None, timeout=30):
        if self._dvr_call is not None:
            return self._dvr_call(cfg, method, path, body)
        import requests
        from app import channels_dvr
        session = getattr(self._local, "session", None)
        if session is None:   # one kept-alive connection per worker thread: tens of thousands of calls
            session = self._local.session = requests.Session()
        url = channels_dvr.dvr_url(cfg) + path
        try:
            resp = session.request(method, url, json=body, timeout=timeout,
                                   headers={"User-Agent": channels_dvr.USER_AGENT})
        except requests.RequestException as exc:
            raise channels_dvr.DvrError("could not reach Channels DVR at %s: %s" % (channels_dvr.dvr_url(cfg), exc))
        if not 200 <= resp.status_code < 300:
            detail = (resp.text or "").strip()[:300] or resp.reason
            raise channels_dvr.DvrError("Channels DVR answered %s for %s %s: %s"
                                        % (resp.status_code, method, path, detail))
        try:
            return resp.json()
        except ValueError:
            return resp.text

    def _delete_file(self, cfg, fid):
        """Delete one DVR item for good (trash, then permanently). Also removes its file, if still there."""
        try:
            self._call(cfg, "DELETE", "/dvr/files/%s" % fid)
            self._call(cfg, "PUT", "/dvr/files/%s/permanently_delete" % fid)
        except Exception as exc:  # noqa: BLE001 - one item that will not go never stops the rest
            log.warning("VOD: could not delete DVR item %s: %s", fid, exc)

    def _pool(self, workers, func, items):
        """Run func over items on a few threads; stops early when coax is stopping. Returns the results in order."""
        items = list(items)
        if not items:
            return []
        if self._dvr_call is not None or workers <= 1 or len(items) == 1:
            return [None if self._stop.is_set() else func(item) for item in items]
        with ThreadPoolExecutor(max_workers=workers) as pool:
            return list(pool.map(lambda item: None if self._stop.is_set() else func(item), items))

    # -- items ------------------------------------------------------------------------------------------------------
    def _create(self, cfg, pid, pl, rels, refresh=None, preemptible=False):
        """Create these files' items in the DVR. A new show's first episode goes alone (it creates the group), then
        coax names the group; the rest go in parallel. `refresh` (the collections update) runs every
        COLLECTIONS_EVERY seconds meanwhile, so the collections fill as the titles arrive.

        `preemptible`: new hand-picks (request(pid, urgent=True)) stop this run between items with _Preempted, so a
        pick never waits behind a long Add all import; the sync then starts over with the picks first."""
        st = self._pstate(pid)
        total, done = len(rels), [0]
        stopped = [False]
        failed = []
        refreshing = threading.Lock()
        last = [time.monotonic()]

        def tick():
            if refresh is None or time.monotonic() - last[0] < COLLECTIONS_EVERY or not refreshing.acquire(False):
                return
            try:
                last[0] = time.monotonic()
                refresh()
            except Exception as exc:  # noqa: BLE001
                log.warning("VOD provider %s: collections not updated: %s", pid, exc)
            finally:
                refreshing.release()

        def make(rel):
            if preemptible and (stopped[0] or pid in self._urgent):
                stopped[0] = True
                return
            meta = pl.meta[rel]
            if meta["kind"] == "movie" and needs_lookup(meta):
                meta = with_tmdb(meta, self._search_movie(cfg, pid, meta))
            body = file_body(rel, pl.files[rel], meta, hidden=rel not in pl.visible)
            try:
                made = self._call(cfg, "POST", "/dvr/files", body) or {}
            except Exception as exc:  # noqa: BLE001
                failed.append(str(exc))
                return
            fid = str(made.get("ID") or "") if isinstance(made, dict) else ""
            if not fid:
                failed.append("no id in the answer")
                return
            with self._state_lock:
                st["files"][rel] = fid
                if body.get("VisibilityMode") == "hidden":
                    st.setdefault("hidden", []).append(fid)
                if meta["kind"] == "movie":
                    st["todo"]["f:" + fid] = [meta.get("tmdb") or "", meta["title"], meta.get("year") or ""]
                done[0] += 1
                if done[0] % 200 == 0 or done[0] == total:
                    self.running[pid] = "%s to Channels DVR (%d of %d)" % (
                        "adding" if preemptible else "adding hand-picked titles", done[0], total)
                self._save_soon()
            tick()

        new_groups, first, rest = set(), [], []
        for rel in rels:
            gid = pl.meta[rel].get("gid")
            if gid and gid not in st["groups"] and gid not in new_groups:
                new_groups.add(gid)
                first.append(rel)
            else:
                rest.append(rel)
        if not self._search_handle(pid):
            lead = next((r for r in rest if pl.meta[r]["kind"] == "movie"), None)
            if lead:   # one movie first: TMDB searches go through a DVR item, and this becomes it
                rest.remove(lead)
                make(lead)
        # Movies first (most of a library, and every one is a title of its own), then the shows, then their episodes.
        self._pool(CREATE_WORKERS, make, [r for r in rest if pl.meta[r]["kind"] == "movie"])
        self._pool(CREATE_WORKERS, make, first)
        made_groups = [g for g in sorted(new_groups) if any(st["files"].get(r) for r in first
                                                            if pl.meta[r].get("gid") == g)]
        self._pool(CREATE_WORKERS, lambda g: self._new_show(cfg, pid, pl, g), made_groups)
        self._pool(CREATE_WORKERS, make, [r for r in rest if pl.meta[r]["kind"] != "movie"])
        if failed:
            log.warning("VOD provider %s: %d of %d items not added to Channels DVR (first error: %s); the next sync "
                        "tries again", pid, len(failed), total, failed[0])
        self._save_state()
        if stopped[0]:
            raise _Preempted()

    def _search_handle(self, pid):
        """A movie item of coax's to run TMDB searches through: the DVR's search takes any item's id with ?q=."""
        files = self._pstate(pid)["files"]
        return next((fid for rel, fid in files.items() if rel.startswith("Movies/")), None)

    def _search_movie(self, cfg, pid, meta):
        """coax's own TMDB lookup for a movie the provider left thin: the DVR's TMDB search (Channels DVR's own
        access, no API key), the result picked by coax (same name, year within one; the provider's TMDB id wins)."""
        handle = self._search_handle(pid)
        if not handle:
            return None
        from urllib.parse import quote
        try:
            results = self._call(cfg, "GET", "/dvr/files/%s/matches/tmdb/movies?q=%s" % (handle, quote(meta["title"])))
        except Exception as exc:  # noqa: BLE001
            log.debug("VOD: TMDB search for %s failed: %s", meta["title"], exc)
            return None
        if meta.get("tmdb"):
            return next((r for r in results or [] if isinstance(r, dict) and str(r.get("id")) == str(meta["tmdb"])),
                        None)
        return best_match(results, meta["title"], meta.get("year"))

    def _search_show(self, cfg, gid, show):
        from urllib.parse import quote
        try:
            results = self._call(cfg, "GET", "/dvr/groups/%s/matches/tmdb?q=%s" % (gid, quote(show["name"])))
        except Exception as exc:  # noqa: BLE001
            log.debug("VOD: TMDB search for %s failed: %s", show["name"], exc)
            return None
        if show.get("tmdb"):
            return next((r for r in results or [] if isinstance(r, dict) and str(r.get("id")) == str(show["tmdb"])),
                        None)
        return best_match(results, show["name"], show.get("year"), ("name", "original_name"), "first_air_date")

    def _new_show(self, cfg, pid, pl, gid):
        st = self._pstate(pid)
        show = dict(pl.shows[gid])
        found = self._search_show(cfg, gid, show) if needs_lookup(show) else None
        if found:
            show["tmdb"] = str(found["id"])
            if not show.get("genres") and found.get("genre_ids"):
                show["genres"] = [TMDB_GENRES[g] for g in found["genre_ids"] if g in TMDB_GENRES][:5]
        try:
            self._call(cfg, "PUT", "/dvr/groups/%s" % gid, show_body(show, found))
            if gid not in pl.visible_shows:
                self._call(cfg, "PUT", "/dvr/groups/%s/visibility/hidden" % gid)
        except Exception as exc:  # noqa: BLE001
            log.warning("VOD: could not name show %s in Channels DVR: %s", show["name"], exc)
        with self._state_lock:
            st["groups"][gid] = 1
            if gid not in pl.visible_shows:
                hidden = st.setdefault("hidden_shows", [])
                if gid not in hidden:
                    hidden.append(gid)
            st["todo"]["g:" + gid] = [show.get("tmdb") or "", show["name"], show.get("year") or ""]

    def _visibility(self, cfg, pid, pl):
        """Show or hide existing items when a group's choice changed. Only changes are sent."""
        st = self._pstate(pid)
        files = {fid: rel for rel, fid in st["files"].items() if rel in pl.files}
        want = {fid for fid, rel in files.items() if rel not in pl.visible}
        done = set(st.get("hidden") or []) & set(files)
        self._pool(CREATE_WORKERS, lambda f: self._call(cfg, "PUT", "/dvr/files/%s/visibility/hidden" % f),
                   sorted(want - done))
        self._pool(CREATE_WORKERS, lambda f: self._call(cfg, "PUT", "/dvr/files/%s/visibility/main" % f),
                   sorted(done - want))
        st["hidden"] = sorted(want)
        groups = set(st["groups"]) & set(pl.shows)
        want_g = groups - pl.visible_shows
        done_g = set(st.get("hidden_shows") or []) & groups
        for gid in sorted(want_g - done_g):
            self._call(cfg, "PUT", "/dvr/groups/%s/visibility/hidden" % gid)
        for gid in sorted(done_g - want_g):
            self._call(cfg, "PUT", "/dvr/groups/%s/visibility/main" % gid)
        st["hidden_shows"] = sorted(want_g)

    def _drop_missing(self, cfg, pid, pl):
        """Delete from the DVR the items and shows the picks no longer have."""
        st = self._pstate(pid)
        gone = [(rel, fid) for rel, fid in st["files"].items() if rel not in pl.files]
        self._pool(CREATE_WORKERS, lambda item: self._delete_file(cfg, item[1]), gone)
        with self._state_lock:
            for rel, fid in gone:
                st["files"].pop(rel, None)
                st["todo"].pop("f:" + fid, None)
        for gid in [g for g in st["groups"] if g not in pl.shows]:
            try:
                self._call(cfg, "DELETE", "/dvr/groups/%s" % gid)
            except Exception as exc:  # noqa: BLE001
                log.warning("VOD: could not delete show %s from Channels DVR: %s", gid, exc)
            with self._state_lock:
                st["groups"].pop(gid, None)
                st["todo"].pop("g:" + gid, None)
        return len(gone)

    def _collections(self, cfg, provider, members):
        """One DVR collection per picked group, one Personal Section with a row for each."""
        pid = provider["id"]
        st = self._pstate(pid)
        existing = {str(c.get("ID")): c for c in self._call(cfg, "GET", "/dvr/collections/content") or []}
        kept = {}
        rows = []
        for (kind, group), keys in members.items():
            if kind == "movies":
                items = list(dict.fromkeys(st["files"][k] for k in keys if st["files"].get(k)))
            else:
                items = [k for k in keys if k in st["groups"]]
            key = "%s:%s" % (kind, group)
            name = group_label(provider, group)
            cid = (st.get("collections") or {}).get(key)
            body = {"Name": name, "Type": "movies" if kind == "movies" else "shows", "Items": items}
            if cid and cid in existing:
                current = existing[cid]
                if "Items" not in current:   # the list leaves the items out
                    current = self._call(cfg, "GET", "/dvr/collections/content/%s" % cid) or current
                if current.get("Name") != name or list(current.get("Items") or []) != items:
                    merged = dict(current)
                    merged.update(body)
                    self._call(cfg, "PUT", "/dvr/collections/content/%s" % cid, merged)
            else:
                cid = str((self._call(cfg, "POST", "/dvr/collections/content/new", body) or {}).get("ID") or "")
            if cid:
                kept[key] = cid
                rows.append({"ID": uuid.uuid4().hex[:16], "Title": name.split(" · ", 1)[-1],
                             "ContentType": "collection", "ParentID": cid, "SortType": "date_added",
                             "SortOrder": "forward", "ShowMoreButton": True})
        for key, cid in (st.get("collections") or {}).items():
            if key not in kept and cid in existing:
                self._call(cfg, "DELETE", "/dvr/collections/content/%s" % cid)
        st["collections"] = kept
        self._section(cfg, provider, rows)

    def _section(self, cfg, provider, rows):
        st = self._pstate(provider["id"])
        sections = {str(s.get("ID")): s for s in self._call(cfg, "GET", "/dvr/custom_sections") or []}
        sid = st.get("section")
        if not rows:
            if sid and sid in sections:
                self._call(cfg, "DELETE", "/dvr/custom_sections/%s" % sid)
            st["section"] = None
            return
        title = provider.get("name") or provider["id"]
        if sid and sid in sections:
            current = sections[sid]
            old = [(r.get("ParentID"), r.get("Title")) for r in current.get("Rows") or []]
            if old != [(r["ParentID"], r["Title"]) for r in rows] or current.get("Title") != title:
                body = dict(current)
                body.update(Title=title, Rows=rows)
                self._call(cfg, "PUT", "/dvr/custom_sections/%s" % sid, body)
        else:
            made = self._call(cfg, "POST", "/dvr/custom_sections/new",
                              {"Title": title, "Icon": SECTION_ICON, "SectionType": "rows", "Rows": rows}) or {}
            st["section"] = str(made.get("ID") or "") or None

    # -- TMDB matching ----------------------------------------------------------------------------------------------
    def _match_one(self, cfg, pid, key, hint):
        """Match one movie or show on TMDB through the DVR. Returns True (matched), False (no match: not tried
        again) or None (an error: tried again later)."""
        tmdb, title, year = (list(hint) + ["", "", ""])[:3]
        kind, _sep, ident = key.partition(":")
        try:
            if kind == "f":
                if not tmdb:
                    found = best_match(self._call(cfg, "GET", "/dvr/files/%s/matches/tmdb/movies" % ident), title, year)
                    if found is None:
                        from urllib.parse import quote
                        found = best_match(self._call(cfg, "GET", "/dvr/files/%s/matches/tmdb/movies?q=%s"
                                                      % (ident, quote(title))), title, year)
                    if found is None:
                        return False
                    tmdb = found["id"]
                self._call(cfg, "PUT", "/dvr/files/%s/matches/tmdb/movies/%s" % (ident, tmdb), timeout=120)
                return True
            from urllib.parse import quote
            results = self._call(cfg, "GET", "/dvr/groups/%s/matches/tmdb?q=%s" % (ident, quote(title))) or []
            if tmdb:
                found = next((r for r in results if isinstance(r, dict) and str(r.get("id")) == str(tmdb)), None) \
                    or {"id": int(tmdb)}
            else:
                found = best_match(results, title, year, ("name", "original_name"), "first_air_date")
                if found is None:
                    return False
            self._call(cfg, "PUT", "/dvr/groups/%s/matches/tmdb/%s" % (ident, found["id"]), found, timeout=300)
            if found.get("overview") or found.get("poster_path"):
                group = self._call(cfg, "GET", "/dvr/groups/%s" % ident) or {}
                show = {"name": group.get("Name") or title, "year": year, "plot": group.get("Summary") or "",
                        "image": group.get("Image") or "", "fanart": group.get("FanArt") or "",
                        "genres": group.get("Genres") or [], "release": group.get("ReleaseDate") or ""}
                self._call(cfg, "PUT", "/dvr/groups/%s" % ident, show_body(show, found))
            return True
        except Exception as exc:  # noqa: BLE001
            log.debug("VOD: TMDB match of %s %s failed: %s", key, title, exc)
            return None

    def match_pending(self, limit=None):
        """Work through every provider's unmatched movies and shows, a few at a time. Returns how many were done."""
        cfg = self.store.get()
        if not ((cfg.get("channels_dvr") or {}).get("url") or "").strip():
            return 0
        handled = 0
        for pid in list(self.state):
            provider = next((p for p in cfg.get("providers") or [] if p.get("id") == pid), None)
            if provider is None or provider.get("enabled", True) is False:
                continue
            if self.running.get(pid):
                continue   # titles are being added: the DVR writes one thing at a time, adding comes first
            st = self._pstate(pid)
            now = time.time()
            retry = st.setdefault("retry", {})
            todo = [(k, v) for k, v in list(st["todo"].items()) if retry.get(k, 0) <= now]
            todo.sort(key=lambda kv: kv[0][0] != "g")   # shows first: one match fills every episode
            if limit is not None:
                todo = todo[:limit]
            if not todo:
                continue
            stats = {"matched": 0, "none": 0, "error": 0}

            def one(item):
                key, hint = item
                result = self._match_one(cfg, pid, key, hint)
                with self._state_lock:
                    if result is None:
                        retry[key] = time.time() + MATCH_RETRY
                        stats["error"] += 1
                    else:
                        st["todo"].pop(key, None)
                        retry.pop(key, None)
                        stats["matched" if result else "none"] += 1
                    self._save_soon()
            self._pool(MATCH_WORKERS, one, todo)
            handled += len(todo)
            self._save_state()
            log.info("VOD provider %s: %d matched on TMDB, %d with no match, %d to try again; %d left", pid,
                     stats["matched"], stats["none"], stats["error"], len(st["todo"]))
        return handled

    # -- files ------------------------------------------------------------------------------------------------------
    def _roots(self, pid, sub=SUBDIR):
        return [os.path.join(self.imports_dir, "Movies", sub, str(pid)),
                os.path.join(self.imports_dir, "TV", sub, str(pid))]

    def _existing(self, pid):
        out = {}
        for root in self._roots(pid):
            for folder, _dirs, names in os.walk(root):
                for name in names:
                    if name.endswith(".strm"):
                        full = os.path.join(folder, name)
                        out[os.path.relpath(full, self.imports_dir).replace(os.sep, "/")] = full
        return out

    def _write(self, pid, files):
        """Make the provider's folders hold exactly these files. Returns (count written, paths deleted)."""
        existing = self._existing(pid)
        written, deleted = 0, []
        for rel, content in files.items():
            full = os.path.join(self.imports_dir, *rel.split("/"))
            if rel in existing:
                try:
                    with open(full, encoding="utf-8") as handle:
                        if handle.read() == content:
                            continue
                except OSError:
                    pass
            _makedirs(os.path.dirname(full))
            tmp = full + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                handle.write(content)
            try:
                os.chmod(tmp, 0o666)
            except OSError:
                pass
            os.replace(tmp, full)
            written += 1
        for rel, full in existing.items():
            if rel not in files:
                try:
                    os.unlink(full)
                    deleted.append(rel)
                except OSError as exc:
                    log.warning("VOD: could not remove %s: %s", rel, exc)
        for root in self._roots(pid):
            folders = [w[0] for w in os.walk(root)] + [os.path.dirname(root)]   # down to Movies/.coax, TV/.coax
            for folder in sorted(folders, key=len, reverse=True):
                try:
                    if not os.listdir(folder):
                        os.rmdir(folder)
                except OSError:
                    pass
        return written, deleted

    def _migrate(self, cfg, pid):
        """Take the earlier, scanner-imported layout (Movies/coax/<pid>, TV/coax/<pid>) out of the DVR and the disk
        once: its items would stand beside the new ones."""
        old = [r for r in self._roots(pid, OLD_SUBDIR) if os.path.isdir(r)]
        if not old:
            return
        self.running[pid] = "removing the earlier scanned titles"
        marks = ("/Imports/Movies/%s/%s/" % (OLD_SUBDIR, pid), "/Imports/TV/%s/%s/" % (OLD_SUBDIR, pid))
        ids = []
        if ((cfg.get("channels_dvr") or {}).get("url") or "").strip():
            for kind in ("movies", "episodes"):
                for item in self._call(cfg, "GET", "/api/v1/%s?source=stream-files" % kind, timeout=600) or []:
                    if any(m in str(item.get("path") or "") for m in marks) and item.get("id"):
                        ids.append(str(item["id"]))
            ids = list(dict.fromkeys(ids))
            self._pool(CREATE_WORKERS, lambda fid: self._delete_file(cfg, fid), ids)
        for root in old:
            shutil.rmtree(root, ignore_errors=True)
            parent = os.path.dirname(root)
            try:
                if not os.listdir(parent):
                    os.rmdir(parent)
            except OSError:
                pass
        st = self._pstate(pid)
        st.pop("hidden", None)
        st.pop("hidden_shows", None)
        log.info("VOD provider %s: %d scanned titles of the earlier layout removed from Channels DVR", pid, len(ids))

    # -- sync -------------------------------------------------------------------------------------------------------
    def sync(self, provider_id):
        """Bring one provider's items, collections and section up to date. Never raises."""
        pid = str(provider_id)
        with self._lock:
            started = time.time()
            st = self._pstate(pid)
            try:
                self._sync(pid)
                st.update(error="", last_sync=started)
            except _Preempted:
                # Hand-picks came in: start over at once with them first. Not an error, and not a finished sync.
                log.info("VOD provider %s: new hand-picks first; the rest carries on right after them", pid)
                self._wanted.add(pid)
                self._wake.set()
            except Exception as exc:  # noqa: BLE001 - a VOD sync must never take coax down
                from app.providers.net import redact
                cfg = self.store.get()
                provider = next((p for p in cfg.get("providers") or [] if p.get("id") == pid), {})
                message = redact(getattr(exc, "message", None) or str(exc),
                                 [l.get("password") or "" for l in provider.get("lines") or []])
                log.warning("VOD sync of provider %s failed: %s", pid, message)
                st.update(error=message, last_try=started)
            finally:
                self.running.pop(pid, None)
                self._save_state()
        self._match_wake.set()

    def _sync(self, pid):
        from app.providers import vod as vodcat, catalog
        self._urgent.discard(pid)   # this run starts with every pick there is now
        cfg = self.store.get()
        provider = next((p for p in cfg.get("providers") or [] if p.get("id") == pid), None)
        movie_groups, series_groups = selected(provider or {})
        if provider is None or not (movie_groups or series_groups):
            self.remove(pid, cfg)
            return
        if provider.get("enabled", True) is False:
            return   # switched off is a pause: the library stays (watch state, resume), nothing is fetched
        # The catalog first: the On demand page lists the available groups' titles even when nothing is picked.
        self.running[pid] = "reading the provider's movies and series"
        vod = vodcat.load(pid)
        hours = vod_hours(provider)
        age = catalog.age_hours(vod) if vod else None
        if (provider.get("type") or "xtream") == "xtream" and \
                (vod is None or age is None or (hours is not None and age >= hours)):
            vod = vodcat.refresh(provider)
        vod = vod or {}
        all_m, all_s = add_all(provider)
        pick_m, pick_s = picks(provider)
        if not (all_m or all_s or pick_m or pick_s):
            if self.state.get(pid, {}).get("files") or self.state.get(pid, {}).get("collections"):
                self.remove(pid, cfg)   # the last title was taken out: clear Channels DVR and the files
            return
        ok, why = imports_ready(self.imports_dir)
        if not ok:
            raise RuntimeError(why)
        base = ((cfg.get("server") or {}).get("base_url") or "").strip().rstrip("/")
        if not base:
            raise RuntimeError("set coax's Base URL in Settings first: the Stream Files point at it")
        self._migrate(cfg, pid)
        if self._respect_deletions(cfg, provider):
            cfg = self.store.get()   # picks the user deleted in Channels DVR are no longer picked
            provider = next((p for p in cfg.get("providers") or [] if p.get("id") == pid), provider)
            pick_m, pick_s = picks(provider)
        wanted, avail = set(all_s), set(series_groups)
        # Picked shows first (they go to Channels DVR before any Add all backlog), then newest first (the panel's
        # last_modified): recently added and airing shows land in the first batch.
        shows = sorted((s for s in vod.get("series") or [] if wanted.intersection(s.get("groups") or [])
                        or (str(s.get("id")) in pick_s and avail.intersection(s.get("groups") or []))),
                       key=lambda s: (str(s.get("id")) not in pick_s, -(s.get("modified") or 0)))
        st = self._pstate(pid)
        known = vodcat.episodes_for(provider, shows, fetch=False) if shows else {}
        # A picked show's episodes are read now (a few calls), so it does not wait for the next batch.
        picked_shows = [s for s in shows if str(s.get("id")) in pick_s and not known.get(s["id"])]
        if picked_shows:
            self.running[pid] = "reading the episodes of hand-picked shows"
            known.update(vodcat.episodes_for(provider, picked_shows, max_fetch=50))
        # Movies first: the movies and every show whose episodes are already known go to Channels DVR now, before
        # any episode list is asked for (that can take half an hour on a big catalog).
        self._publish(cfg, provider, vod, known, base)
        # Then the episode lists, in batches: each batch goes to Channels DVR as soon as it is read.
        st.pop("continue_at", None)
        st.pop("series_left", None)
        spent, info = 0.0, {}
        while shows and spent < EPISODE_BUDGET and not self._stop.is_set():
            if pid in self._urgent:
                raise _Preempted()
            started = time.monotonic()

            def progress(done, total):
                self.running[pid] = "reading episodes (%d of %d series)" % (done, total)
            info = {}
            episodes = vodcat.episodes_for(provider, shows, progress=progress, max_fetch=BATCH_SERIES, info=info,
                                           deadline=started + min(BATCH_SECONDS, EPISODE_BUDGET - spent))
            spent += time.monotonic() - started
            if info.get("fetched"):
                self._publish(cfg, provider, vod, episodes, base)
            if not info.get("left") or info.get("throttled") or not info.get("fetched"):
                break
        if info.get("left"):
            # Not done: carry on soon rather than at the next daily refresh; a throttling panel gets an hour's rest.
            wait = THROTTLE_WAIT if info.get("throttled") else CONTINUE_AFTER
            st.update(continue_at=time.time() + wait, series_left=info["left"])
            log.info("VOD provider %s: %d series still to read; the next batch in %d minutes", pid, info["left"],
                     wait // 60)

    def _publish(self, cfg, provider, vod, episodes, base):
        """Write the files for what is known now, create their items in Channels DVR, update the collections."""
        pid = provider["id"]
        pl = plan(provider, vod, episodes, base)
        self._titles.update(pl.titles)
        self.running[pid] = "writing Stream Files"
        written, _deleted = self._write(pid, pl.files)
        st = self._pstate(pid)
        st.update(movies=sum(1 for r in pl.files if r.startswith("Movies/")),
                  episodes=sum(1 for r in pl.files if r.startswith("TV/")), shows=len(pl.shows))
        from app.providers import catalog
        catalog.atomic_write_json(self._titles_path(pid), {"%s:%s" % key: name for key, name in pl.titles.items()})
        if not ((cfg.get("channels_dvr") or {}).get("url") or "").strip():
            st["dvr_note"] = "Set the Channels DVR URL in Settings so coax can add the titles to it."
            log.info("VOD provider %s: %d movies, %d episodes of %d shows written (%d new); no Channels DVR URL", pid,
                     st["movies"], st["episodes"], st["shows"], written)
            return
        st.pop("dvr_note", None)
        removed = self._drop_missing(cfg, pid, pl)
        # Newest first, so what the provider added last is in the library first (movies before episodes: _create).
        new = [rel for rel in pl.files if rel not in st["files"]]
        new.sort(key=lambda r: -(pl.meta[r].get("added") or 0))
        picked = [rel for rel in new if rel in pl.picked]
        rest = [rel for rel in new if rel not in pl.picked]
        started = time.monotonic()
        try:
            if new:
                self._collections(cfg, provider, pl.members)   # the collections and section exist from the start...
            if picked:   # hand-picks never wait behind an Add all import
                self.running[pid] = "adding hand-picked titles to Channels DVR (0 of %d)" % len(picked)
                self._create(cfg, pid, pl, picked)
                self._collections(cfg, provider, pl.members)
            if rest:
                self.running[pid] = "adding to Channels DVR (0 of %d)" % len(rest)
                self._create(cfg, pid, pl, rest, refresh=lambda: self._collections(cfg, provider, pl.members),
                             preemptible=True)
        finally:
            self._note_present(pid, pl)
        self._visibility(cfg, pid, pl)
        log.info("VOD provider %s: %d movies, %d episodes of %d shows; %d added to Channels DVR in %.0fs, %d removed",
                 pid, st["movies"], st["episodes"], st["shows"], sum(1 for r in new if st["files"].get(r)),
                 time.monotonic() - started, removed)
        self.running[pid] = "updating the collections"
        self._collections(cfg, provider, pl.members)
        self._save_state()
        self._match_wake.set()

    def _note_present(self, pid, pl):
        """Which titles are in Channels DVR now ("m:<id>", "s:<id>"), for the On demand page's Content list."""
        st = self._pstate(pid)
        with self._state_lock:
            st["present"] = sorted(k for k, v in pl.present.items()
                                   if (st["files"].get(v) if k.startswith("m:") else v in st["groups"]))
            # Where each hand-pick lives, so one deleted in Channels DVR can be forgotten (_respect_deletions).
            st["where"] = {k: (pl.present[k] if k.startswith("m:") else pl.folders.get(pl.present[k], ""))
                           for k in pl.picked_keys if k in pl.present}

    def present(self, pid):
        """The set of "m:<id>" / "s:<id>" keys of a provider's titles that are in Channels DVR."""
        return set((self.state.get(str(pid)) or {}).get("present") or [])

    def _respect_deletions(self, cfg, provider):
        """A hand-picked title the user deleted inside Channels DVR is no longer picked (it is never added back).
        True when a pick was dropped. Add all titles keep their old behavior: coax does not re-add what was deleted."""
        pid = provider["id"]
        st = self._pstate(pid)
        pick_m, pick_s = picks(provider)
        if not (pick_m or pick_s) or not ((cfg.get("channels_dvr") or {}).get("url") or "").strip():
            return False
        from app import channels_dvr
        present = {}
        for key in (st.get("present") or []):
            kind, _sep, ident = key.partition(":")
            if (kind == "m" and ident in pick_m) or (kind == "s" and ident in pick_s):
                present[key] = ident
        if not present:
            return False
        where = st.get("where") or {}   # "m:<id>" -> movie path, "s:<id>" -> the show's folder path prefix
        gone = []

        def check(key):
            kind, ident = key.split(":", 1)
            try:
                if kind == "m":
                    fid = st["files"].get(where.get(key) or "")
                    if not fid:
                        return
                    item = self._call(cfg, "GET", "/dvr/files/%s" % fid)
                else:
                    # A show is deleted in Channels DVR when every one of its episodes is (or the show is gone).
                    item = self._call(cfg, "GET", "/dvr/groups/%s/files" % group_id(pid, ident))
                    if isinstance(item, list) and all(isinstance(f, dict) and f.get("Deleted") for f in item):
                        gone.append(key)
                    return
            except channels_dvr.DvrError as exc:
                if "answered 404 " in str(exc):
                    gone.append(key)
                return
            except Exception:  # noqa: BLE001 - unknown: keep the pick
                return
            if isinstance(item, dict) and item.get("Deleted"):
                gone.append(key)
        self._pool(CREATE_WORKERS, check, sorted(present))
        if not gone:
            return False
        gone_m = {k[2:] for k in gone if k.startswith("m:")}
        gone_s = {k[2:] for k in gone if k.startswith("s:")}

        def mutate(c):
            for p in c.get("providers") or []:
                if p.get("id") == pid and isinstance(p.get("vod"), dict):
                    vod = materialize(p["vod"])
                    vod["picks"] = {"movies": [i for i in vod["picks"].get("movies") or [] if str(i) not in gone_m],
                                    "series": [i for i in vod["picks"].get("series") or [] if str(i) not in gone_s]}
                    p["vod"] = vod
        self.store.update(mutate)
        # Forget them here too: they are gone from Channels DVR, so nothing must try to delete them again, and a
        # later pick of the same title adds it afresh.
        with self._state_lock:
            for key in gone:
                spot = where.pop(key, "") or ""
                if key.startswith("m:"):
                    fid = st["files"].pop(spot, None)
                    if fid:
                        st["todo"].pop("f:" + str(fid), None)
                else:
                    gid = group_id(pid, key[2:])
                    st["groups"].pop(gid, None)
                    st["todo"].pop("g:" + gid, None)
                    for rel in [r for r in st["files"] if spot and r.startswith(spot)]:
                        st["todo"].pop("f:" + str(st["files"].pop(rel)), None)
            st["where"] = where
            st["present"] = [k for k in st.get("present") or [] if k not in set(gone)]
        log.info("VOD provider %s: %d hand-picked titles were deleted in Channels DVR; they are no longer picked",
                 pid, len(gone))
        return True

    def remove(self, pid, cfg=None):
        """Take a provider's VOD out of the DVR: its items, shows, files, collections and section."""
        cfg = cfg or self.store.get()
        pid = str(pid)
        st = self.state.get(pid)
        has_dvr = bool(((cfg.get("channels_dvr") or {}).get("url") or "").strip())
        if st and has_dvr:
            try:
                self._pool(CREATE_WORKERS, lambda fid: self._delete_file(cfg, fid),
                           list((st.get("files") or {}).values()))
                for gid in st.get("groups") or {}:
                    try:
                        self._call(cfg, "DELETE", "/dvr/groups/%s" % gid)
                    except Exception:  # noqa: BLE001
                        pass
                existing = {str(c.get("ID")) for c in self._call(cfg, "GET", "/dvr/collections/content") or []}
                for cid in (st.get("collections") or {}).values():
                    if cid in existing:
                        self._call(cfg, "DELETE", "/dvr/collections/content/%s" % cid)
                sections = {str(s.get("ID")) for s in self._call(cfg, "GET", "/dvr/custom_sections") or []}
                if st.get("section") in sections:
                    self._call(cfg, "DELETE", "/dvr/custom_sections/%s" % st["section"])
            except Exception as exc:  # noqa: BLE001
                log.warning("VOD: could not remove provider %s from Channels DVR: %s", pid, exc)
        deleted = []
        if os.path.isdir(self.imports_dir):
            _written, deleted = self._write(pid, {})
            if has_dvr or not st:
                self._migrate(cfg, pid)
        if st or deleted:
            log.info("VOD provider %s removed from Channels DVR (%d items)", pid,
                     len((st or {}).get("files") or {}) or len(deleted))
        self.state.pop(pid, None)

    # -- schedule ---------------------------------------------------------------------------------------------------
    def request(self, provider_id=None, urgent=False):
        """Sync one provider (or every provider) soon. `urgent` (new hand-picks): a running Add all import of that
        provider stops between titles and the sync starts over with the picks first."""
        cfg = self.store.get()
        ids = [provider_id] if provider_id else [p["id"] for p in cfg.get("providers") or []]
        self._wanted.update(str(i) for i in ids)
        if urgent and provider_id:
            self._urgent.add(str(provider_id))
        self._wake.set()

    def _due(self, provider):
        movie_groups, series_groups = selected(provider)
        st = self.state.get(provider["id"]) or {}
        if not (movie_groups or series_groups):
            return bool(st)   # every group made unavailable: clean up once
        if st.get("continue_at") and time.time() >= st["continue_at"]:
            return True   # episode lists still to read
        if st.get("files") and "present" not in st:
            return True   # an older build's library: learn which titles are in Channels DVR for the On demand page
        if os.path.isdir(os.path.join(self.imports_dir, "Movies", OLD_SUBDIR, provider["id"])) or \
                os.path.isdir(os.path.join(self.imports_dir, "TV", OLD_SUBDIR, provider["id"])):
            return True   # the earlier layout is still there: move over now
        hours = vod_hours(provider)
        if hours is None:
            return False   # manual: Save and Sync now only
        last = max(st.get("last_sync") or 0, st.get("last_try") or 0)
        return time.time() - last >= hours * 3600

    def run_once(self):
        cfg = self.store.get()
        wanted, self._wanted = self._wanted, set()
        known = set()
        for provider in cfg.get("providers") or []:
            known.add(provider["id"])
            if self._stop.is_set():
                return
            if provider["id"] in wanted or self._due(provider):
                self.sync(provider["id"])
        for pid in [p for p in self.state if p not in known]:   # a deleted provider
            with self._lock:
                self.remove(pid, cfg)
                self._save_state()
            from app.providers import vod as vodcat
            vodcat.forget(pid)

    def start(self):
        def loop():
            while not self._stop.is_set():
                try:
                    self.run_once()
                except Exception:  # noqa: BLE001
                    log.exception("VOD scheduler pass failed")
                self._wake.wait(TICK_SECONDS)
                self._wake.clear()

        def matcher():
            while not self._stop.is_set():
                try:
                    busy = self.match_pending(limit=MATCH_WORKERS * 50)
                except Exception:  # noqa: BLE001
                    log.exception("VOD TMDB matching pass failed")
                    busy = 0
                if not busy:
                    self._match_wake.wait(TICK_SECONDS)
                    self._match_wake.clear()
        self._thread = threading.Thread(target=loop, name="vod", daemon=True)
        self._thread.start()
        self._matcher = threading.Thread(target=matcher, name="vod-match", daemon=True)
        self._matcher.start()
        return self

    def stop(self):
        self._stop.set()
        self._wake.set()
        self._match_wake.set()
