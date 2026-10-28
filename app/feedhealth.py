"""Feed health: when a stream causes real trouble, coax fixes the channel's order and leaves it there.

Rules, in short:
- Only real trouble counts. Problems coax absorbs (stalls the reserve covers, refills, catch-ups) are normal
  for IPTV and are ignored.
- Real trouble on a stream:
    desync       its sound is seconds off its picture (content check after a switch)  -> blocked at once
    placeholder  the provider served an offline slate instead of the channel           -> blocked at once
    dead         it stopped working and another stream of the channel took over         -> blocked
    frozen / novideo  coax had to restart or retune it; 3 times within an hour         -> blocked
  A stream that dies while no other stream of the channel works is not blocked (that is the provider or the
  network, not the stream).
- A blocked stream is not tuned for 12 hours, then 24 hours, then 3 days if it keeps doing it within a week
  (out of sync: 24 hours, then 3 days). Nothing is ever deleted. If every stream of a channel is blocked,
  coax still plays the least bad one.
- When a stream is blocked, the channel's saved order changes once: the blocked stream goes to the bottom,
  and if it was the main stream, the best working stream becomes the main stream (the one that has played
  cleanly the longest; a stream that never played ranks below one that has). Everything else keeps its place.
- No trouble, no change. Nothing moves back when a block ends; the order stays until the next real problem.
- Each block is one entry under the bell, with Undo (restores the order and lifts the block).

State: <data>/cache/feedhealth.json; bell: <data>/notifications.json. Never raises.
"""

import copy
import json
import logging
import os
import threading
import time

log = logging.getLogger("coax.health")

HOUR = 3600
DAY = 24 * HOUR
WEEK = 7 * DAY
INSTANT = {"desync", "placeholder"}
REPEATED = {"frozen", "novideo"}
REPEAT_COUNT = 3
REPEAT_WINDOW = HOUR
DEAD_REPEAT = 3             # a stream that died while another took over: blocked the 3rd time within 6 hours
PROVEN_MINUTES = 10         # clean minutes that make a stream checked (as does a passed sync check)
DEAD_CONFIRM = 600          # a "dead" stream is blocked if another stream of the channel takes over within 10 min
SAME_KIND_GAP = 120
BLOCKS = [12 * HOUR, DAY, 3 * DAY]
DESYNC_BLOCKS = [DAY, 3 * DAY]
MAX_NOTES = 200

TEXT = {"desync": "its sound was off its picture", "placeholder": "it showed an offline slate instead of the channel",
        "dead": "it stopped working 3 times within 6 hours while another stream worked", "frozen": "it froze 3 times within an hour",
        "novideo": "it sent no video 3 times within an hour"}


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


