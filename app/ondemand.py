"""The On demand page: the available groups' titles (Catalog), what goes to Channels DVR (Content), and the
unsaved changes (the draft).

No database: the catalog is the provider's own cached movie and series lists (app.providers.vod), read once into
memory per provider and read again only when the provider's list file changes. The Content list is that catalog
seen through the provider's choices (Add all groups and hand-picks, in config.json) and the library's record of
what is already in Channels DVR (vod-state.json). The draft is one small JSON file, like the Channels page's held
Auto-match suggestions: kept on the server until Save or Dismiss, versioned so a stale tab cannot save over it.
"""

import json
import os
import re
import sys
import threading
import time
import unicodedata

from app import config as configmod
from app import vod as vodlib

_ARTICLE = re.compile(r"^(the|a|an)\s+")
_NOT_WORD = re.compile(r"[^0-9a-z]+")

MAX_LIMIT = 500


def norm(text):
    """Search form: lower case, no accents or punctuation, no leading 'The', 'A' or 'An'."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = " ".join(_NOT_WORD.sub(" ", text).split())
    return _ARTICLE.sub("", text)


def query_words(text):
    """A search box's words, in the same form (a leading article is dropped, so 'the heat' finds 'Heat')."""
    return norm(text).split()


class Title:
    """One copy of a title in a provider's catalog. Kept small: a big provider lists well over 100,000 of them."""
    __slots__ = ("kind", "id", "name", "year", "groups", "quality", "hay", "added", "dup")

    def __init__(self, kind, item, name, year, added):
        self.kind = kind                       # "m" movie, "s" series
        self.id = str(item.get("id"))
        self.name = name
        self.year = sys.intern(year or "")
        self.groups = tuple(sys.intern(g) for g in item.get("groups") or ())   # a few names shared by all titles
        self.quality = vodlib.quality(item.get("name"), self.groups)
        self.hay = ("%s %s" % (norm(name), self.year)).strip()
        self.added = added
        # The plan's own name for the title (two copies with one name are one title in Channels DVR), as a hash.
        self.dup = hash(vodlib.safe_name("%s (%s)" % (name, year) if year else name).lower())

    @property
    def key(self):
        return "%s:%s" % (self.kind, self.id)


class Catalogs:
    """Each provider's titles in memory, read again when its list file changes."""

    def __init__(self):
        self._lock = threading.Lock()
        self._building = {}   # pid -> lock: one read of a provider's list at a time (it is tens of megabytes)
        self._by_pid = {}     # pid -> ((mtime, size), [Title], {key: Title})

    def get(self, pid):
        from app.providers import vod as vodcat
        path = vodcat.cache_path(pid)
        with self._lock:
            building = self._building.setdefault(pid, threading.Lock())
        with building:
            try:
                st = os.stat(path)
                stamp = (st.st_mtime, st.st_size)
            except OSError:
                stamp = None
            with self._lock:
                hit = self._by_pid.get(pid)
            if hit and hit[0] == stamp:
                return hit[1], hit[2]
            return self._build(pid, stamp, vodcat)

    def _build(self, pid, stamp, vodcat):
        vod = vodcat.load(pid) if stamp else None
        titles = []
        for movie in (vod or {}).get("movies") or []:
            if movie.get("id") is None:
                continue
            name, year = vodcat.title_year(movie.get("name"), movie.get("year"))
            titles.append(Title("m", movie, name, year, movie.get("added") or 0))
        for show in (vod or {}).get("series") or []:
            if show.get("id") is None:
                continue
            name, year = vodcat.title_year(show.get("name"), show.get("year") or "")
            titles.append(Title("s", show, name, year, show.get("modified") or 0))
        titles.sort(key=lambda t: -(_num(t.added)))   # newest first
        by_key = {t.key: t for t in titles}
        with self._lock:
            self._by_pid[pid] = (stamp, titles, by_key)
        return titles, by_key

    def forget(self, pid):
        with self._lock:
            self._by_pid.pop(pid, None)


