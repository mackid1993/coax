"""Video on demand in Channels DVR: the provider's movies and series as Stream Files in the DVR's library.

Channels DVR imports a ".strm" file (a text file holding one video address) from its Imports folder as a regular
library item: matched to its movie or show, searchable, with watch state and resume. coax writes one per movie and
episode of the VOD groups picked on a provider, into the DVR's Imports folder (mounted at /imports):

  Movies/coax/<provider id>/<Title (Year)>.strm
  TV/coax/<provider id>/<Show (Year)>/Season 01/<Show (Year)> - S01E02.strm

Each file holds coax's own address (<Base URL>/vod/<provider>/<m|e>/<id>.<ext>), never the provider's: the login
stays out of the DVR, and every play goes through coax, which holds a line slot for it (app/vodproxy.py). The DVR's
scanner reads the files only; it never opens the address.

After writing, coax asks the DVR to scan (and to prune what it deleted), then keeps one DVR collection per picked
group and one Personal Section per provider showing them. By default the titles are set to Hidden, so they stay out
of the main Movies and TV Shows listings and live in their section; a group can be set to show its titles with the
user's own library instead (vod.library), and switching it either way changes them on the next sync. The sync runs when the picks change and on the provider's refresh schedule (default daily).
Nothing outside Movies/coax and TV/coax is ever written or removed.
"""

import json
import logging
import os
import re
import threading
import time
import uuid

log = logging.getLogger("coax.vod")

IMPORTS_DIR = "/imports"
SUBDIR = "coax"
TICK_SECONDS = 60
EPISODE_BUDGET = 1800        # seconds of get_series_info calls per sync; the next sync carries on
SCAN_WAIT = 6 * 3600         # how long to wait for the DVR's scanner before updating collections anyway
SCAN_POLL = 5
SECTION_ICON = "film"
_BAD_CHARS = re.compile(r'[\\/*?"<>|\x00-\x1f]')


def imports_ready(path=None):
    """(True, "") when the Channels DVR Imports folder is mounted, else (False, why)."""
    path = path or IMPORTS_DIR
    if not os.path.isdir(path):
        return False, ("Channels DVR's Imports folder is not connected to coax. Mount it at /imports "
                       "(CHANNELS_DVR_IMPORTS in the compose .env), then recreate the container.")
    if not (os.path.isdir(os.path.join(path, "Movies")) or os.path.isdir(os.path.join(path, "TV"))):
        return False, ("The folder mounted at /imports is not Channels DVR's Imports folder (it has no Movies or TV "
                       "folder). Point CHANNELS_DVR_IMPORTS at the Imports folder inside the DVR's storage folder.")
    if not os.access(path, os.W_OK):
        return False, "coax cannot write to the Channels DVR Imports folder mounted at /imports."
    return True, ""


def safe_name(text, limit=150):
    """A file or folder name every file system takes: no path or reserved characters, ':' as ' -'."""
    text = _BAD_CHARS.sub("", (text or "").replace(":", " -"))
    text = " ".join(text.split()).strip(" .")
    while len(text.encode("utf-8")) > limit:
        text = text[:-1]
    return text.strip(" .") or "Untitled"


def selected(provider):
    vod = provider.get("vod") if isinstance(provider.get("vod"), dict) else {}
    return list(vod.get("movies") or []), list(vod.get("series") or [])


def in_library(provider):
    """Picked groups whose titles also show in the main Movies and TV Shows listings (the rest are Hidden there and
    live in their collections and section): (movie groups, series groups)."""
    vod = provider.get("vod") if isinstance(provider.get("vod"), dict) else {}
    show = vod.get("library") if isinstance(vod.get("library"), dict) else {}
    return set(show.get("movies") or []), set(show.get("series") or [])


def group_label(provider, group):
    from app.names import clean_name
    return "%s · %s" % (provider.get("name") or provider.get("id"), clean_name(group))


