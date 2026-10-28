"""Feed health: keep each channel's streams in their most stable order, all the time.

coax sees every problem on every stream it plays. Each problem is a strike against that stream:

  desync     sound seconds off the picture (content check after a switch)      weight 10, demote now
  dead       a stream failed or stopped answering (engine gave up on it)       weight 3,  demote now
  placeholder  the provider served an offline slate instead of the channel     weight 3,  demote now
  novideo    coax had to retune because no video came for 90 s                 weight 3
  frozen     the engine stopped sending anything for 25 s                      weight 2
  avsplit    the feed's own audio and video timestamps came apart              weight 2
  stall      the playlist stalled long enough to need the reserve              weight 1
  keepalive  video stopped long enough that coax had to hold the connection    weight 1

and every clean 10 minutes of playback is credit. A stream's score is its weighted strikes over the last
day, each fading with a 6 hour half-life, minus a little for clean minutes. Lower is better.

What coax does with it, right away:
- Demote: a stream with a "demote now" problem moves below every stream that has not had one in the last
  hour. If that stream was the main one, the best of the others becomes the main stream.
- Reorder: whenever a strike lands, the streams are kept in score order (the main stream keeps its place
  unless another stream scores clearly better, so a single hiccup does not reshuffle the channel).
- Remove: a backup that stayed bad for a long time (score >= REMOVE_SCORE with strikes on at least
  REMOVE_HOURS separate hours of the last day, and less than 20 clean minutes) is removed, as is a stream
  whose sound was found off its picture twice. A channel always keeps at least one stream, and a channel
  with one stream is never changed.

A stream that recovers earns its way back up as its strikes fade. Every change is written to the bell
(<data>/notifications.json, with Undo) and as a note on the channel (feed_notes in <data>/cache/feedhealth.json).
Never raises.
"""

import copy
import json
import logging
import math
import os
import threading
import time

log = logging.getLogger("coax.health")

DAY = 24 * 3600
HALF_LIFE = 6 * 3600
WEIGHT = {"desync": 10.0, "dead": 3.0, "placeholder": 3.0, "novideo": 3.0, "frozen": 2.0, "avsplit": 2.0,
          "stall": 1.0, "keepalive": 1.0}
DEMOTE_NOW = {"desync", "dead", "placeholder"}
DEMOTE_WINDOW = 3600       # a stream hit by a "demote now" problem in the last hour sorts below the rest
SAME_KIND_GAP = 120        # the same problem on the same stream within 2 minutes counts once
CLEAN_CREDIT = 0.05        # score taken off per clean minute (10 clean minutes undo half a stall)
PROMOTE_MARGIN = 3.0       # a backup must score this much better than the main stream to replace it
REMOVE_SCORE = 12.0
REMOVE_HOURS = 6
REMOVE_MAX_GOOD = 20
DESYNC_REMOVE = 2
REORDER_GAP = 300          # at most one score reorder per channel every 5 minutes
MAX_NOTES = 200
CHANNEL_NOTES = 20

