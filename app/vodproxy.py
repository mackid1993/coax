"""Plays a provider's movie or episode for Channels DVR: /vod/<provider>/<m|e>/<id>.<ext>, bytes as they are.

No transcoding: a file downloads many times faster than it plays (about 20x through the owner's VPN), so the
player keeps its own buffer, and the formats (4K, HEVC, Dolby Vision, Atmos in MKV) are the player's business.

A player opens several connections to coax for one play: at the start it reads the beginning, then the index at
the end of the file (MKV cues, MP4 moov), then the beginning again; a fast forward is a burst of a dozen seeks
in a few seconds, up to three open at once. The provider must never see more than one connection per play, or a
one-connection line answers 509 and a recording on the line can die. So, per play (one viewer, one provider):

- the play holds one line slot in the hub (Hub.vod_hold), like a tune, for as long as it lasts and a few seconds
  after its last request, so a seek never loses the line to someone else between two requests;
- one provider connection at a time: a request that needs the provider closes the older connection first and
  waits until it is closed; the player has already given up on it;
- a burst of seeks (requests less than a second apart) waits a moment and only the newest one connects;
- the end of the file is read once, at the player's first index read, and kept in memory: its later index
  reads (one per seek) are answered by coax without touching the provider;
- only explicit byte ranges go to the provider (some CDNs answer 500 to a "last N bytes" range);
- a busy answer right after a reconnect (509 and the like: the panel has not noticed the old connection close
  yet) is retried for a few seconds;
- a tune or recording that finds every line busy stops the play to take its line (Hub.open).

A play is one viewer (client address) playing one title. Viewers that share an address (behind NAT, or a relay)
each get their own play and line. A viewer who starts another title takes the line of their own idle play (one with
no request open) when the provider has no free line, so backing out of one movie into another never waits.
"""

import logging
import re
import socket
import threading
import time

import requests

from app import USER_AGENT

log = logging.getLogger("coax.vod")

TAIL = 4 * 1024 * 1024       # end of the file kept in memory (the player's index reads)
LINGER = 8.0                 # a play keeps its line this long after its last request ends
BURST = 1.0                  # requests closer together than this are a burst of seeks...
DEBOUNCE = 0.35              # ...and wait this long, so only the newest one connects
RETRY_FOR = 10.0             # keep retrying a busy provider this long
TAKEOVER_WAIT = 3.0          # a viewer's new title waits this long for their old title's connection to close
STOP_WAIT = 2.5              # a tune taking the line waits at most this long for the movie's connection to go
CLOSE_WAIT = 20.0            # longest wait for an older connection of the play to open or close (slow panels: 5 s)
TIMEOUT = (5, 30)
CHUNK = 256 * 1024
BUSY = {429, 500, 502, 503, 504, 509, 520, 521, 522, 523, 524}
CONTENT_TYPES = {"mkv": "video/x-matroska", "mp4": "video/mp4", "m4v": "video/mp4", "mov": "video/quicktime",
                 "avi": "video/x-msvideo", "ts": "video/mp2t", "mpg": "video/mpeg", "mpeg": "video/mpeg",
                 "webm": "video/webm", "wmv": "video/x-ms-wmv", "flv": "video/x-flv"}
_RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")
_CONTENT_RANGE = re.compile(r"bytes\s+(\d+)-(\d+)/(\d+)")


class UpstreamError(Exception):
    def __init__(self, status):
        super().__init__("provider answered %s" % status)
        self.status = status or 502


def parse_range(value):
    """'bytes=100-' -> (100, None); 'bytes=100-199' -> (100, 199); 'bytes=-500' -> (None, 500); else None."""
    match = _RANGE.match((value or "").strip())
    if not match or (not match.group(1) and not match.group(2)):
        return None
    start = int(match.group(1)) if match.group(1) else None
    end = int(match.group(2)) if match.group(2) else None
    if start is not None and end is not None and end < start:
        return None
    return start, end


def _hard_close(resp):
    """Close a provider response at once, even while another thread is blocked reading it."""
    try:
        sock = resp.raw._connection.sock   # noqa: SLF001 - urllib3 keeps the socket here
    except AttributeError:
        sock = None
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
    try:
        resp.close()
    except Exception:  # noqa: BLE001
        pass


