"""Tune a channel, run its engine, pipe the engine's output to the DVR.

This follows ah4c's model (github.com/sullrich/ah4c; MIT-style license, Copyright 2023
Fancy Bits, LLC; main.go: tune(), copyFlush(),
reader.Close()), which has proven reliable with Channels DVR:

- One DVR connection = one engine process = one provider line slot. Channels DVR
  shares its own tuner across devices, so there is no fan-out or ring buffer here.
- tune: under one lock, walk the channel's streams in order and take the first
  provider line with a free slot (max_streams 0 = unlimited). If none, answer 503.
- stream: copy the engine's stdout to the socket, flushing after every chunk.
- close: every path ends in Session.close(), which kills the engine's whole process
  group, reaps it, and only then gives the slot back (so the provider never sees
  two connections on one line). Close is idempotent.

The engine (app/engine) owns all stream logic: reserve, stalls, failover, keepalive,
A/V checks, transcode, viewer buffer. The hub watches the engine from outside and never
gives up while the DVR is connected:

- engine exits: restart it on the same line and slot (it resumes its timeline from its own
  state files; COAX_RESTART=1 tells it to trust that state for up to a minute).
- engine frozen (no bytes at all for STALL_TIMEOUT after video has flowed): kill and restart.
- no real video (only keepalive) for DEAD_TIMEOUT: retune with a fresh engine; every second
  retune in a row moves to another free line of the same provider when there is one.
- between engine processes the hub sends MPEG-TS null packets so the DVR keeps the
  connection (and a recording keeps going). Restarts back off up to BACKOFF_MAX.

Owning the whole pipeline also lets the hub share what one engine learned: streams that
failed recently are tried last (tune order and backup order), a failover onto another
provider's line holds that line's slot, a just-released line is used last, and each
channel's 10-minute engine status lines are kept in cache/history/<channel id>.jsonl.
"""

import collections
import hashlib
import json
import logging
import re
import os
import select
import signal
import subprocess
import sys
import threading
import time

log = logging.getLogger("coax.hub")

READ_SIZE = 64 * 1024
from app import USER_AGENT
ENGINE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "engine")
RESTART_WINDOW = 600.0     # restarts within this window make the next one wait longer
RESTART_DELAY = 1.0        # first restart delay; doubles per recent restart
BACKOFF_MAX = 15.0
STOP_TIMEOUT = 3.0
STALL_TIMEOUT = 25.0       # no bytes at all from an engine that already sent video: frozen
DEAD_TIMEOUT = 90.0        # bytes but no real video for this long: retune
LINE_COOLDOWN = 5.0        # a line released this recently is used last
HISTORY_MAX = 1024 * 1024  # per-channel history file is trimmed past this size

# One MPEG-TS null packet (PID 0x1FFF), the same one the engine sends as keepalive.
NUL_PKT = b"G\x1f\xff\x10" + b"\xff" * 184
REF = re.compile(r"\[ref ([0-9a-f]{8})\]")
_HSX = re.compile(r"(/live/[^/?]+/[^/?]+/[0-9]+)\.ts(?=$|\?)")


def _has_video(chunk, pos):
    """True if the chunk holds any packet that is not a null packet. pos = bytes already sent mod 188."""
    i = (188 - pos) % 188
    n = len(chunk)
    while i + 3 < n:
        if chunk[i] == 0x47 and ((chunk[i + 1] & 0x1F) << 8 | chunk[i + 2]) != 0x1FFF:
            return True
        i += 188
    return False


# Engine log lines that are a problem with the stream playing now (stream switch lines carry their own ref).
PROBLEMS = [
    (re.compile(r"offline placeholder", re.I), "placeholder"),
]


def url_refs(url):
    """Short ids the engine logs for a stream URL (raw, and the HLS form hlsloop switches to)."""
    out = set()
    for u in (url, _HSX.sub(r"\1.m3u8", url or "", count=1)):
        if u:
            out.add(hashlib.md5(u.encode()).hexdigest()[:8])
    return out


def default_engine_command(fmt, url, user_agent, channel_id):
    """hlsloop.py for HLS providers, tsloop.py for raw MPEG-TS providers."""
    script = "tsloop.py" if str(fmt).lower() == "ts" else "hlsloop.py"
    return [sys.executable, "-u", os.path.join(ENGINE_DIR, script), url, user_agent, str(channel_id)]


def _default_stream_url(provider, line, stream_id):
    from app.providers import stream_url  # lazy: no import-time dependency
    return stream_url(provider, line, stream_id)


_REDACT = [
    (re.compile(r"(/(?:live|hls|movie|series|timeshift)/[^/\s]+/)[^/\s]+/"), r"\1********/"),
    (re.compile(r"((?:password|pass|pwd|token)=)[^&\s'\")]+", re.I), r"\1********"),
]