def _hours(seconds):
    return "%d hours" % (seconds // HOUR) if seconds < 2 * DAY else "%d days" % (seconds // DAY)


class FeedHealth:
    def __init__(self, store, data_dir):
        self.store = store
        self.path = os.path.join(data_dir, "cache", "feedhealth.json")
        self.npath = os.path.join(data_dir, "notifications.json")
        self.lock = threading.RLock()
        data = _load(self.path, {})
        self.feeds = data.get("feeds") if isinstance(data.get("feeds"), dict) else {}
        self.notes = _load(self.npath, [])
        self.pending = {}           # channel -> (troubled stream, stream switched to, time): promote once proven

    # -- state ------------------------------------------------------------------------------

    @staticmethod
    def _k(ch, key):
        return "%s|%s|%s" % (ch, key[0], key[1])

    def _rec(self, ch, key):
        rec = self.feeds.setdefault(self._k(ch, key), {})
        for f in ("ev", "good", "blocks"):
            rec.setdefault(f, [])
        return rec

    def _persist(self, now):
        for k in list(self.feeds):
            rec = self.feeds[k]
            rec["ev"] = [e for e in rec.get("ev", []) if now - e[0] < WEEK]
            rec["good"] = [g for g in rec.get("good", []) if now - g[0] < WEEK]
            rec["blocks"] = [b for b in rec.get("blocks", []) if now - b[0] < WEEK or b[1] > now]
            if not rec["ev"] and not rec["good"] and not rec["blocks"] and (rec.get("ok") is None or now - rec["ok"] >= WEEK):
                del self.feeds[k]
        _save(self.path, {"feeds": self.feeds})

    def blocked_until(self, ch, key, now=None):
        now = now or time.time()
        rec = self.feeds.get(self._k(ch, key)) or {}
        return max((b[1] for b in rec.get("blocks", []) if b[1] > now), default=0)

    def is_blocked(self, ch, key, now=None):
        return self.blocked_until(ch, key, now) > 0

    def _clean(self, ch, key, now):
        rec = self.feeds.get(self._k(ch, key)) or {}
        return sum(m for t, m in rec.get("good", []) if now - t < WEEK)

    def _strikes(self, ch, key, now):
        rec = self.feeds.get(self._k(ch, key)) or {}
        return sum(1 for t, _ in rec.get("ev", []) if now - t < WEEK)

    # -- signals from the hub -----------------------------------------------------------------

    def problem(self, ch, key, kind, detail="", now=None):
        """Real trouble on a stream. Absorbed problems are never reported here."""
        now = now or time.time()
        with self.lock:
            rec = self._rec(ch, key)
            if any(k == kind and now - t < SAME_KIND_GAP for t, k in rec["ev"]):
                return
            rec["ev"].append([now, kind])
            if kind in INSTANT:
                self._block(ch, key, kind, detail, now)
            elif kind in REPEATED:
                n = sum(1 for t, k in rec["ev"] if k in REPEATED and now - t < REPEAT_WINDOW)
                if n >= REPEAT_COUNT:
                    self._block(ch, key, kind, detail, now)
            self._persist(now)

    def switched(self, ch, old, new, now=None):
        """The channel left `old` for `new`: if `old` just died, it is the stream's fault, not the network's."""
        now = now or time.time()
        if old is None or old == new:
            return
        with self.lock:
            rec = self.feeds.get(self._k(ch, old)) or {}
            if any(k == "dead" and now - t < DEAD_CONFIRM for t, k in rec.get("ev", [])):
                rec = self._rec(ch, old)
                c = [t for t in rec.get("confirmed", []) if now - t < 6 * HOUR]
                if not c or now - c[-1] >= DEAD_CONFIRM:
                    c.append(now)
                rec["confirmed"] = c
                if len(c) >= DEAD_REPEAT:
                    self._block(ch, old, "dead", "", now)
                else:
                    self.pending[str(ch)] = (old, new, now)
                    self._try_promote(ch, now)
                self._persist(now)

    def good(self, ch, key, minutes, now=None):
        now = now or time.time()
        with self.lock:
            self._rec(ch, key)["good"].append([now, minutes])
            self._try_promote(ch, now)
            self._persist(now)

    def verified(self, ch, key, now=None):
        """The stream passed the sync check (before or after a switch)."""
        now = now or time.time()
        with self.lock:
            self._rec(ch, key)["ok"] = now
            self._try_promote(ch, now)
            self._persist(now)

    def _try_promote(self, ch, now):
        """After trouble, the stream coax switched to moves ahead of the troubled one, once it is proven
        (passed the sync check or played 10 minutes cleanly). Nothing else ever moves it back."""
        p = self.pending.get(str(ch))
        if p is None:
            return
        old, new, t = p
        if now - t > HOUR:
            del self.pending[str(ch)]
            return
        if self.is_blocked(ch, new, now) or not self._proven(ch, new, now):
            return
        del self.pending[str(ch)]
        done = {}

        def mutate(cfg):
            chan = self._channel(cfg, ch)
            if chan is None:
                return None
            streams = chan.get("streams") or []
            keys = [_key(s) for s in streams]
            if old not in keys or new not in keys or keys.index(new) < keys.index(old):
                return None
            i = keys.index(old)
            order = [k for k in keys if k != new]
            order.insert(i, new)
            by = {_key(s): s for s in streams}
            chan["streams"] = [by[k] for k in order]
            done.update(old=keys, new=order, name=chan.get("name"))
            return True
        try:
            if not self.store.update(mutate):
                return
        except Exception:  # noqa: BLE001
            log.exception("could not reorder streams")
            return
        keys, order = done["old"], done["new"]
        if order[0] != keys[0]:
            text = ("Made stream %s %s the main stream: the main stream had connection trouble and this one is "
                    "stable. The order stays like this until the next trouble." % new)
        else:
            text = ("Moved stream %s %s ahead of stream %s %s: that one had connection trouble and this one is "
                    "stable." % (new[0], new[1], old[0], old[1]))
        log.warning("[%s] %s", done["name"], text)
        self._note("promoted" if order[0] != keys[0] else "reordered", ch, done["name"], new, text,
                   {"order": [list(k) for k in keys]}, now)

    RANK_W = {"desync": 100.0, "placeholder": 20.0, "dead": 10.0, "novideo": 8.0, "frozen": 5.0,
              "avsplit": 1.0, "stall": 0.5, "keepalive": 0.5}

    def rank(self, ch, key, now=None):
        """How bad a stream has been: blocked first, then weighted problems of the last day (fading by half
        every 6 hours; sound off its picture outweighs everything), minus clean playback. Lower is better."""
        now = now or time.time()
        with self.lock:
            rec = self.feeds.get(self._k(ch, key)) or {}
            bad = 0.0
            for t, k in rec.get("ev", []):
                if now - t < DAY:
                    bad += self.RANK_W.get(k, 1.0) * 0.5 ** ((now - t) / (6 * HOUR))
            good = min(self._clean(ch, key, now), 600) / 60.0
            return (1 if self.is_blocked(ch, key, now) else 0, round(bad - good, 3))

    def _proven(self, ch, key, now):
        rec = self.feeds.get(self._k(ch, key)) or {}
        ok = rec.get("ok")
        return self._clean(ch, key, now) >= PROVEN_MINUTES or (ok is not None and now - ok < WEEK)

    # older callers
    def fail(self, ch, key, now=None):
        self.problem(ch, key, "dead", now=now)

    def desync(self, ch, key, seconds, ahead, now=None):
        self.problem(ch, key, "desync", "%.0f seconds %s its picture" % (seconds, "ahead of" if ahead else "behind"), now)

    # -- the fix ------------------------------------------------------------------------------------

    @staticmethod
    def _channel(cfg, ch):
        return next((c for c in cfg.get("channels") or [] if str(c.get("id")) == str(ch)), None)

    def _block(self, ch, key, kind, detail, now):
        try:
            chan = self._channel(self.store.get(), ch)
            if chan is None or len(chan.get("streams") or []) < 2:
                return
            if self.is_blocked(ch, key, now):
                return
            rec = self._rec(ch, key)
            ladder = DESYNC_BLOCKS if kind == "desync" else BLOCKS
            prior = sum(1 for b in rec["blocks"] if now - b[0] < WEEK and (b[2] == "desync") == (kind == "desync"))
            dur = ladder[min(prior, len(ladder) - 1)]
            rec["blocks"].append([now, now + dur, kind])
            self._fix_order(ch, key, kind, detail, dur, now)
        except Exception:  # noqa: BLE001 - feed health never takes coax down
            log.exception("feed health block failed")

    def _fix_order(self, ch, bad, kind, detail, dur, now):
        done = {}

        def mutate(cfg):
            chan = self._channel(cfg, ch)
            if chan is None:
                return None
            streams = chan.get("streams") or []
            keys = [_key(s) for s in streams]
            if bad not in keys:
                return None
            working = [k for k in keys if not self.is_blocked(ch, k, now)]
            blocked = sorted([k for k in keys if k not in working], key=lambda k: self.blocked_until(ch, k, now))
            new = working + blocked
            if keys[0] not in working:
                # Only a checked stream (passed the sync check, or played cleanly) may become the main stream.
                proven = [k for k in working if self._proven(ch, k, now)]
                if proven:
                    best = min(proven, key=lambda k: (-self._clean(ch, k, now), self._strikes(ch, k, now), keys.index(k)))
                    new = [best] + [k for k in working if k != best] + blocked
                else:
                    new = keys   # nothing checked to take over: keep the order; tuning skips the blocked stream
            done.update(old=keys, new=new, name=chan.get("name"))
            if new == keys:
                return True
            by = {_key(s): s for s in streams}
            chan["streams"] = [by[k] for k in new]
            return True
        if not self.store.update(mutate):
            return
        old, new = done["old"], done["new"]
        text = "Blocked stream %s %s for %s because %s%s." % (bad[0], bad[1], _hours(dur), TEXT.get(kind, kind),
                                                              " (%s)" % detail if detail else "")
        if new[0] != old[0]:
            text += " Stream %s %s is now the main stream." % (new[0][0], new[0][1])
        log.warning("[%s] %s", done["name"], text)
        self._note("promoted" if new[0] != old[0] else "blocked", ch, done["name"], bad, text,
                   {"order": [list(k) for k in old]}, now)

    # -- the bell -----------------------------------------------------------------------------------

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

    def dismiss(self, nid):
        with self.lock:
            n0 = len(self.notes)
            self.notes = [n for n in self.notes if n["id"] != str(nid)]
            if len(self.notes) == n0:
                raise KeyError(nid)
            _save(self.npath, self.notes)
            return self.list()

    def clear(self):
        with self.lock:
            self.notes = []
            _save(self.npath, self.notes)
            return self.list()

    def undo(self, nid):
        with self.lock:
            note = next((n for n in self.notes if n["id"] == str(nid)), None)
            if note is None or note.get("undone") or not note.get("undo"):
                raise KeyError(nid)
            order = [tuple(x) for x in note["undo"].get("order") or []]

            def mutate(cfg):
                chan = self._channel(cfg, note["channel_id"])
                if chan is None:
                    return None
                streams = chan.get("streams") or []
                if sorted(order) == sorted(_key(s) for s in streams):
                    by = {_key(s): s for s in streams}
                    chan["streams"] = [by[k] for k in order]
                return True
            if not self.store.update(mutate):
                raise KeyError(nid)
            rec = self.feeds.get(self._k(note["channel_id"], (note["provider"], note["stream_id"])))
            if rec:
                now = time.time()
                rec["blocks"] = [[b[0], min(b[1], now), b[2]] for b in rec.get("blocks", [])]
                rec["ev"] = []
            note["undone"] = True
            note["read"] = True
            _save(self.npath, self.notes)
            self._persist(time.time())
            return note