TEXT = {"desync": "sound was off its picture", "dead": "stopped working", "placeholder": "showed an offline slate",
        "novideo": "sent no video", "frozen": "froze", "avsplit": "had audio and video come apart",
        "stall": "stalled", "keepalive": "dropped video"}


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
        data = _load(self.path, {})
        self.feeds = data.get("feeds") if isinstance(data.get("feeds"), dict) else {}
        self.chnotes = data.get("notes") if isinstance(data.get("notes"), dict) else {}
        self.reordered = data.get("reordered") if isinstance(data.get("reordered"), dict) else {}
        self.notes = _load(self.npath, [])

    # -- history ------------------------------------------------------------------------

    @staticmethod
    def _k(ch, key):
        return "%s|%s|%s" % (ch, key[0], key[1])

    def _rec(self, ch, key):
        return self.feeds.setdefault(self._k(ch, key), {"ev": [], "good": []})

    def score(self, ch, key, now):
        rec = self.feeds.get(self._k(ch, key)) or {}
        s = 0.0
        for t, kind in rec.get("ev", []):
            if now - t < DAY:
                s += WEIGHT.get(kind, 1.0) * math.pow(0.5, (now - t) / HALF_LIFE)
        good = sum(m for t, m in rec.get("good", []) if now - t < DAY)
        return max(s - CLEAN_CREDIT * good, 0.0)

    def _recent(self, ch, key, now, kinds, window):
        rec = self.feeds.get(self._k(ch, key)) or {}
        return any(now - t < window and k in kinds for t, k in rec.get("ev", []))

    def _hours(self, ch, key, now):
        rec = self.feeds.get(self._k(ch, key)) or {}
        return len({int(t // 3600) for t, _ in rec.get("ev", []) if now - t < DAY})

    def _good(self, ch, key, now):
        rec = self.feeds.get(self._k(ch, key)) or {}
        return sum(m for t, m in rec.get("good", []) if now - t < DAY)

    def _count(self, ch, key, now, kind):
        rec = self.feeds.get(self._k(ch, key)) or {}
        return sum(1 for t, k in rec.get("ev", []) if k == kind and now - t < DAY)

    def _persist(self, now):
        for k in list(self.feeds):
            rec = self.feeds[k]
            rec["ev"] = [e for e in rec.get("ev", []) if now - e[0] < DAY]
            rec["good"] = [g for g in rec.get("good", []) if now - g[0] < DAY]
            if not rec["ev"] and not rec["good"]:
                del self.feeds[k]
        _save(self.path, {"feeds": self.feeds, "notes": self.chnotes, "reordered": self.reordered})

    # -- signals from the hub --------------------------------------------------------------

    def problem(self, ch, key, kind, detail="", now=None):
        """A problem seen on a stream (kind is one of WEIGHT)."""
        now = now or time.time()
        with self.lock:
            rec = self._rec(ch, key)
            if any(k == kind and now - t < SAME_KIND_GAP for t, k in rec["ev"]):
                return
            rec["ev"].append([now, kind])
            self._persist(now)
            self._decide(ch, now, (key, kind, detail))

    def good(self, ch, key, minutes, now=None):
        now = now or time.time()
        with self.lock:
            self._rec(ch, key)["good"].append([now, minutes])
            self._persist(now)
            self._decide(ch, now, None)

    # compatibility with 1.0.2 callers
    def fail(self, ch, key, now=None):
        self.problem(ch, key, "dead", now=now)

    def desync(self, ch, key, seconds, ahead, now=None):
        self.problem(ch, key, "desync", "%.0f seconds %s its picture" % (seconds, "ahead of" if ahead else "behind"), now)

    # -- decisions ---------------------------------------------------------------------------

    @staticmethod
    def _channel(cfg, ch):
        return next((c for c in cfg.get("channels") or [] if str(c.get("id")) == str(ch)), None)

    def _decide(self, ch, now, hit):
        try:
            chan = self._channel(self.store.get(), ch)
            if chan is None or len(chan.get("streams") or []) < 2:
                return
            keys = [_key(s) for s in chan["streams"]]
            # 1. Remove a stream that has been bad for a long time.
            for k in keys:
                desyncs = self._count(ch, k, now, "desync")
                long_bad = (k != keys[0] and self.score(ch, k, now) >= REMOVE_SCORE
                            and self._hours(ch, k, now) >= REMOVE_HOURS and self._good(ch, k, now) < REMOVE_MAX_GOOD)
                if desyncs >= DESYNC_REMOVE or long_bad:
                    why = ("its sound was off its picture %d times today" % desyncs if desyncs >= DESYNC_REMOVE else
                           "it kept failing in %d separate hours of the last day" % self._hours(ch, k, now))
                    self._remove(ch, k, why, now)
                    return
            # 2. Best order: streams hit by a "demote now" problem in the last hour go last, then by score.
            def rank(k):
                return (self._recent(ch, k, now, DEMOTE_NOW, DEMOTE_WINDOW), self.score(ch, k, now))
            order = sorted(keys, key=lambda k: (rank(k), keys.index(k)))
            main = keys[0]
            urgent = hit is not None and hit[1] in DEMOTE_NOW
            if order[0] != main:
                better = (rank(main)[0] and not rank(order[0])[0]) or \
                    self.score(ch, main, now) - self.score(ch, order[0], now) >= PROMOTE_MARGIN
                if not better:
                    order = [main] + [k for k in order if k != main]
            if order == keys:
                if urgent:
                    key, kind, detail = hit
                    self._note("flagged", ch, chan.get("name"), key,
                               "Stream %s %s (stream %d) %s%s; it stays where it is, below the working streams." % (
                                   key[0], key[1], keys.index(key) + 1, TEXT.get(kind, kind),
                                   " (%s)" % detail if detail else ""), None, now)
                return
            if not urgent and now - self.reordered.get(str(ch), 0) < REORDER_GAP and order[0] == main:
                return
            self._reorder(ch, keys, order, hit, now)
        except Exception:  # noqa: BLE001 - feed health never takes coax down
            log.exception("feed health decision failed")

    def _reorder(self, ch, old, new, hit, now):
        done = {}

        def mutate(cfg):
            chan = self._channel(cfg, ch)
            if chan is None:
                return None
            streams = chan.get("streams") or []
            if [_key(s) for s in streams] != old:
                return None
            by = {_key(s): s for s in streams}
            chan["streams"] = [by[k] for k in new]
            done["name"] = chan.get("name")
            return True
        try:
            if not self.store.update(mutate):
                return
        except Exception:  # noqa: BLE001
            log.exception("could not reorder streams")
            return
        self.reordered[str(ch)] = now
        moved, cause = (hit[0], hit[1]) if hit else (None, None)
        if new[0] != old[0]:
            kind = "promoted"
            text = "Made stream %s %s the main stream (was stream %d)" % (new[0][0], new[0][1], old.index(new[0]) + 1)
            if moved == old[0] and cause:
                text += " because the main stream %s%s." % (TEXT.get(cause, cause), " (%s)" % hit[2] if hit[2] else "")
            else:
                text += " because it has been more stable."
        else:
            kind = "reordered"
            if moved and cause:
                text = "Moved stream %s %s from place %d to %d because it %s%s." % (
                    moved[0], moved[1], old.index(moved) + 1, new.index(moved) + 1, TEXT.get(cause, cause),
                    " (%s)" % hit[2] if hit[2] else "")
            else:
                text = "Put the streams in order of how stable they have been."
        log.warning("[%s] %s", done["name"], text)
        self._note(kind, ch, done["name"], moved or new[0], text, {"order": [list(k) for k in old]}, now)

    def _remove(self, ch, key, reason, now):
        done = {}

        def mutate(cfg):
            chan = self._channel(cfg, ch)
            if chan is None:
                return None
            streams = chan.get("streams") or []
            for i, s in enumerate(streams):
                if _key(s) == key and len(streams) > 1:
                    done.update(index=i, stream=copy.deepcopy(s), name=chan.get("name"))
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
        text = "Removed stream %s %s (was stream %d) because %s." % (key[0], key[1], done["index"] + 1, reason)
        log.warning("[%s] %s", done["name"], text)
        self._note("removed", ch, done["name"], key, text, {"index": done["index"], "stream": done["stream"]}, now)

    # -- notifications and channel notes ------------------------------------------------------

    def _note(self, kind, ch, name, key, text, undo, now):
        nid = str(int(now * 1000))
        while any(n["id"] == nid for n in self.notes):
            nid = str(int(nid) + 1)
        note = {"id": nid, "t": now, "kind": kind, "channel_id": ch, "channel": name, "provider": key[0],
                "stream_id": key[1], "text": text, "undo": undo, "undone": False, "read": False}
        self.notes.insert(0, note)
        del self.notes[MAX_NOTES:]
        _save(self.npath, self.notes)
        lst = self.chnotes.setdefault(str(ch), [])
        lst.insert(0, {"t": now, "kind": kind, "text": text})
        del lst[CHANNEL_NOTES:]
        self._persist(now)

    def channel_notes(self):
        with self.lock:
            return copy.deepcopy(self.chnotes)

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
                    if not any(_key(s) == key for s in streams):
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
            self.reordered[str(note["channel_id"])] = time.time()
            note["undone"] = True
            note["read"] = True
            lst = self.chnotes.setdefault(str(note["channel_id"]), [])
            lst.insert(0, {"t": time.time(), "kind": "undo", "text": "Undid: " + note["text"]})
            del lst[CHANNEL_NOTES:]
            _save(self.npath, self.notes)
            self._persist(time.time())
            return note