def _redact(text):
    """Hide provider passwords in engine log lines (stream URLs carry them)."""
    for pattern, repl in _REDACT:
        text = pattern.sub(repl, text)
    return text


def _r(p):
    return re.compile(p, re.I)


# Engine log line -> (kind, level, plain-English text) for the Status page timeline.
EVENT_RULES = [
    (_r(r"switching to stream (\d+) of (\d+)"), "failover", "warn",
     lambda m: "Switched to %s (stream %s of %s)" % ("the main stream" if m.group(1) == "1" else "backup stream",
                                                     m.group(1), m.group(2))),
    (_r(r"offline placeholder"), "placeholder", "warn", "Provider showed an offline placeholder; avoiding it"),
    (_r(r"audio and video are ([\d.]+)s apart"), "avsync", "warn",
     lambda m: "Audio and video %ss apart in this feed; held back" % m.group(1)),
    (_r(r"audio and video back in step"), "avsync", "ok", "Audio and video back in step"),
    (_r(r"provider restarted the channel"), "rejoin", "warn", "Provider restarted the channel; rejoined at live"),
    (_r(r"timeline jumped ([\d.]+)s"), "rejoin", "warn", lambda m: "Timeline jumped %ss; re-synced" % m.group(1)),
    (_r(r"continuing on the new stream timeline seamlessly"), "rejoin", "ok", "Joined the new stream seamlessly"),
    (_r(r"playing 3 percent slower"), "speed", "warn",
     lambda m: "Reserve low; playing 3% slower to rebuild it"),
    (_r(r"back to real speed"), "speed", "ok", "Reserve rebuilt; back to real speed"),
    (_r(r"reserve:? empty"), "reserve", "bad", "Reserve empty; picture now arrives only as fast as the provider sends it"),
    (_r(r"reserve:? refilled to (\d+)s"), "reserve", "ok", lambda m: "Reserve refilled to %ss" % m.group(1)),
    (_r(r"playlist stale (\d+)s"), "stall", "info", lambda m: "Provider playlist stalled %ss; covered by the reserve" % m.group(1)),
    (_r(r"catching up (\d+) segments"), "stall", "info", lambda m: "Caught up %s late segments" % m.group(1)),
    (_r(r"playlist error \(([^)]{0,60})"), "error", "warn", lambda m: "Provider error: %s" % m.group(1)),
    (_r(r"stream error \(([^)]{0,60})"), "error", "warn", lambda m: "Stream error: %s" % m.group(1)),
    (_r(r"(stream|backup stream) (\d+) of (\d+) (failed|is not answering)"), "error", "warn",
     lambda m: "Stream %s of %s %s; skipping it for a while" % (m.group(2), m.group(3), m.group(4))),
    (_r(r"sending keepalive packets"), "keepalive", "warn", "No new video; kept the connection alive"),
    (_r(r"video flowing again after ([\d.]+)s"), "keepalive", "ok", lambda m: "Video back after %ss" % m.group(1)),
    (_r(r"skipped segment"), "stall", "warn", "Skipped a broken segment"),
    (_r(r"recording: (.+) extended by (\d+)s"), "recording", "ok",
     lambda m: "Recording %s extended by %ss" % (m.group(1)[:40], m.group(2))),
    (_r(r"viewer buffer: (\d+) viewers"), "viewers", "info", lambda m: "%s viewers; new viewer given a head start" % m.group(1)),
    (_r(r"transcoding with (.+)$"), "transcode", "info", lambda m: "Transcoding with %s" % m.group(1)),
    (_r(r"resuming after an engine restart"), "restart", "info", "Resumed after an engine restart"),
    (_r(r"sync check: stream (\d+) of \d+ has its sound ([\d.]+)s (ahead of|behind)"), "avsync", "bad",
     lambda m: "Stream %s's sound was %ss %s its picture; left it and removed it from the channel"
     % (m.group(1), m.group(2), m.group(3))),
    (_r(r"sync check: stream (\d+) is in step"), "avsync", "ok", lambda m: "Stream %s checked: sound in step" % m.group(1)),
]


