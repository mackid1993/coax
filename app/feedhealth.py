"""Feed health: learn which streams of a channel are bad and act on it, then tell the user (the bell).

Owning the whole pipeline means coax sees every failure of every stream. This module keeps a rolling
day of that history per channel stream (in <data>/cache/feedhealth.json) and makes three decisions:

- Out-of-sync stream: the engine compared a stream it just switched to against the one it left and found
  the sound several seconds off the picture. That stream is removed from the channel at once.
- Constantly failing backup: a backup that failed in REMOVE_FAILS separate half hours of the last day, and
  played well for less than REMOVE_MAX_GOOD minutes, while another stream of the channel played well,
  is removed.
- Unreliable main stream: when the main stream failed in PROMOTE_FAILS separate half hours of the last day
  and a backup played at least PROMOTE_GOOD minutes without a single failure, that backup becomes the main
  stream (the old main stream moves to second place). At most once a day per channel.

A channel is never left with fewer than one stream, and a channel with one stream is never changed.
Every change is recorded as a notification (<data>/notifications.json) with an Undo. Never raises.
"""

import copy
import json
import logging
import os
import threading
import time

log = logging.getLogger("coax.health")

DAY = 24 * 3600
FAIL_WINDOW = 1800        # failures of one stream within half an hour count once
REMOVE_FAILS = 6          # failure half hours in a day that make a backup constantly failing
REMOVE_MAX_GOOD = 20      # ... when it played well for fewer minutes than this that day
OTHER_GOOD = 60           # ... and another stream of the channel played well at least this long
PROMOTE_FAILS = 3         # failure half hours in a day that make a main stream unreliable
PROMOTE_GOOD = 60         # minutes a backup must have played well, with no failure, to take over
PROMOTE_COOLDOWN = DAY
MAX_NOTES = 200


def _load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, type(default)) else default
    except (OSError, ValueError):
        return default


def _save(path, data):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except OSError as exc:
        log.warning("could not save %s: %s", path, exc)


def _key(stream):
    return (str(stream.get("provider")), str(stream.get("stream_id")))