class Play:
    """One viewer playing one title from one provider. The title never changes: its size and kept end of file
    belong to it."""

    def __init__(self, plays, client_ip, provider_id, item, name):
        self.plays = plays
        self.client_ip = client_ip
        self.provider_id = provider_id
        self.item = item               # (kind, id, ext)
        self.name = name
        self.hold = None
        self.cond = threading.Condition()
        self.gen = 0                   # bumped by every request that needs the provider; older copies stop
        self.upstream = None           # the one open provider response
        self.opening = False           # a provider connection is being opened
        self.stopped = False
        self.active = 0
        self.last_start = 0.0
        self.last_end = time.monotonic()
        self.size = None
        self.tail = None               # (offset, bytes) of the end of the file
        self.content_type = None
        self.source = None             # an M3U title's provider address
        self.sent = 0
        self.started = time.time()

    def stop(self):
        """End the play: close its provider connection, wait for it to be gone, then give the line back."""
        with self.cond:
            if self.stopped:
                return
            self.stopped = True
            self.gen += 1
            if self.upstream is not None:
                _hard_close(self.upstream)
            # A connection still being opened is closed by its opener the moment it answers (it sees `stopped`).
            deadline = time.monotonic() + STOP_WAIT
            while (self.upstream is not None or self.opening) and time.monotonic() < deadline:
                self.cond.wait(0.1)
        self.plays.forget(self)
        if self.hold is not None:
            self.plays.hub.vod_release(self.hold)
        log.info("VOD: %s stopped %s (%.0f MB)", self.client_ip, self.name, self.sent / 1e6)