def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class Session:
    """One DVR connection: one engine process holding one line slot."""

    def __init__(self, hub, channel_cfg, provider, line, stream, url, client_ip, user_agent):
        self.hub = hub
        self.channel_id = channel_cfg.get("id")
        self.uuid = channel_cfg.get("uuid")
        self.name = channel_cfg.get("name") or str(self.channel_id)
        self.provider_id = provider.get("id")
        self.provider_name = provider.get("name") or provider.get("id")
        self.line_id = line.get("id")
        self.line_name = line.get("name") or line.get("id")
        self.stream_id = str(stream.get("stream_id"))
        self.fmt = provider.get("format") or "hls"
        self.engine_ua = USER_AGENT
        self.url = url
        self.client_ip = client_ip
        self.user_agent = user_agent
        self.started_at = time.time()
        self.bytes_out = 0
        self.restarts = []          # monotonic times of engine restarts
        self.encoder = None         # "Intel/AMD graphics (VAAPI)", "NVIDIA graphics (NVENC)", "the CPU ..."
        self.slow = False           # engine is playing 3% slower to refill its reserve
        self.slow_since = None
        self.reserve = None         # seconds of reserve from the engine's last status/refill line
        self.reserve_empty = False  # engine says video only arrives as fast as the provider sends it
        self.events = collections.deque(maxlen=60)  # what the engine corrected, newest last
        self.stream_no = 1          # which of the channel's streams is playing (1 = main)
        self.extra_slots = []       # (provider_id, line_id) held for backups on other providers
        self.cur_key = (provider.get("id"), str(stream.get("stream_id")))  # the stream playing now
        self.degraded = False       # last 10-minute status showed repeated failovers or long keepalive
        self.dead_retunes = 0       # retunes in a row without real video
        self.spawns = 0
        self._pos = 0               # bytes sent mod 188, so injected null packets stay aligned
        self.had_video = False      # real video has reached the DVR at least once
        self.instance = 0           # 0 for the first session of this channel, 1 for a second one, ...
        self.proc = None
        self.closed = False
        self.reading = False       # a thread is inside os.read on proc.stdout
        self._lock = threading.Lock()  # guards proc and closed

    # -- engine process ---------------------------------------------------------

    def _spawn(self):
        cmd = self.hub._engine_command(self.fmt, self.url, self.engine_ua, self.channel_id)
        env = dict(os.environ)
        env["COAX_PORT"] = str(self.hub._port())
        # Two DVR connections can run the same channel at once; keep their engine state files
        # apart. The first session keeps the plain names (so files do not pile up in /tmp).
        env["COAX_SESSION"] = "~%d" % self.instance if self.instance else ""
        if self.spawns:
            env["COAX_RESTART"] = "1"   # resume from the previous engine's state even if it is a little old
        else:
            env.pop("COAX_RESTART", None)
        self.spawns += 1
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, start_new_session=True, bufsize=0, env=env)
        threading.Thread(target=self._pump_stderr, args=(proc,), daemon=True,
                         name="coax-log-%s" % self.channel_id).start()
        return proc

    def _pump_stderr(self, proc):
        try:
            for raw in iter(proc.stderr.readline, b""):
                line = _redact(raw.decode("utf-8", "replace").rstrip())
                if line:
                    self._watch(line)
                    log.info("[%s] %s", self.name, line)
        except Exception:  # noqa: BLE001
            pass
        finally:
            try:
                proc.stderr.close()
            except Exception:  # noqa: BLE001
                pass

    def event(self, kind, text, level="info"):
        self.events.append({"t": time.time(), "kind": kind, "text": text, "level": level})

    def _watch(self, line):
        """Pick transcode and real-time state out of the engine's log lines (display only)."""
        for pattern, kind, level, fmt in EVENT_RULES:
            m = pattern.search(line)
            if m:
                try:
                    text = fmt(m) if callable(fmt) else fmt
                except Exception:  # noqa: BLE001
                    text = line
                self.event(kind, text, level)
                if kind == "failover":
                    try:
                        self.stream_no = int(m.group(1))
                    except (IndexError, ValueError, TypeError):
                        pass
                break
        m = REF.search(line)
        if m:
            if "sync check:" in line:
                self.hub._desync(self, m.group(1), line)
            elif "switching to stream" in line:
                self.hub._follow(self, m.group(1))
            elif re.search(r"\b(failed|is not answering)\b", line):
                self.hub._note_bad(m.group(1), self)
        for pattern, kind in PROBLEMS:
            if pattern.search(line):
                self.hub._health("problem", self.channel_id, self.cur_key, kind)
                break
        if "status: " in line:
            self.hub._history(self, line)
            m = re.search(r"(\d+) (?:switches|failovers), (\d+)s keepalive", line)
            if m:
                self.degraded = int(m.group(1)) >= 2 or int(m.group(2)) >= 30
                if int(m.group(1)) == 0 and int(m.group(2)) < 10:
                    self.hub._health("good", self.channel_id, self.cur_key, 10)
        m = re.search(r"transcoding with (.+)$", line)
        if m:
            self.encoder = m.group(1).strip()
        if "percent slower" in line:
            self.slow, self.slow_since = True, time.time()
        elif "back to real speed" in line:
            self.slow, self.slow_since = False, None
        if "reserve: empty" in line:
            self.reserve_empty = True
        m = re.search(r"reserve(?::)? (?:refilled to |back to |down to )?(\d+)s", line)
        if m:
            self.reserve = int(m.group(1))
            if "refilled" in line or "back to" in line:
                self.reserve_empty = False
        m = re.search(r"status: .*?reserve (\d+)s", line)
        if m:
            self.reserve = int(m.group(1))
            self.reserve_empty = self.reserve < 2

    @staticmethod
    def _kill(proc, close_stdout=True):
        """Stop the engine and everything it started (ffmpeg), then reap it.

        close_stdout=False when another thread may still be reading the pipe; that
        thread closes it once its read returns (the dead process gives EOF)."""
        if proc is None:
            return
        for sig, wait in ((signal.SIGTERM, STOP_TIMEOUT), (signal.SIGKILL, STOP_TIMEOUT)):
            if proc.poll() is not None:
                break
            try:
                os.killpg(proc.pid, sig)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                proc.wait(timeout=wait)
            except subprocess.TimeoutExpired:
                continue
        try:
            os.killpg(proc.pid, signal.SIGKILL)  # leftover children in the group
        except (ProcessLookupError, PermissionError):
            pass
        if close_stdout:
            try:
                proc.stdout.close()
            except Exception:  # noqa: BLE001
                pass

    def _backoff(self):
        now = time.monotonic()
        self.restarts = [t for t in self.restarts if now - t < RESTART_WINDOW]
        self.restarts.append(now)
        return min(RESTART_DELAY * 2 ** (len(self.restarts) - 1), BACKOFF_MAX)

    def _keepalive(self):
        """Null packets for the DVR while no engine output is coming (only on a packet boundary, and
        only once video has flowed, so a fresh tune starts exactly as the engine sends it)."""
        if self._pos or not self.had_video:
            return b""
        return NUL_PKT * 7

    # -- the copy loop (ah4c copyFlush) -------------------------------------------

    def iter_bytes(self):
        """Yield engine output until the DVR leaves. Never ends on its own while the DVR is connected."""
        while True:
            with self._lock:
                if self.closed:
                    return
                if self.proc is None:
                    try:
                        self.proc = self._spawn()
                    except Exception as exc:  # noqa: BLE001
                        # End the stream; the caller's close() releases the slot.
                        log.error("[%s] cannot start the engine: %s", self.name, exc)
                        return
                proc = self.proc
                self.reading = True
            why = None
            started = time.monotonic()
            last_byte = last_video = None
            try:
                fd = proc.stdout.fileno()
                while True:
                    try:
                        ready, _, _ = select.select([fd], [], [], 1.0)
                    except (OSError, ValueError):
                        break
                    now = time.monotonic()
                    if not ready:
                        if last_video is not None and now - last_byte >= STALL_TIMEOUT:
                            why = "frozen"
                            break
                        if now - (last_video or started) >= DEAD_TIMEOUT:
                            why = "dead"
                            break
                        if now - (last_byte or started) >= 2:
                            ka = self._keepalive()
                            if ka:
                                yield ka
                        continue
                    try:
                        chunk = os.read(fd, READ_SIZE)
                    except OSError:
                        chunk = b""
                    if not chunk:
                        break
                    last_byte = now
                    if (last_video is None or now - last_video >= 1.0) and _has_video(chunk, self._pos):
                        last_video = now
                        self.had_video = True
                        self.dead_retunes = 0
                    self.bytes_out += len(chunk)
                    self._pos = (self._pos + len(chunk)) % 188
                    yield chunk
                    if now - (last_video or started) >= DEAD_TIMEOUT:
                        why = "dead"
                        break
            finally:
                # Only the reading thread closes the pipe it reads.
                with self._lock:
                    self.reading = False
                try:
                    proc.stdout.close()
                except Exception:  # noqa: BLE001
                    pass
            if why is None:
                try:
                    code = proc.wait(timeout=STOP_TIMEOUT)
                except subprocess.TimeoutExpired:
                    code = "still running, killed"
            with self._lock:
                if self.closed:
                    return
            self._kill(proc)   # the old engine is gone before anything new connects
            with self._lock:
                if self.closed:
                    return
                self.proc = None
            moved = False
            if why in ("frozen", "dead"):
                self.hub._health("problem", self.channel_id, self.cur_key, "frozen" if why == "frozen" else "novideo")
            if why == "frozen":
                log.warning("[%s] engine sent nothing for %.0fs; restarting it on the same line", self.name, STALL_TIMEOUT)
                self.event("restart", "Engine froze for %.0fs; restarted it" % STALL_TIMEOUT, "warn")
            elif why == "dead":
                self.dead_retunes += 1
                if self.dead_retunes % 2 == 0:
                    moved = self.hub._move_line(self)
                if moved:
                    log.warning("[%s] no video for %.0fs; retuning on line %s / %s", self.name, DEAD_TIMEOUT,
                                self.provider_name, self.line_name)
                    self.event("restart", "No video for %.0fs; retuned on %s" % (DEAD_TIMEOUT, self.line_name), "warn")
                else:
                    log.warning("[%s] no video for %.0fs; retuning with a fresh engine", self.name, DEAD_TIMEOUT)
                    self.event("restart", "No video for %.0fs; retuned with a fresh connection" % DEAD_TIMEOUT, "warn")
            else:
                log.warning("[%s] engine exited (code %s); restarting it on the same line", self.name, code)
                self.event("restart", "Engine stopped (code %s); restarted on the same line" % code, "warn")
            if self._pos:   # finish the packet the old engine left half-written
                pad = b"\xff" * (188 - self._pos)
                self._pos = 0
                yield pad
            ka = self._keepalive()
            if ka:
                yield ka
            end = time.monotonic() + self._backoff()
            while True:
                left = end - time.monotonic()
                if left <= 0 or self.closed:
                    break
                time.sleep(min(1.0, left))
                ka = self._keepalive() if left >= 1.0 and not self.closed else b""
                if ka:
                    yield ka

    def close(self):
        """Kill the engine, then release the slot. Safe to call more than once."""
        with self._lock:
            if self.closed:
                return
            self.closed = True
            proc, self.proc = self.proc, None
            reading = self.reading
        self._kill(proc, close_stdout=not reading)
        self.hub._release(self)
        log.info("[%s] %s disconnected after %.0fs, %d bytes; line %s / %s released", self.name,
                 self.client_ip, time.time() - self.started_at, self.bytes_out,
                 self.provider_name, self.line_name)