class FeedHealth:
    def __init__(self, store, data_dir):
        self.store = store
        self.path = os.path.join(data_dir, "cache", "feedhealth.json")
        self.npath = os.path.join(data_dir, "notifications.json")
        self.lock = threading.RLock()
        self.feeds = _load(self.path, {})
        self.notes = _load(self.npath, [])

    # -- history --------------------------------------------------------------------

    @staticmethod
    def _k(ch, key):
        return "%s|%s|%s" % (ch, key[0], key[1])

    def _rec(self, ch, key):
        return self.feeds.setdefault(self._k(ch, key), {"fails": [], "good": []})

    def _stats(self, ch, key, now):
        rec = self.feeds.get(self._k(ch, key)) or {}
        fails = [t for t in rec.get("fails", []) if now - t < DAY]
        good = sum(m for t, m in rec.get("good", []) if now - t < DAY)
        return len(fails), good

    def _save_feeds(self, now):
        for k in list(self.feeds):
            if k.startswith("_"):
                continue
            rec = self.feeds[k]
            rec["fails"] = [t for t in rec.get("fails", []) if now - t < DAY]
            rec["good"] = [g for g in rec.get("good", []) if now - g[0] < DAY]
            if not rec["fails"] and not rec["good"]:
                del self.feeds[k]
        _save(self.path, self.feeds)

    def fail(self, ch, key, now=None):
        now = now or time.time()
        with self.lock:
            rec = self._rec(ch, key)
            if not rec["fails"] or now - rec["fails"][-1] >= FAIL_WINDOW:
                rec["fails"].append(now)
            self._save_feeds(now)
            self._decide(ch, now)

    def good(self, ch, key, minutes, now=None):
        now = now or time.time()
        with self.lock:
            self._rec(ch, key)["good"].append([now, minutes])
            self._save_feeds(now)
            self._decide(ch, now)

    def desync(self, ch, key, seconds, ahead, now=None):
        now = now or time.time()
        with self.lock:
            self._remove(ch, key, "its sound was %.0f seconds %s its picture" % (seconds, "ahead of" if ahead else "behind"),
                         "desync", now)

    # -- decisions ------------------------------------------------------------------

    def _channel(self, cfg, ch):
        return next((c for c in cfg.get("channels") or [] if str(c.get("id")) == str(ch)), None)

    def _decide(self, ch, now):
        try:
            chan = self._channel(self.store.get(), ch)
            if chan is None or len(chan.get("streams") or []) < 2:
                return
            keys = [_key(s) for s in chan["streams"]]
            st = {k: self._stats(ch, k, now) for k in keys}
            for k in keys[1:]:
                fails, good = st[k]
                if fails >= REMOVE_FAILS and good < REMOVE_MAX_GOOD and \
                        any(st[o][1] >= OTHER_GOOD for o in keys if o != k):
                    self._remove(ch, k, "it failed in %d separate half hours of the last day and rarely played" % fails,
                                 "removed", now)
                    return
            fails0 = st[keys[0]][0]
            last = (self.feeds.get("_promoted") or {}).get(str(ch), 0)
            if fails0 >= PROMOTE_FAILS and now - last >= PROMOTE_COOLDOWN:
                cands = [k for k in keys[1:] if st[k][0] == 0 and st[k][1] >= PROMOTE_GOOD]
                if cands:
                    best = max(cands, key=lambda k: st[k][1])
                    self._promote(ch, best, "the main stream failed in %d separate half hours of the last day, and "
                                  "this backup played %d minutes without a problem" % (fails0, st[best][1]), now)
        except Exception:  # noqa: BLE001 - feed health never takes coax down
            log.exception("feed health decision failed")

    def _remove(self, ch, key, reason, kind, now):
        done = {}

        def mutate(cfg):
            chan = self._channel(cfg, ch)
            if chan is None:
                return None
            streams = chan.get("streams") or []
            for i, s in enumerate(streams):
                if _key(s) == key:
                    if len(streams) < 2:
                        return None
                    done.update(index=i, stream=copy.deepcopy(s), name=chan.get("name"), main=i == 0)
                    del streams[i]
                    return True
            return None
        try:
            if not self.store.update(mutate):
                return
        except Exception:  # noqa: BLE001
            log.exception("could not remove a stream")
            return
        self.feeds.pop(self._k(ch, key), None)
        self._save_feeds(now)
        which = "main stream" if done["main"] else "backup stream %d" % (done["index"] + 1)
        text = "Removed %s (%s %s) because %s." % (which, key[0], key[1], reason)
        log.warning("[%s] %s", done["name"], text)
        self._note(kind, ch, done["name"], key, text, {"index": done["index"], "stream": done["stream"]}, now)

    def _promote(self, ch, key, reason, now):
        done = {}

        def mutate(cfg):
            chan = self._channel(cfg, ch)
            if chan is None:
                return None
            streams = chan.get("streams") or []
            i = next((j for j, s in enumerate(streams) if _key(s) == key), None)
            if not i:
                return None
            done.update(order=[_key(s) for s in streams], name=chan.get("name"), index=i)
            streams.insert(0, streams.pop(i))
            return True
        try:
            if not self.store.update(mutate):
                return
        except Exception:  # noqa: BLE001
            log.exception("could not promote a stream")
            return
        self.feeds.setdefault("_promoted", {})[str(ch)] = now
        self._save_feeds(now)
        text = "Made backup stream %d (%s %s) the main stream because %s." % (done["index"] + 1, key[0], key[1], reason)
        log.warning("[%s] %s", done["name"], text)
        self._note("promoted", ch, done["name"], key, text, {"order": done["order"]}, now)

    # -- notifications ----------------------------------------------------------------

    def _note(self, kind, ch, name, key, text, undo, now):
        nid = str(int(now * 1000))
        while any(n["id"] == nid for n in self.notes):
            nid = str(int(nid) + 1)
        self.notes.insert(0, {"id": nid, "t": now, "kind": kind, "channel_id": ch, "channel": name,
                              "provider": key[0], "stream_id": key[1], "text": text, "undo": undo,
                              "undone": False, "read": False})
        del self.notes[MAX_NOTES:]
        _save(self.npath, self.notes)

    def list(self):
        with self.lock:
            return {"items": copy.deepcopy(self.notes), "unread": sum(1 for n in self.notes if not n.get("read"))}

    def mark_read(self):
        with self.lock:
            for n in self.notes:
                n["read"] = True
            _save(self.npath, self.notes)
            return self.list()

    def undo(self, nid):
        with self.lock:
            note = next((n for n in self.notes if n["id"] == str(nid)), None)
            if note is None or note.get("undone") or not note.get("undo"):
                raise KeyError(nid)
            u = note["undo"]
            key = (note["provider"], note["stream_id"])

            def mutate(cfg):
                chan = self._channel(cfg, note["channel_id"])
                if chan is None:
                    return None
                streams = chan.setdefault("streams", [])
                if "stream" in u:
                    if any(_key(s) == key for s in streams):
                        return True
                    streams.insert(min(int(u["index"]), len(streams)), u["stream"])
                    return True
                order = [tuple(x) for x in u.get("order") or []]
                if sorted(order) == sorted(_key(s) for s in streams):
                    by = {_key(s): s for s in streams}
                    chan["streams"] = [by[k] for k in order]
                return True
            if not self.store.update(mutate):
                raise KeyError(nid)
            self.feeds.pop(self._k(note["channel_id"], key), None)
            note["undone"] = True
            note["read"] = True
            _save(self.npath, self.notes)
            _save(self.path, self.feeds)
            return note