class Plays:
    def __init__(self, hub, get_config, title=None, session=None):
        self.hub = hub
        self.get_config = get_config
        self.title = title or (lambda kind, item_id, provider_id=None: None)
        self.http = session or requests   # no shared pool: an idle kept-alive connection counts at the panel
        self._lock = threading.Lock()
        self._plays = {}               # (client ip, provider id, item) -> Play
        self._janitor = None

    # -- bookkeeping ------------------------------------------------------------------------------------------------
    def status(self):
        """The plays holding a line now, for the Status page: each one uses a provider connection like a tune."""
        with self._lock:
            plays = [p for p in self._plays.values() if not p.stopped]
        out = []
        for p in plays:
            hold = p.hold
            out.append({"provider_id": p.provider_id, "provider_name": getattr(hold, "provider_name", "") or "",
                        "line_id": getattr(hold, "line_id", None), "line_name": getattr(hold, "line_name", "") or "",
                        "client_ip": p.client_ip, "name": p.name, "kind": "movie" if p.item[0] == "m" else "episode",
                        "started": p.started, "sent": p.sent, "streaming": p.active > 0})
        out.sort(key=lambda x: x["started"])
        return out

    def forget(self, play):
        key = (play.client_ip, play.provider_id, play.item)
        with self._lock:
            if self._plays.get(key) is play:
                del self._plays[key]

    def _sweep(self):
        while True:
            time.sleep(1.0)
            now = time.monotonic()
            with self._lock:
                idle = [p for p in self._plays.values() if p.active == 0 and now - p.last_end > LINGER]
            for play in idle:
                play.stop()

    def _play_for(self, client_ip, provider, item):
        kind, item_id, _ext = item
        key = (client_ip, provider["id"], item)
        with self._lock:
            if self._janitor is None:
                self._janitor = threading.Thread(target=self._sweep, name="vod-janitor", daemon=True)
                self._janitor.start()
            play = self._plays.get(key)
            if play is not None and not play.stopped:
                return play
        name = self.title(kind, item_id, provider["id"]) or ("%s %s" % (kind, item_id))
        play = Play(self, client_ip, provider["id"], item, name)
        hold = self.hub.vod_hold(provider["id"], "VOD: " + name, play.stop) if self.hub else None
        if hold is None:
            # No free line: this viewer's own play of another title gives its line up once it is idle (nothing
            # open). The player closes the old title's connection as it starts the new one, so wait a moment for it.
            deadline = time.monotonic() + TAKEOVER_WAIT
            while hold is None:
                with self._lock:
                    mine = [p for (ip, pid, _i), p in self._plays.items()
                            if ip == client_ip and pid == provider["id"] and not p.stopped]
                idle = [p for p in mine if p.active == 0]
                for old in idle:
                    log.info("VOD: %s moved on from %s to %s", client_ip, old.name, name)
                    old.stop()
                    hold = self.hub.vod_hold(provider["id"], "VOD: " + name, play.stop)
                    if hold is not None:
                        break
                if hold is not None or not mine or time.monotonic() > deadline:
                    break
                time.sleep(0.1)
        if hold is None:
            return None
        play.hold = hold
        with self._lock:
            other = self._plays.get(key)
            if other is not None and not other.stopped:   # a second request for it got there first
                self.hub.vod_release(hold)
                return other
            self._plays[key] = play
        log.info("VOD: %s playing %s on %s / %s", client_ip, name, hold.provider_name, hold.line_name)
        return play

    # -- the provider -----------------------------------------------------------------------------------------------
    def _url(self, provider, play):
        try:
            return self._address(provider, play)
        except UpstreamError:
            raise
        except Exception as exc:  # noqa: BLE001 - a bad line rewrite or the like: a clean 502, not a traceback
            log.warning("VOD: no provider address for %s: %s", play.name, exc)
            raise UpstreamError(502)

    def _address(self, provider, play):
        kind, item_id, ext = play.item
        line = play.hold.line
        if (provider.get("type") or "xtream").lower() == "m3u":
            from app.providers import m3u, vod
            if play.source is None:   # once per title: the M3U address is in the VOD list, read from disk
                found = vod.find(provider["id"], kind, item_id)
                if not found or not found.get("url"):
                    raise UpstreamError(404)
                play.source = found["url"]
            return m3u.apply_line_rewrite(play.source, line)
        from app.providers import xtream
        return xtream.vod_url(provider, line, kind, item_id, ext)

    def _open(self, play, my, url, range_header):
        """One provider request; busy answers are retried for a few seconds. Raises UpstreamError."""
        deadline = time.monotonic() + RETRY_FOR
        delay = 0.5
        while True:
            status = None
            headers = {"User-Agent": USER_AGENT}
            if range_header:
                headers["Range"] = range_header
            try:
                resp = self.http.get(url, headers=headers, stream=True, timeout=TIMEOUT)
            except requests.RequestException:
                status = 502
            else:
                if resp.status_code < 400 or resp.status_code == 416:
                    return resp
                status = resp.status_code
                _hard_close(resp)
                if status not in BUSY:
                    raise UpstreamError(status)
            if time.monotonic() + delay > deadline or play.gen != my or play.stopped:
                raise UpstreamError(status)
            time.sleep(delay)
            delay = min(delay * 2, 2.0)

    def _note(self, play, resp):
        """Learn the file's size and type from a provider answer."""
        match = _CONTENT_RANGE.search(resp.headers.get("Content-Range") or "")
        if match:
            play.size = int(match.group(3))
        elif resp.status_code == 200 and resp.headers.get("Content-Length", "").isdigit():
            play.size = int(resp.headers["Content-Length"])
        play.content_type = play.content_type or resp.headers.get("Content-Type")
        return match

    def _learn_size(self, play, my, url):
        """One byte, for the size. Only explicit ranges go to the provider: some CDNs answer 500 to bytes=-N."""
        resp = self._open(play, my, url, "bytes=0-0")
        try:
            self._note(play, resp)
        finally:
            _hard_close(resp)

    def _fetch_tail(self, play, my, url):
        """Read the end of the file once and keep it: the player's index reads come back to it on every seek."""
        start = max(play.size - TAIL, 0)
        resp = self._open(play, my, url, "bytes=%d-%d" % (start, play.size - 1))
        try:
            match = self._note(play, resp)
            if match and int(match.group(1)) == start and int(match.group(2)) - start < 2 * TAIL:
                body = resp.content
                if len(body) == int(match.group(2)) - start + 1:
                    play.tail = (start, body)
        finally:
            _hard_close(resp)

    # -- one request ------------------------------------------------------------------------------------------------
    def serve(self, handler, provider_id, kind, item_id, ext):
        cfg = self.get_config() or {}
        provider = next((p for p in cfg.get("providers") or [] if p.get("id") == provider_id), None)
        if provider is None:
            return handler.send_error(404, "unknown provider")
        if provider.get("enabled", True) is False:
            return handler.send_error(503, "provider is switched off")
        item = ("episode" if kind == "e" else "movie", str(item_id), (ext or "mp4").lower())
        play = self._play_for(handler.client_address[0], provider, item)
        if play is None:
            return handler.send_error(503, "no free provider line")
        head = handler.command == "HEAD"
        wanted = parse_range(handler.headers.get("Range"))
        with play.cond:
            play.active += 1
        try:
            self._serve(handler, provider, play, item, head, wanted)
        finally:
            with play.cond:
                play.active -= 1
                play.last_end = time.monotonic()
                play.cond.notify_all()

    def _from_tail(self, play, wanted):
        """(start, end) of the bytes to answer from the kept end of the file, or None."""
        if play.tail is None or play.size is None or wanted is None:
            return None
        start, end = wanted
        if start is None:
            start, end = max(play.size - end, 0), play.size - 1
        end = play.size - 1 if end is None else min(end, play.size - 1)
        if play.tail[0] <= start <= end:
            return start, end
        return None

    def _send_head(self, handler, status, play, length, content_range=None):
        handler.send_response(status)
        handler.send_header("Content-Type", play.content_type or CONTENT_TYPES.get(play.item[2], "application/octet-stream"))
        if length is not None:
            handler.send_header("Content-Length", str(length))
        if content_range:
            handler.send_header("Content-Range", content_range)
        handler.send_header("Accept-Ranges", "bytes")
        handler.send_header("Cache-Control", "no-store")
        handler.end_headers()

    def _serve(self, handler, provider, play, item, head, wanted):
        handler.close_connection = True
        span = self._from_tail(play, wanted)
        if span is not None:   # an index read: from memory, the provider connection carries on
            start, end = span
            self._send_head(handler, 206, play, end - start + 1, "bytes %d-%d/%d" % (start, end, play.size))
            if not head:
                offset = play.tail[0]
                handler.wfile.write(play.tail[1][start - offset:end - offset + 1])
            return
        if head and play.size is not None:
            return self._send_head(handler, 200, play, play.size)

        with play.cond:
            play.gen += 1
            my = play.gen
            now = time.monotonic()
            burst = now - play.last_start < BURST
            play.last_start = now
            if play.upstream is not None:
                _hard_close(play.upstream)   # the player has moved on from it
        if burst:
            time.sleep(DEBOUNCE)
            if play.gen != my:
                return   # a newer seek came in; this one is already abandoned
        with play.cond:
            deadline = time.monotonic() + CLOSE_WAIT
            while (play.upstream is not None or play.opening) and play.gen == my and time.monotonic() < deadline:
                play.cond.wait(0.2)
            if play.gen != my or play.stopped:
                return
            if play.upstream is not None or play.opening:
                return handler.send_error(503, "provider connection still closing")
            play.opening = True
        resp = span = None
        try:
            url = self._url(provider, play)
            if play.size is None and (head or (wanted is not None and wanted[0] is None)):
                self._learn_size(play, my, url)
            if head:
                pass
            elif play.size is not None and wanted is not None and play.tail is None \
                    and (play.size - (wanted[1] or 0) if wanted[0] is None else wanted[0]) >= play.size - TAIL:
                self._fetch_tail(play, my, url)
                span = self._from_tail(play, wanted)
            if not head and span is None and play.gen == my and not play.stopped:
                rng = handler.headers.get("Range")
                if wanted is not None and wanted[0] is None and play.size is not None:
                    rng = "bytes=%d-%d" % (max(play.size - wanted[1], 0), play.size - 1)
                resp = self._open(play, my, url, rng)
        except UpstreamError as exc:
            log.warning("VOD: %s: %s", play.name, exc)
            return handler.send_error(exc.status if exc.status < 500 else 502, "provider error")
        finally:
            with play.cond:
                if play.opening:
                    play.opening = False
                    if resp is not None and play.gen == my and not play.stopped:
                        play.upstream = resp
                    elif resp is not None:
                        _hard_close(resp)
                        resp = None
                play.cond.notify_all()
        if head and play.size is not None and resp is None:
            return self._send_head(handler, 200, play, play.size)
        if span is not None:
            return self._serve(handler, provider, play, item, head, wanted)   # now from memory
        if resp is None:
            return
        try:
            self._note(play, resp)
            handler.send_response(resp.status_code)
            handler.send_header("Content-Type", resp.headers.get("Content-Type") or play.content_type
                                or CONTENT_TYPES.get(item[2], "application/octet-stream"))
            for key in ("Content-Length", "Content-Range", "Last-Modified"):
                if resp.headers.get(key):
                    handler.send_header(key, resp.headers[key])
            handler.send_header("Accept-Ranges", "bytes")
            handler.send_header("Cache-Control", "no-store")
            handler.end_headers()
            if head:
                return
            for chunk in resp.iter_content(CHUNK):
                if play.gen != my or play.stopped:
                    break
                handler.wfile.write(chunk)
                play.sent += len(chunk)
        except Exception:  # noqa: BLE001 - the player went away, or this connection was closed for a newer one
            pass             # (urllib3 raises odd errors when another thread closes the response mid-read)
        finally:
            with play.cond:
                if play.upstream is resp:
                    play.upstream = None
                play.cond.notify_all()
            _hard_close(resp)
