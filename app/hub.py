"""Tune a channel, run its engine, pipe the engine's output to the DVR.

This follows ah4c's model (github.com/sullrich/ah4c, main.go: tune(), copyFlush(),
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
A/V checks, transcode, viewer buffer. If the engine process exits while the DVR is
still connected, it is restarted on the same line and slot (the engine resumes its
timeline from its own state files), up to RESTART_LIMIT times per RESTART_WINDOW.
"""

import logging
import os
import signal
import subprocess
import sys
import threading
import time

log = logging.getLogger("coax.hub")

READ_SIZE = 64 * 1024
from app import USER_AGENT
ENGINE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "engine")
RESTART_LIMIT = 5
RESTART_WINDOW = 600.0
RESTART_DELAY = 1.0
STOP_TIMEOUT = 3.0


def default_engine_command(fmt, url, user_agent, channel_id):
    """hlsloop.py for HLS providers, tsloop.py for raw MPEG-TS providers."""
    script = "tsloop.py" if str(fmt).lower() == "ts" else "hlsloop.py"
    return [sys.executable, "-u", os.path.join(ENGINE_DIR, script), url, user_agent, str(channel_id)]


def _default_stream_url(provider, line, stream_id):
    from app.providers import stream_url  # lazy: no import-time dependency
    return stream_url(provider, line, stream_id)


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
        self.extra_slots = []       # (provider_id, line_id) held for backups on other providers
        self.instance = 0           # 0 for the first session of this channel, 1 for a second one, ...
        self.proc = None
        self.closed = False
        self._lock = threading.Lock()  # guards proc and closed

    # -- engine process ---------------------------------------------------------

    def _spawn(self):
        cmd = self.hub._engine_command(self.fmt, self.url, self.engine_ua, self.channel_id)
        env = dict(os.environ)
        env["COAX_PORT"] = str(self.hub._port())
        # Two DVR connections can run the same channel at once; keep their engine state files
        # apart. The first session keeps the plain names (so files do not pile up in /tmp).
        env["COAX_SESSION"] = "~%d" % self.instance if self.instance else ""
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, start_new_session=True, bufsize=0, env=env)
        threading.Thread(target=self._pump_stderr, args=(proc,), daemon=True,
                         name="coax-log-%s" % self.channel_id).start()
        return proc

    def _pump_stderr(self, proc):
        try:
            for raw in iter(proc.stderr.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip()
                if line:
                    log.info("[%s] %s", self.name, line)
        except Exception:  # noqa: BLE001
            pass
        finally:
            try:
                proc.stderr.close()
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _kill(proc):
        """Stop the engine and everything it started (ffmpeg), then reap it."""
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
        try:
            proc.stdout.close()
        except Exception:  # noqa: BLE001
            pass

    def _may_restart(self):
        now = time.monotonic()
        self.restarts = [t for t in self.restarts if now - t < RESTART_WINDOW]
        if len(self.restarts) >= RESTART_LIMIT:
            return False
        self.restarts.append(now)
        return True

    # -- the copy loop (ah4c copyFlush) -------------------------------------------

    def iter_bytes(self):
        """Yield engine output until the DVR leaves or the engine gives up."""
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
            fd = proc.stdout.fileno()
            while True:
                try:
                    chunk = os.read(fd, READ_SIZE)
                except OSError:
                    chunk = b""
                if not chunk:
                    break
                self.bytes_out += len(chunk)
                yield chunk
            # Engine output ended.
            code = proc.wait()
            with self._lock:
                if self.closed:
                    return
                self._kill(proc)
                self.proc = None
                if not self._may_restart():
                    log.warning("[%s] engine exited (code %s); restart limit reached, ending the stream",
                                self.name, code)
                    return
            log.warning("[%s] engine exited (code %s); restarting it on the same line", self.name, code)
            time.sleep(RESTART_DELAY)

    def close(self):
        """Kill the engine, then release the slot. Safe to call more than once."""
        with self._lock:
            if self.closed:
                return
            self.closed = True
            proc, self.proc = self.proc, None
        self._kill(proc)
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

    def _release(self, session):
        """Give back the session's main slot and every backup slot it holds."""
        with self._lock:
            keys = [(session.provider_id, session.line_id)] + list(session.extra_slots)
            session.extra_slots = []
            for key in keys:
                holders = self._slots.get(key, [])
                if session in holders:
                    holders.remove(session)
                if not holders:
                    self._slots.pop(key, None)
            if session in self._sessions:
                self._sessions.remove(session)

    # -- tune (ah4c tune()) ---------------------------------------------------------

    def open(self, uuid, client_ip, user_agent):
        """Take a line slot for this channel. None means answer 503."""
        cfg = self._config()
        channel_cfg = next((c for c in cfg.get("channels") or [] if str(c.get("uuid")) == str(uuid)), None)
        if channel_cfg is None or not channel_cfg.get("enabled", True):
            return None
        providers = self._providers(cfg)
        for stream, provider in self._candidates(channel_cfg, providers):
            for line in provider.get("lines") or []:
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
                    break  # this stream is unusable on any line of its provider; try the next stream
                log.info("[%s] %s tuned stream %s on %s / %s", session.name, client_ip, session.stream_id,
                         session.provider_name, session.line_name)
                return session
        log.warning("[%s] no free line for any stream of this channel", channel_cfg.get("name"))
        return None

    # -- queries --------------------------------------------------------------------

    def backup_urls(self, channel_id):
        """Every usable stream of the channel, in order, for the engine's failover.

        Streams on the running session's provider use the session's own line (no extra
        slot). A stream on another provider needs a line slot of its own: the first call
        reserves one free line per provider for the session (kept in session.extra_slots and
        released with the session), later calls reuse it. If a provider has no free line,
        its streams are left out, so no provider ever goes over its line or total limits.
        Returns [] unless exactly one session runs the channel."""
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
            for stream, provider in self._candidates(channel_cfg, providers):
                pid = provider.get("id")
                lines = provider.get("lines") or []
                if pid == own.provider_id:
                    line = next((l for l in lines if l.get("id") == own.line_id), None)
                else:
                    held = next((lid for p, lid in own.extra_slots if p == pid), None)
                    line = next((l for l in lines if l.get("id") == held), None) if held is not None else None
                    if line is None:
                        line = next((l for l in lines if self._free_locked(provider, l)), None)
                        if line is not None:
                            key = (pid, line.get("id"))
                            self._hold_locked(own, key)
                            own.extra_slots.append(key)
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
                urls.append(url)
        return urls

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