def _num(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


class Choices:
    """One provider's choices, read from its config: available groups, Add all groups, hand-picks."""

    def __init__(self, provider):
        m, s = vodlib.selected(provider)
        self.avail = {"m": set(m), "s": set(s)}
        am, as_ = vodlib.add_all(provider)
        self.all = {"m": set(am), "s": set(as_)}
        pm, ps = vodlib.picks(provider)
        self.picks = {"m": pm, "s": ps}

    def offered(self, t):
        """In an available group: the Catalog lists it."""
        return bool(self.avail[t.kind].intersection(t.groups))

    def via(self, t):
        """'all' (an Add all group carries it), 'pick' (hand-picked) or '' (not chosen)."""
        if self.all[t.kind].intersection(t.groups):
            return "all"
        if t.id in self.picks[t.kind] and self.offered(t):
            return "pick"
        return ""


def wanted_counts(titles, choices, extra=()):
    """How many separate copies of each title go to Channels DVR: {dup name: count}. Add all copies of one title
    are one copy (the plan files them as one); each hand-pick of another copy is one more. `extra`: Titles the
    draft adds."""
    seen_all, counts = set(), {}
    for t in titles:
        via = choices.via(t)
        if via == "all":
            if t.dup in seen_all:
                continue
            seen_all.add(t.dup)
        elif via != "pick":
            continue
        counts[t.dup] = counts.get(t.dup, 0) + 1
    for t in extra:
        counts[t.dup] = counts.get(t.dup, 0) + 1
    return counts


def matches(t, kind, group, words):
    if kind and t.kind != kind:
        return False
    if group and group not in t.groups:
        return False
    return all(w in t.hay for w in words)


def row(pid, t, choices, present, counts, draft):
    via = choices.via(t)
    change = draft.get(t.key, "")
    in_dvr = t.key in present
    wanted = bool(via)
    n = counts.get(t.dup, 0)
    return {
        "provider": pid, "key": t.key, "kind": "movie" if t.kind == "m" else "series", "id": t.id,
        "name": t.name, "year": t.year, "groups": list(t.groups), "quality": t.quality,
        "via": via,                                    # all | pick | ""
        "in_channels": in_dvr and wanted,
        "pending": wanted and not in_dvr,              # chosen and saved, on its way to Channels DVR
        "draft": change,                               # add | remove | "" (unsaved)
        # Another copy of the same title is (or will be) in Channels DVR as well.
        "duplicate": (n >= 2 and wanted and change != "remove") or (change == "add" and n >= 2),
        "added": t.added,
    }


class Draft:
    """The unsaved changes of the On demand page, on disk until Save or Dismiss.

    {"rev": n, "pending": {provider id: {"m:<id>" | "s:<id>": "add" | "remove"}}, "saved_at": iso}"""

    def __init__(self, data_dir):
        self.path = os.path.join(data_dir, "ondemand-draft.json")
        self.lock = threading.Lock()
        self.data = self._read()

    def _read(self):
        try:
            with open(self.path, "rb") as handle:
                data = json.loads(handle.read() or b"{}")
        except FileNotFoundError:
            return {"rev": 0, "pending": {}}
        except (OSError, ValueError):
            return {"rev": 0, "pending": {}}
        if not isinstance(data, dict) or not isinstance(data.get("pending"), dict) or \
                not isinstance(data.get("rev", 0), int) or isinstance(data.get("rev"), bool):
            return {"rev": 0, "pending": {}}
        return data

    def write(self, pending, saved_at):
        """Store a new version (the caller holds the lock)."""
        data = {"rev": self.data.get("rev", 0) + 1, "pending": pending, "saved_at": saved_at}
        configmod.atomic_write(self.path, json.dumps(data).encode())
        self.data = data
        return data


KEY = re.compile(r"^[ms]:[A-Za-z0-9_.-]{1,64}$")
PID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def valid_pending(pending):
    if not isinstance(pending, dict):
        return False
    for pid, changes in pending.items():
        if not PID.match(str(pid)) or not isinstance(changes, dict):
            return False
        for key, change in changes.items():
            if not KEY.match(str(key)) or change not in ("add", "remove"):
                return False
    return True


def tidy(pending, cfg, catalogs):
    """The draft as it can still be saved: changes to titles the provider no longer offers (gone from its list, or
    their group no longer available), adds of titles already chosen and removes of titles not chosen are dropped.
    Returns (pending, notes): notes say what was dropped and why."""
    out, gone, done = {}, 0, 0
    providers = {p.get("id"): p for p in cfg.get("providers") or []}
    for pid, changes in (pending or {}).items():
        provider = providers.get(pid)
        if provider is None:
            gone += len(changes)
            continue
        _titles, by_key = catalogs.get(pid)
        choices = Choices(provider)
        kept = {}
        for key, change in changes.items():
            t = by_key.get(key)
            if t is None or not choices.offered(t):
                gone += 1
                continue
            via = choices.via(t)
            if (change == "add" and via) or (change == "remove" and via != "pick"):
                done += 1   # already so (saved elsewhere, or an Add all group took the title in)
                continue
            kept[key] = change
        if kept:
            out[pid] = kept
    notes = []
    if gone:
        notes.append("%d unsaved %s dropped: the provider no longer offers %s." % (
            gone, "change was" if gone == 1 else "changes were", "that title" if gone == 1 else "those titles"))
    if done:
        notes.append("%d unsaved %s no longer needed (already in place)." % (
            done, "change is" if done == 1 else "changes are"))
    return out, notes


def apply(cfg, pending):
    """Write a draft's adds and removes into the providers' hand-picks (in place). Returns {pid: (added, removed)}."""
    result = {}
    for provider in cfg.get("providers") or []:
        changes = pending.get(provider.get("id")) or {}
        if not changes:
            continue
        vod = vodlib.materialize(provider.get("vod"))
        picks = {"m": [str(i) for i in vod["picks"].get("movies") or []],
                 "s": [str(i) for i in vod["picks"].get("series") or []]}
        have = {"m": set(picks["m"]), "s": set(picks["s"])}
        dropped = {"m": set(), "s": set()}
        added = removed = 0
        for key, change in changes.items():
            kind, ident = key.split(":", 1)
            if change == "add" and ident not in have[kind]:
                picks[kind].append(ident)
                have[kind].add(ident)
                added += 1
            elif change == "remove" and ident in have[kind]:
                have[kind].discard(ident)
                dropped[kind].add(ident)
                removed += 1
        vod["picks"] = {"movies": [i for i in picks["m"] if i not in dropped["m"]],
                        "series": [i for i in picks["s"] if i not in dropped["s"]]}
        provider["vod"] = vod
        result[provider["id"]] = (added, removed)
    return result


def set_add_all(cfg, pid, kind, group, on, titles):
    """Switch a group's Add all on or off (in place). On: the group's hand-picks become the group's own (they leave
    the picks; the group carries them now). Off: the whole group leaves Channels DVR, including titles that were
    hand-picked before Add all was switched on (any pick left in the group goes too). `titles`: the provider's
    catalog (Catalogs.get), read before the config is locked."""
    for provider in cfg.get("providers") or []:
        if provider.get("id") != pid:
            continue
        vod = vodlib.materialize(provider.get("vod"))
        name = "movies" if kind == "m" else "series"
        if group not in (vod.get(name) or []):
            raise ValueError("that group is not available; make it available on the Providers page first")
        groups = [g for g in vod["all"].get(name) or [] if g != group]
        if on:
            groups.append(group)
        inside = {t.id for t in titles if t.kind == kind and group in t.groups}
        vod["picks"][name] = [i for i in vod["picks"].get(name) or [] if str(i) not in inside]
        vod["all"][name] = groups
        provider["vod"] = vod
        return
    raise KeyError(pid)


def estimate(count, per_minute=150):
    """'about 11 hours' for this many titles at the measured import pace."""
    minutes = count / float(per_minute)
    if minutes < 1.5:
        return "about a minute"
    if minutes < 90:
        return "about %d minutes" % round(minutes)
    hours = minutes / 60.0
    if hours < 36:
        return "about %d hours" % round(hours)
    return "about %d days" % round(hours / 24.0)


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