def plan(provider, vod, episodes, base):
    """Every file the provider's picks should have: {relative path: content}, per group the paths in it, the titles
    for the log, and the paths shown in the main listings (a title in any group set to show there)."""
    from app.providers import vod as vodcat
    pid = provider["id"]
    movie_groups, series_groups = selected(provider)
    files, members = {}, {("movies", g): [] for g in movie_groups}
    members.update({("series", g): [] for g in series_groups})
    titles = {}
    visible = set()
    lib_movies, lib_series = in_library(provider)
    wanted = set(movie_groups)
    taken = {}   # lower-case path -> the path of the copy that stands for the title
    for movie in vod.get("movies") or []:
        groups = [g for g in movie.get("groups") or [] if g in wanted]
        if not groups:
            continue
        name = safe_name(vodcat.display_name(movie.get("name"), movie.get("year")))
        rel = "Movies/%s/%s/%s.strm" % (SUBDIR, pid, name)
        key = rel.lower()
        if key in taken:
            rel = taken[key]   # the same title twice (another quality or group): the first copy stands for both
        else:
            taken[key] = rel
            files[rel] = "%s/vod/%s/m/%s.%s\n" % (base, pid, movie["id"], movie.get("ext") or "mp4")
            titles[("movie", str(movie["id"]))] = vodcat.display_name(movie.get("name"), movie.get("year"))
        for group in groups:
            if rel not in members[("movies", group)]:
                members[("movies", group)].append(rel)
        if lib_movies.intersection(groups):
            visible.add(rel)
    wanted = set(series_groups)
    show_dirs = {}
    for show in vod.get("series") or []:
        groups = [g for g in show.get("groups") or [] if g in wanted]
        if not groups:
            continue
        show_name = vodcat.display_name(show.get("name"), show.get("year"))
        folder = safe_name(show_name)
        if folder.lower() in show_dirs and show_dirs[folder.lower()] != show["id"]:
            continue   # two series with one name: keep the first, the DVR would merge them anyway
        show_dirs[folder.lower()] = show["id"]
        first = None
        seen = set()
        for ep in sorted(episodes.get(show["id"]) or [], key=lambda e: (e.get("season") or 0, e.get("episode") or 0)):
            season, number = int(ep.get("season") or 0), int(ep.get("episode") or 0)
            if number <= 0 or (season, number) in seen:
                continue
            seen.add((season, number))
            rel = "TV/%s/%s/%s/Season %02d/%s - S%02dE%02d.strm" % (SUBDIR, pid, folder, season, folder, season, number)
            files[rel] = "%s/vod/%s/e/%s.%s\n" % (base, pid, ep["id"], ep.get("ext") or "mp4")
            titles[("episode", str(ep["id"]))] = "%s S%02dE%02d" % (show_name, season, number)
            first = first or rel
            if lib_series.intersection(groups):
                visible.add(rel)
        if first:
            for group in groups:
                members[("series", group)].append(first)   # a show collection holds the show: any episode finds it
    return files, members, titles, visible