class Hub:
    def __init__(self, get_config, stream_url=None, engine_command=None):
        self._get_config = get_config
        self._stream_url = stream_url or _default_stream_url
        self._engine_command = engine_command or default_engine_command
        self._lock = threading.Lock()   # guards _slots and _sessions (ah4c tunerLock)
        self._slots = {}                # (provider_id, line_id) -> [Session]
        self._sessions = []
        self.port = None                # the port coax listens on; main may set it (else config)
        self.history_dir = None         # default: <data dir>/cache/history
        self._refs = {}                 # engine log ref -> (provider_id, line_id, stream_id)
        self._bad = {}                  # (provider_id, stream_id) -> [retry after (monotonic), strikes]
        self._released = {}             # (provider_id, line_id) -> monotonic time it was last released
        self.health = None              # app.feedhealth.FeedHealth, set by main

    def _config(self):
        try:
            return self._get_config() or {}
        except Exception:  # noqa: BLE001
            log.exception("hub: could not read config")
            return {}

    @staticmethod
    def _providers(cfg):
        return {p.get("id"): p for p in cfg.get("providers") or [] if p.get("id") is not None}

    @staticmethod
    def _candidates(channel_cfg, providers):
        for stream in channel_cfg.get("streams") or []:
            provider = providers.get(stream.get("provider"))
            if provider is not None and stream.get("stream_id") not in (None, ""):
                yield stream, provider

    def _free_locked(self, provider, line):
        limit = _int(line.get("max_streams"))
        if limit > 0 and len(self._slots.get((provider.get("id"), line.get("id")), ())) >= limit:
            return False
        cap = _int(provider.get("max_streams"))  # provider-wide total; 0 = no cap
        if cap > 0:
            used = sum(len(v) for (pid, _), v in self._slots.items() if pid == provider.get("id"))
            if used >= cap:
                return False
        return True

    def _port(self):
        port = _int(self.port) or _int((self._config().get("server") or {}).get("port"))
        return port if 0 < port < 65536 else 7780

    def _hold_locked(self, session, key):
        self._slots.setdefault(key, []).append(session)

    def _drop_locked(self, session, key):
        holders = self._slots.get(key, [])
        if session in holders:
            holders.remove(session)
            self._released[key] = time.monotonic()
        if not holders:
            self._slots.pop(key, None)

    def _release(self, session):
        """Give back the session's main slot and every backup slot it holds."""
        with self._lock:
            keys = [(session.provider_id, session.line_id)] + list(session.extra_slots)
            session.extra_slots = []
            for key in keys:
                self._drop_locked(session, key)
            if session in self._sessions:
                self._sessions.remove(session)

    # -- what the hub learns from engines ------------------------------------------

    def _remember(self, url, provider_id, line_id, stream_id):
        if len(self._refs) > 5000:
            self._refs.clear()
        for ref in url_refs(url):
            self._refs[ref] = (provider_id, line_id, str(stream_id))

    def _is_bad(self, provider_id, stream_id):
        entry = self._bad.get((provider_id, str(stream_id)))
        return bool(entry) and entry[0] > time.monotonic()

    def _note_bad(self, ref, session=None):
        """An engine gave up on a stream: try it last everywhere for a while (60 s, doubling to 10 min)."""
        hit = self._refs.get(ref)
        if hit is None:
            return
        key = (hit[0], hit[2])
        with self._lock:
            strikes = (self._bad.get(key) or [0, 0])[1]
            self._bad[key] = [time.monotonic() + min(60 * 2 ** strikes, 600), strikes + 1]
        if session is not None:
            self._health("problem", session.channel_id, key, "dead")

    def _desync(self, session, ref, line):
        """Sync check result: a stream in step is checked; one whose sound is off its picture is blocked."""
        hit = self._refs.get(ref)
        if hit is not None and "is in step" in line:
            self._health("verified", session.channel_id, (hit[0], hit[2]))
            return
        m = re.search(r"sound ([\d.]+)s (ahead of|behind)", line)
        if hit is None or m is None:
            return
        with self._lock:
            self._bad[(hit[0], hit[2])] = [time.monotonic() + 86400, 9]
        self._health("problem", session.channel_id, (hit[0], hit[2]), "desync",
                     "%s seconds %s its picture" % (m.group(1), m.group(2)))

    def _blocked(self, channel_id, stream_provider):
        """Feed health blocked this stream (it still plays if every stream of the channel is blocked)."""
        health = self.health
        if health is None:
            return False
        stream, provider = stream_provider
        try:
            return health.is_blocked(channel_id, (str(provider.get("id")), str(stream.get("stream_id"))))
        except Exception:  # noqa: BLE001
            return False

    def _rank(self, channel_id, stream_provider):
        stream, provider = stream_provider
        try:
            return self.health.rank(channel_id, (str(provider.get("id")), str(stream.get("stream_id"))))
        except Exception:  # noqa: BLE001
            return (0, 0.0)

    def _health(self, kind, channel_id, key, *args):
        health = self.health
        if health is None:
            return
        try:
            getattr(health, kind)(channel_id, key, *args)
        except Exception:  # noqa: BLE001 - feed health never breaks streaming
            log.exception("feed health %s failed", kind)

    def _follow(self, session, ref):
        """The engine switched streams: hold the line it now uses, and show which stream it is."""
        hit = self._refs.get(ref)
        if hit is None:
            return
        pid, lid, sid = hit
        self._health("switched", session.channel_id, session.cur_key, (pid, sid))
        session.cur_key = (pid, sid)
        with self._lock:
            self._bad.pop((pid, sid), None)
            for key in list(session.extra_slots):
                self._drop_locked(session, key)
            session.extra_slots = []
            if pid != session.provider_id and session in self._sessions:
                self._hold_locked(session, (pid, lid))
                session.extra_slots.append((pid, lid))
        channel = next((c for c in self._config().get("channels") or []
                        if str(c.get("id")) == str(session.channel_id)), None)
        for i, st in enumerate((channel or {}).get("streams") or []):
            if st.get("provider") == pid and str(st.get("stream_id")) == sid:
                session.stream_no = i + 1
                break

    def _lines(self, provider):
        """Enabled lines, a line released in the last LINE_COOLDOWN seconds last."""
        now = time.monotonic()
        lines = [l for l in provider.get("lines") or [] if l.get("enabled", True)]
        return sorted(lines, key=lambda l: now - self._released.get((provider.get("id"), l.get("id")), -1e9)
                      < LINE_COOLDOWN)

    def _move_line(self, session):
        """Move a session to another free line of its provider (fresh login). False if there is none."""
        provider = self._providers(self._config()).get(session.provider_id)
        if provider is None:
            return False
        with self._lock:
            if session.closed:
                return False
            old = (session.provider_id, session.line_id)
            new = next((l for l in self._lines(provider)
                        if l.get("id") != session.line_id and self._free_locked(provider, l)), None)
            if new is None:
                return False
            self._drop_locked(session, old)
            self._hold_locked(session, (session.provider_id, new.get("id")))
            old_line = (session.line_id, session.line_name)
            session.line_id, session.line_name = new.get("id"), new.get("name") or new.get("id")
        try:
            url = self._stream_url(provider, new, session.stream_id)
        except Exception:  # noqa: BLE001
            url = None
        if not url:
            with self._lock:
                self._drop_locked(session, (session.provider_id, session.line_id))
                session.line_id, session.line_name = old_line
                self._hold_locked(session, old)
            return False
        session.url = url
        self._remember(url, session.provider_id, session.line_id, session.stream_id)
        return True

    def _history(self, session, line):
        """Keep the engine's 10-minute status lines per channel (survives restarts)."""
        try:
            folder = self.history_dir
            if folder is None:
                from app import config as configmod
                folder = os.path.join(configmod.DATA_DIR, "cache", "history")
            os.makedirs(folder, exist_ok=True)
            path = os.path.join(folder, "%s.jsonl" % re.sub(r"[^0-9A-Za-z_-]", "_", str(session.channel_id)))
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps({"t": round(time.time()), "channel": session.name,
                                         "line": session.line_name, "status": line}) + "\n")
            if os.path.getsize(path) > HISTORY_MAX:
                with open(path, encoding="utf-8") as handle:
                    rows = handle.readlines()
                with open(path + ".tmp", "w", encoding="utf-8") as handle:
                    handle.writelines(rows[len(rows) // 2:])
                os.replace(path + ".tmp", path)
        except Exception:  # noqa: BLE001 - history is best effort
            pass

    # -- tune (ah4c tune()) ---------------------------------------------------------

    def open(self, uuid, client_ip, user_agent):
        """Take a line slot for this channel. None means answer 503."""
        cfg = self._config()
        channel_cfg = next((c for c in cfg.get("channels") or [] if str(c.get("uuid")) == str(uuid)), None)
        if channel_cfg is None or not channel_cfg.get("enabled", True):
            return None
        providers = self._providers(cfg)
        cands = sorted(self._candidates(channel_cfg, providers),
                       key=lambda sp: (self._blocked(channel_cfg.get("id"), sp),
                                       self._is_bad(sp[1].get("id"), sp[0].get("stream_id"))))
        for stream, provider in cands:
            if not provider.get("enabled", True):
                continue
            for line in self._lines(provider):
                with self._lock:
                    if not self._free_locked(provider, line):
                        continue
                    # Reserve before building the URL so a concurrent tune cannot take it.
                    session = Session(self, channel_cfg, provider, line, stream, None, client_ip, user_agent)
                    taken = {s.instance for s in self._sessions if str(s.channel_id) == str(session.channel_id)}
                    session.instance = next(i for i in range(len(taken) + 1) if i not in taken)
                    self._hold_locked(session, (provider.get("id"), line.get("id")))
                    self._sessions.append(session)
                try:
                    session.url = self._stream_url(provider, line, str(stream.get("stream_id")))
                except Exception as exc:  # noqa: BLE001
                    log.warning("[%s] cannot build a URL for stream %s on %s/%s: %s", session.name,
                                stream.get("stream_id"), provider.get("id"), line.get("id"), exc)
                if not session.url:
                    self._release(session)
                    continue  # this line can't serve the stream (pattern, missing stream); try the next
                self._remember(session.url, provider.get("id"), line.get("id"), stream.get("stream_id"))
                try:
                    from app.providers import stream_format
                    session.fmt = stream_format(provider, stream) or session.fmt
                except Exception:  # noqa: BLE001
                    pass
                log.info("[%s] %s tuned stream %s on %s / %s", session.name, client_ip, session.stream_id,
                         session.provider_name, session.line_name)
                return session
        log.warning("[%s] no free line for any stream of this channel", channel_cfg.get("name"))
        return None

    # -- queries --------------------------------------------------------------------

    def backup_urls(self, channel_id):
        """Every usable stream of the channel, in order, for the engine's failover.

        Streams on the running session's provider use the session's own line. A stream on
        another provider is included only if that provider has a free line right now; nothing
        is reserved, because the engine reads this list often (status checks) and a held slot
        would block other channels. If a failover and a new tune race for the same last line,
        the provider answers busy and the engine moves on. Disabled providers and lines are
        skipped. Returns [] unless exactly one session runs the channel."""
        cfg = self._config()
        channel_cfg = next((c for c in cfg.get("channels") or [] if str(c.get("id")) == str(channel_id)), None)
        if channel_cfg is None:
            return []
        providers = self._providers(cfg)
        plan = []
        with self._lock:
            mine = [s for s in self._sessions if str(s.channel_id) == str(channel_id)]
            if len(mine) != 1:
                # None: nothing streams this channel, so nothing may connect for it. More than
                # one (two DVR connections on one channel): the request does not say which
                # engine asks, so any URL could land on the other session's line. Each engine
                # then stays on its own stream.
                return []
            own = mine[0]
            cands = sorted(self._candidates(channel_cfg, providers),
                           key=lambda sp: (self._blocked(channel_id, sp),
                                           self._is_bad(sp[1].get("id"), sp[0].get("stream_id"))))
            if any(not self._blocked(channel_id, sp) for sp in cands):
                cands = [sp for sp in cands if not self._blocked(channel_id, sp)]
            # Failover order is by score: the stream that has been least bad comes first. The playing stream
            # stays first in the list so the engine knows where it is; the engine tries the rest in this order.
            if self.health is not None:
                playing = own.cur_key
                cands.sort(key=lambda sp: (0 if (str(sp[1].get("id")), str(sp[0].get("stream_id"))) == playing else 1,
                                           self._rank(channel_id, sp), self._is_bad(sp[1].get("id"), sp[0].get("stream_id"))))
            for stream, provider in cands:
                if not provider.get("enabled", True):
                    continue
                pid = provider.get("id")
                lines = [l for l in provider.get("lines") or [] if l.get("enabled", True)]
                if pid == own.provider_id:
                    line = next((l for l in lines if l.get("id") == own.line_id), None)
                else:
                    held = [k[1] for k in own.extra_slots if k[0] == pid]
                    line = next((l for l in lines if l.get("id") in held), None) or \
                        next((l for l in self._lines(provider) if self._free_locked(provider, l)), None)
                if line is not None:
                    plan.append((provider, line, str(stream.get("stream_id"))))
        urls = []
        for provider, line, stream_id in plan:
            try:
                url = self._stream_url(provider, line, stream_id)
            except Exception as exc:  # noqa: BLE001
                log.warning("backup URL for stream %s on %s failed: %s", stream_id, provider.get("id"), exc)
                continue
            if url:
                self._remember(url, provider.get("id"), line.get("id"), stream_id)
                urls.append(url)
        return urls

    @staticmethod
    def _live_reserve(s):
        """The engine's reserve from its state file (written every second); None if not fresh."""
        script = "tsloop" if str(s.fmt).lower() == "ts" else "hlsloop"
        name = "%s-%s%s.json" % (script, hashlib.md5(("ch%s" % s.channel_id).encode()).hexdigest(),
                                  "~%d" % s.instance if s.instance else "")
        try:
            path = os.path.join("/tmp", name)
            if time.time() - os.path.getmtime(path) > 5:
                return None
            with open(path, encoding="utf-8") as f:
                res = json.load(f).get("res")
            return int(round(float(res))) if res is not None else None
        except (OSError, ValueError, TypeError):
            return None

    def status(self):
        now = time.time()
        with self._lock:
            sessions = list(self._sessions)
        out = []
        for s in sessions:
            up = max(now - s.started_at, 0.001)
            proc = s.proc
            out.append({
                "channel_id": s.channel_id, "uuid": s.uuid, "channel": s.name, "state": "running",
                "provider_id": s.provider_id, "provider": s.provider_name,
                "line_id": s.line_id, "line": s.line_name, "stream_id": s.stream_id,
                "uptime": round(now - s.started_at, 1), "bytes": s.bytes_out,
                "bitrate_bps": int(s.bytes_out * 8 / up), "restarts": len(s.restarts),
                "transcode": {"on": bool(s.encoder), "profile": (self._config().get("transcode") or "off"),
                              "encoder": s.encoder},
                "speed": {"slow": s.slow, "since": s.slow_since,
                          "reserve": self._live_reserve(s) if self._live_reserve(s) is not None else s.reserve,
                          "empty": s.reserve_empty},
                "stream_no": s.stream_no,
                "degraded": s.degraded,
                "events": list(s.events)[-25:],
                "engine_pid": proc.pid if proc is not None and proc.poll() is None else None,
                "clients": [{"ip": s.client_ip, "user_agent": s.user_agent,
                             "connected": round(now - s.started_at, 1), "bytes": s.bytes_out}],
            })
        out.sort(key=lambda item: str(item["channel"]))
        return out

    def line_status(self):
        cfg = self._config()
        with self._lock:
            slots = {k: [(s.channel_id, s.name) for s in v] for k, v in self._slots.items()}
        out = []
        for provider in cfg.get("providers") or []:
            for line in provider.get("lines") or []:
                holders = slots.get((provider.get("id"), line.get("id")), [])
                out.append({
                    "provider_id": provider.get("id"), "provider": provider.get("name") or provider.get("id"),
                    "line_id": line.get("id"), "line": line.get("name") or line.get("id"),
                    "used": len(holders), "max": _int(line.get("max_streams")),
                    "channels": [{"id": cid, "name": name} for cid, name in holders],
                })
        return out

    def shutdown(self):
        with self._lock:
            sessions = list(self._sessions)
        for s in sessions:
            s.close()