class Library:
    """Writes the Stream Files, keeps the DVR's collections and sections, and remembers what it did."""

    def __init__(self, store, data_dir, imports_dir=None, dvr_call=None):
        self.store = store
        self.imports_dir = imports_dir or IMPORTS_DIR
        self.state_path = os.path.join(data_dir, "cache", "vod-state.json")
        self._dvr_call = dvr_call
        self._lock = threading.Lock()          # one sync at a time
        self._state_lock = threading.Lock()
        self._wake = threading.Event()
        self._wanted = set()
        self._stop = threading.Event()
        self._thread = None
        self._titles = {}
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

    def _pstate(self, pid):
        return self.state.setdefault(str(pid), {"collections": {}, "section": None})

    def title(self, kind, item_id):
        return self._titles.get((kind, str(item_id)))

    def status(self, provider_id):
        ok, why = imports_ready(self.imports_dir)
        st = dict(self.state.get(str(provider_id)) or {})
        st.pop("collections", None)
        st.update(imports_ok=ok, imports_message=why, running=self.running.get(str(provider_id)))
        return st

    # -- the DVR ----------------------------------------------------------------------------------------------------
    def _call(self, cfg, method, path, body=None, timeout=30):
        if self._dvr_call is not None:
            return self._dvr_call(cfg, method, path, body)
        from app import channels_dvr
        return channels_dvr._call(cfg, method, path, body, timeout=timeout)

    def _scan(self, cfg, prune):
        """Ask the DVR to import the new files (and drop the deleted ones), then wait for its scanner."""
        if prune:
            self._call(cfg, "PUT", "/dvr/scanner/imports/prune")
        self._call(cfg, "PUT", "/dvr/scanner/scan")
        deadline = time.monotonic() + SCAN_WAIT
        time.sleep(SCAN_POLL if self._dvr_call is None else 0)
        while time.monotonic() < deadline and not self._stop.is_set():
            info = self._call(cfg, "GET", "/dvr") or {}
            busy = [k for k in (info.get("activity") or {}) if "-scanner" in k or "-pruner" in k]
            if not busy:
                return True
            time.sleep(SCAN_POLL if self._dvr_call is None else 0)
        return False

    def _dvr_ids(self, cfg, pid):
        """DVR ids of this provider's imported files: {relative path: file id} for movies, and
        {relative path: (file id, show id)} for episodes."""
        movies, episodes = {}, {}
        mark = "Imports/"
        for kind in ("movies", "episodes"):
            for item in self._call(cfg, "GET", "/api/v1/%s?source=stream-files" % kind, timeout=300) or []:
                path = str(item.get("path") or "")
                at = path.find(mark)
                if at < 0:
                    continue
                rel = path[at + len(mark):]
                if not rel.startswith(("Movies/%s/%s/" % (SUBDIR, pid), "TV/%s/%s/" % (SUBDIR, pid))):
                    continue
                if kind == "movies":
                    movies[rel] = str(item.get("id") or "")
                else:
                    episodes[rel] = (str(item.get("id") or ""), str(item.get("show_id") or ""))
        return movies, episodes

    def _hide(self, cfg, pid, movie_ids, episodes, visible):
        """Set each title's visibility in the main Movies and TV Shows listings from its groups' choice: Hidden (it
        lives in its collections and section) or shown with the user's own library. Only changes are sent; coax
        remembers what it hid, so switching a group back shows its titles again. A show is hidden as a whole only
        when every one of its files is coax's and hidden; a show of the user's own with coax episodes in it stays."""
        st = self._pstate(pid)
        done = set(st.get("hidden") or [])
        files = {fid: rel for rel, fid in movie_ids.items()}
        files.update({fid: rel for rel, (fid, _show) in episodes.items()})
        want = {fid for fid, rel in files.items() if rel not in visible}
        for fid in sorted(want - done):
            self._call(cfg, "PUT", "/dvr/files/%s/visibility/hidden" % fid)
        for fid in sorted((done - want) & set(files)):
            self._call(cfg, "PUT", "/dvr/files/%s/visibility/main" % fid)
        shows_done = set(st.get("hidden_shows") or [])
        by_show = {}
        for rel, (fid, show) in episodes.items():
            if show:
                by_show.setdefault(show, set()).add(fid)
        hide_shows = set()
        for show, fids in sorted(by_show.items()):
            if fids <= want:
                if show in shows_done:
                    hide_shows.add(show)
                    continue
                group = self._call(cfg, "GET", "/dvr/groups/%s" % show) or {}
                if set(str(f) for f in group.get("FileID") or []) <= set(fids):
                    self._call(cfg, "PUT", "/dvr/groups/%s/visibility/hidden" % show)
                    hide_shows.add(show)
            elif show in shows_done:
                self._call(cfg, "PUT", "/dvr/groups/%s/visibility/main" % show)
        st["hidden"] = sorted(want)
        st["hidden_shows"] = sorted(hide_shows)

    def _collections(self, cfg, provider, members, visible):
        """One DVR collection per picked group, one Personal Section with a row for each."""
        pid = provider["id"]
        st = self._pstate(pid)
        movie_ids, episodes = self._dvr_ids(cfg, pid)
        show_ids = {rel: show for rel, (_fid, show) in episodes.items()}
        self._hide(cfg, pid, movie_ids, episodes, visible)
        existing = {str(c.get("ID")): c for c in self._call(cfg, "GET", "/dvr/collections/content") or []}
        kept = {}
        rows = []
        for (kind, group), paths in members.items():
            ids = movie_ids if kind == "movies" else show_ids
            items = list(dict.fromkeys(ids[p] for p in paths if ids.get(p)))
            key = "%s:%s" % (kind, group)
            name = group_label(provider, group)
            cid = (st.get("collections") or {}).get(key)
            body = {"Name": name, "Type": "movies" if kind == "movies" else "shows", "Items": items}
            if cid and cid in existing:
                current = existing[cid]
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

    # -- files ------------------------------------------------------------------------------------------------------
    def _roots(self, pid):
        return [os.path.join(self.imports_dir, "Movies", SUBDIR, str(pid)),
                os.path.join(self.imports_dir, "TV", SUBDIR, str(pid))]

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
        """Make the provider's folders hold exactly these files. Returns (written, deleted)."""
        existing = self._existing(pid)
        written = deleted = 0
        for rel, content in files.items():
            full = os.path.join(self.imports_dir, *rel.split("/"))
            if rel in existing:
                try:
                    with open(full, encoding="utf-8") as handle:
                        if handle.read() == content:
                            continue
                except OSError:
                    pass
            os.makedirs(os.path.dirname(full), exist_ok=True)
            tmp = full + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                handle.write(content)
            os.chmod(tmp, 0o666)
            os.replace(tmp, full)
            written += 1
        for rel, full in existing.items():
            if rel not in files:
                try:
                    os.unlink(full)
                    deleted += 1
                except OSError as exc:
                    log.warning("VOD: could not remove %s: %s", rel, exc)
        for root in self._roots(pid):
            folders = [w[0] for w in os.walk(root)] + [os.path.dirname(root)]   # down to Movies/coax, TV/coax
            for folder in sorted(folders, key=len, reverse=True):
                try:
                    if not os.listdir(folder):
                        os.rmdir(folder)
                except OSError:
                    pass
        return written, deleted

    # -- sync -------------------------------------------------------------------------------------------------------
    def sync(self, provider_id):
        """Bring one provider's Stream Files, collections and section up to date. Never raises."""
        pid = str(provider_id)
        with self._lock:
            started = time.time()
            st = self._pstate(pid)
            try:
                self._sync(pid)
                st.update(error="", last_sync=started)
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

    def _sync(self, pid):
        from app.providers import vod as vodcat, catalog
        cfg = self.store.get()
        provider = next((p for p in cfg.get("providers") or [] if p.get("id") == pid), None)
        movie_groups, series_groups = selected(provider or {})
        if provider is None or not (movie_groups or series_groups):
            self.remove(pid, cfg)
            return
        if provider.get("enabled", True) is False:
            return   # switched off is a pause: the library stays (watch state, resume), nothing is fetched
        ok, why = imports_ready(self.imports_dir)
        if not ok:
            raise RuntimeError(why)
        base = ((cfg.get("server") or {}).get("base_url") or "").strip().rstrip("/")
        if not base:
            raise RuntimeError("set coax's Base URL in Settings first: the Stream Files point at it")
        self.running[pid] = "reading the provider's movies and series"
        vod = vodcat.load(pid)
        hours = provider.get("refresh_hours") or 24
        age = catalog.age_hours(vod) if vod else None
        if (provider.get("type") or "xtream") == "xtream" and (vod is None or age is None or age >= hours):
            vod = vodcat.refresh(provider)
        vod = vod or {}
        wanted = set(series_groups)
        shows = [s for s in vod.get("series") or [] if wanted.intersection(s.get("groups") or [])]

        def progress(done, total):
            self.running[pid] = "reading episodes (%d of %d series)" % (done, total)
        episodes = vodcat.episodes_for(provider, shows, deadline=time.monotonic() + EPISODE_BUDGET,
                                       progress=progress) if shows else {}
        files, members, titles, visible = plan(provider, vod, episodes, base)
        self._titles.update(titles)
        self.running[pid] = "writing Stream Files"
        written, deleted = self._write(pid, files)
        st = self._pstate(pid)
        st.update(movies=sum(1 for r in files if r.startswith("Movies/")),
                  episodes=sum(1 for r in files if r.startswith("TV/")),
                  shows=len({r.split("/")[3] for r in files if r.startswith("TV/")}))
        log.info("VOD provider %s: %d movies, %d episodes of %d shows (%d written, %d removed)", pid,
                 st["movies"], st["episodes"], st["shows"], written, deleted)
        if not ((cfg.get("channels_dvr") or {}).get("url") or "").strip():
            st["dvr_note"] = "Set the Channels DVR URL in Settings so coax can make the collections."
            return
        st.pop("dvr_note", None)
        if written or deleted or not st.get("collections"):
            self.running[pid] = "waiting for Channels DVR to import them"
            if not self._scan(cfg, prune=bool(deleted)):
                log.warning("VOD provider %s: Channels DVR is still scanning; collections updated anyway", pid)
        self.running[pid] = "updating the collections"
        self._collections(cfg, provider, members, visible)

    def remove(self, pid, cfg=None):
        """Take a provider's VOD out of the DVR: its files, collections and section."""
        cfg = cfg or self.store.get()
        pid = str(pid)
        st = self.state.get(pid)
        if os.path.isdir(self.imports_dir):
            _written, deleted = self._write(pid, {})
        else:
            deleted = 0
        if st and ((cfg.get("channels_dvr") or {}).get("url") or "").strip():
            try:
                existing = {str(c.get("ID")) for c in self._call(cfg, "GET", "/dvr/collections/content") or []}
                for cid in (st.get("collections") or {}).values():
                    if cid in existing:
                        self._call(cfg, "DELETE", "/dvr/collections/content/%s" % cid)
                sections = {str(s.get("ID")) for s in self._call(cfg, "GET", "/dvr/custom_sections") or []}
                if st.get("section") in sections:
                    self._call(cfg, "DELETE", "/dvr/custom_sections/%s" % st["section"])
                if deleted:
                    self._call(cfg, "PUT", "/dvr/scanner/imports/prune")
            except Exception as exc:  # noqa: BLE001
                log.warning("VOD: could not remove provider %s from Channels DVR: %s", pid, exc)
        if st or deleted:
            log.info("VOD provider %s removed from Channels DVR (%d Stream Files)", pid, deleted)
        self.state.pop(pid, None)

    # -- schedule ---------------------------------------------------------------------------------------------------
    def request(self, provider_id=None):
        """Sync one provider (or every provider with picks) soon."""
        cfg = self.store.get()
        ids = [provider_id] if provider_id else [p["id"] for p in cfg.get("providers") or []]
        self._wanted.update(str(i) for i in ids)
        self._wake.set()

    def _due(self, provider):
        movie_groups, series_groups = selected(provider)
        st = self.state.get(provider["id"]) or {}
        if not (movie_groups or series_groups):
            return bool(st)   # picks cleared: clean up once
        hours = provider.get("refresh_hours") or 24
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

    def start(self):
        def loop():
            while not self._stop.is_set():
                try:
                    self.run_once()
                except Exception:  # noqa: BLE001
                    log.exception("VOD scheduler pass failed")
                self._wake.wait(TICK_SECONDS)
                self._wake.clear()
        self._thread = threading.Thread(target=loop, name="vod", daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        self._wake.set()
