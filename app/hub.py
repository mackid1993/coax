"""Channel sessions, line slots, fan-out and engine processes.

One engine process runs per active channel and every client of that channel
shares its output (tuner sharing). Engine stdout (MPEG-TS) goes into a ring
buffer bounded by bytes; each client keeps its own cursor into that ring, so a
slow client never slows the others. A client whose cursor falls off the start
of the ring is dropped.

Threading model: one lock (``Hub._lock``) guards every piece of shared state.
Condition variables built on that same lock wake readers (one per channel) and
openers waiting on a channel that is starting or stopping (``Hub._cond``).
Blocking work (spawning is the one exception, it is quick) happens outside
the lock: reading the engine pipes, building URLs, calling get_config().

Behavior modeled on Dispatcharr's live proxy (apps/proxy/live_proxy): a new
client joins ``new_client_behind_seconds`` behind live (or at the oldest data
when the buffer is shorter), shared sessions use no extra slot, and the
channel lingers for ``channel_shutdown_delay`` seconds after the last client.
"""

import collections
import itertools
import logging
import os
import select
import signal
import subprocess
import sys
import threading
import time

log = logging.getLogger("coax.hub")

TS_PACKET = 188
SYNC_BYTE = 0x47
READ_SIZE = TS_PACKET * 348  # about 64 KB, always a whole number of packets
MAX_CLIENT_READ = TS_PACKET * 5577  # about 1 MB handed to a client at once
DEFAULT_USER_AGENT = "VLC/3.0.21 LibVLC/3.0.21"
ENGINE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "engine")

# Channel states
STARTING = "starting"  # slot reserved, URL being built
RUNNING = "running"    # engine running or restarting, clients may attach
STOPPING = "stopping"  # engine being terminated, no new clients
ENDED = "ended"        # slot released, gone from the hub


def default_engine_command(fmt, url, user_agent, channel_id):
    """The engine command from the spec: hlsloop.py for hls, tsloop.py for ts."""
    script = "tsloop.py" if str(fmt).lower() == "ts" else "hlsloop.py"
    return [sys.executable, "-u", os.path.join(ENGINE_DIR, script), url, user_agent, str(channel_id)]


def _default_stream_url(provider, line, stream_id):
    # Imported lazily so the hub has no import-time dependency on providers.
    try:
        from app.providers import stream_url
    except ImportError:
        from app.providers.catalog import stream_url
    return stream_url(provider, line, stream_id)


def _int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class Session:
    """One client attached to a channel. Iterate ``iter_bytes()``, then ``close()``."""

    def __init__(self, hub, channel, client_ip, user_agent, cursor):
        self._hub = hub
        self.channel = channel
        self.channel_id = channel.channel_id
        self.client_ip = client_ip
        self.user_agent = user_agent
        self.cursor = cursor  # sequence number of the next ring chunk to send
        self.connected_at = time.time()
        self.bytes_sent = 0
        self.dropped = False  # fell behind the ring start
        self.closed = False

    def iter_bytes(self):
        """Yield 188-byte aligned MPEG-TS chunks until the channel ends or we are dropped."""
        try:
            while True:
                data = self._hub._read(self)
                if data is None:
                    return
                yield data
        finally:
            self.close()

    def close(self):
        self._hub._detach(self)


class _Channel:
    """A running channel: one slot, one engine process, a ring buffer, its clients."""

    def __init__(self, hub, channel_cfg, provider, line, stream, behind_seconds):
        self.channel_id = channel_cfg.get("id")
        self.uuid = channel_cfg.get("uuid")
        self.name = channel_cfg.get("name") or str(self.channel_id)
        self.provider_id = provider.get("id")
        self.provider_name = provider.get("name") or self.provider_id
        self.line_id = line.get("id")
        self.line_name = line.get("name") or self.line_id
        self.stream_id = str(stream.get("stream_id"))
        self.fmt = provider.get("format") or "hls"
        self.user_agent = provider.get("user_agent") or DEFAULT_USER_AGENT
        self.url = None
        self.behind_seconds = behind_seconds
        self.state = STARTING
        self.cond = threading.Condition(hub._lock)
        self.clients = []
        self.started_at = time.time()
        self.proc = None
        self.restarts = collections.deque()  # monotonic times of restarts
        self.restart_total = 0
        self.slot_held = True
        self.stop_at = None
        self.idle_generation = 0
        # Ring buffer: chunks[i] has sequence number first_seq + i.
        self.chunks = collections.deque()
        self.first_seq = 0
        self.ring_bytes = 0
        self.bytes_in = 0
        self.rate_samples = collections.deque()  # (monotonic time, bytes_in)

    @property
    def next_seq(self):
        return self.first_seq + len(self.chunks)

    def rate(self):
        """Moving average input rate in bytes/second over the sample window, or None."""
        if len(self.rate_samples) < 2:
            return None
        t0, b0 = self.rate_samples[0]
        t1, b1 = self.rate_samples[-1]
        if t1 - t0 < 1.0:
            return None
        return (b1 - b0) / (t1 - t0)


class Hub:
    def __init__(self, get_config, stream_url=None, engine_command=None,
                 restart_limit=5, restart_window=600.0, restart_delay=1.0,
                 ring_margin_seconds=15.0, ring_min_bytes=4 * 1024 * 1024,
                 ring_max_bytes=64 * 1024 * 1024, rate_window=30.0,
                 stop_timeout=5.0):
        self._get_config = get_config
        self._stream_url = stream_url or _default_stream_url
        self._engine_command = engine_command or default_engine_command
        self.restart_limit = restart_limit
        self.restart_window = restart_window
        self.restart_delay = restart_delay
        self.ring_margin_seconds = ring_margin_seconds
        self.ring_min_bytes = ring_min_bytes
        self.ring_max_bytes = ring_max_bytes
        self.rate_window = rate_window
        self.stop_timeout = stop_timeout
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)  # channel state changes
        self._channels = {}  # uuid -> _Channel
        self._slots = {}     # (provider_id, line_id) -> [channel ids holding a slot]
        self._last_delay = 10.0  # last known channel_shutdown_delay

    # ------------------------------------------------------------------ config

    def _config(self):
        try:
            return self._get_config() or {}
        except Exception:
            log.exception("hub: could not read config")
            return {}

    @staticmethod
    def _server_setting(cfg, key, default):
        value = (cfg.get("server") or {}).get(key, default)
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            return float(default)

    @staticmethod
    def _providers(cfg):
        return {p.get("id"): p for p in cfg.get("providers") or [] if p.get("id") is not None}

    # ------------------------------------------------------------------ slots

    def _line_free_locked(self, provider, line):
        limit = _int(line.get("max_streams"), 0)
        used = len(self._slots.get((provider.get("id"), line.get("id")), ()))
        return limit <= 0 or used < limit

    def _reserve_locked(self, provider, line, channel_id):
        self._slots.setdefault((provider.get("id"), line.get("id")), []).append(channel_id)

    def _release_locked(self, ch):
        """Give back the channel's slot. Safe to call more than once; releases only once."""
        if not ch.slot_held:
            return
        ch.slot_held = False
        key = (ch.provider_id, ch.line_id)
        holders = self._slots.get(key, [])
        if ch.channel_id in holders:
            holders.remove(ch.channel_id)
        if not holders:
            self._slots.pop(key, None)

    def _candidates(self, channel_cfg, providers):
        """(stream, provider) pairs of the channel, in order, whose provider exists."""
        for stream in channel_cfg.get("streams") or []:
            provider = providers.get(stream.get("provider"))
            if provider is not None and stream.get("stream_id") not in (None, ""):
                yield stream, provider

    # ------------------------------------------------------------------ open

    def open(self, uuid, client_ip, user_agent):
        """Attach a client to the channel with this uuid. None means answer 503."""
        cfg = self._config()
        channel_cfg = next((c for c in cfg.get("channels") or [] if str(c.get("uuid")) == str(uuid)), None)
        if channel_cfg is None or not channel_cfg.get("enabled", True):
            return None
        behind = self._server_setting(cfg, "new_client_behind_seconds", 30)
        providers = self._providers(cfg)
        deadline = time.monotonic() + self.stop_timeout + 5.0

        while True:
            with self._lock:
                ch = self._channels.get(uuid)
                if ch is not None and ch.state in (STARTING, STOPPING):
                    # Another opener is starting it, or it is shutting down and still holds
                    # its slot. Wait for it to settle, then look again.
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return None
                    self._cond.wait(min(remaining, 0.5))
                    continue
                if ch is not None and ch.state == RUNNING:
                    ch.behind_seconds = behind
                    return self._attach_locked(ch, client_ip, user_agent)
                # No session for this channel: start one.
            session, retry = self._start(uuid, channel_cfg, providers, behind, client_ip, user_agent)
            if not retry:
                return session

    def _start(self, uuid, channel_cfg, providers, behind, client_ip, user_agent):
        """Reserve a slot and start the channel. Returns (session or None, retry)."""
        for stream, provider in self._candidates(channel_cfg, providers):
            for line in provider.get("lines") or []:
                with self._lock:
                    if uuid in self._channels:
                        return None, True  # someone started it meanwhile: go join them
                    if not self._line_free_locked(provider, line):
                        continue
                    ch = _Channel(self, channel_cfg, provider, line, stream, behind)
                    self._reserve_locked(provider, line, ch.channel_id)
                    self._channels[uuid] = ch
                try:
                    url = self._stream_url(provider, line, str(stream.get("stream_id")))
                except Exception as exc:
                    url = None
                    log.warning("[%s] cannot build a URL for stream %s on %s/%s: %s", ch.name,
                                stream.get("stream_id"), provider.get("id"), line.get("id"), exc)
                with self._lock:
                    if not url:
                        self._release_locked(ch)
                        ch.state = ENDED
                        if self._channels.get(uuid) is ch:
                            del self._channels[uuid]
                        self._cond.notify_all()
                        break  # this stream is unusable on any line; try the next stream
                    ch.url = url
                    ch.state = RUNNING
                    session = self._attach_locked(ch, client_ip, user_agent)
                    self._cond.notify_all()
                log.info("[%s] tuned stream %s on %s / %s", ch.name, ch.stream_id, ch.provider_name, ch.line_name)
                threading.Thread(target=self._supervise, args=(ch,), name="coax-engine-%s" % ch.channel_id,
                                 daemon=True).start()
                return session, False

        log.warning("[%s] no free line for any stream of this channel", channel_cfg.get("name"))
        return None, False

    def _attach_locked(self, ch, client_ip, user_agent):
        ch.idle_generation += 1  # cancels a pending shutdown-delay check
        session = Session(self, ch, client_ip, user_agent, self._join_position_locked(ch))
        ch.clients.append(session)
        log.info("[%s] client %s connected (%d watching)", ch.name, client_ip, len(ch.clients))
        return session

    def _join_position_locked(self, ch):
        """Sequence number about behind_seconds back from live, on a chunk (packet) boundary."""
        if ch.behind_seconds <= 0 or not ch.chunks:
            return ch.next_seq
        rate = ch.rate()
        if rate is None:
            return ch.first_seq  # buffer is younger than the estimate: start at the oldest data
        target = rate * ch.behind_seconds
        total = 0
        seq = ch.next_seq
        for chunk in reversed(ch.chunks):
            if total >= target:
                break
            total += len(chunk)
            seq -= 1
        return seq

    # ------------------------------------------------------------------ clients

    def _read(self, session):
        """Next data for one client, or None when it is finished. Blocks while waiting."""
        with self._lock:
            ch = session.channel
            while True:
                if session.closed or session.dropped:
                    return None
                if session.cursor < ch.first_seq:
                    self._drop_locked(ch, session)
                    return None
                if session.cursor < ch.next_seq:
                    parts = []
                    size = 0
                    for chunk in itertools.islice(ch.chunks, session.cursor - ch.first_seq, None):
                        if parts and size + len(chunk) > MAX_CLIENT_READ:
                            break
                        parts.append(chunk)
                        size += len(chunk)
                    session.cursor += len(parts)
                    session.bytes_sent += size
                    break
                if ch.state == ENDED:
                    return None
                ch.cond.wait(1.0)
        return parts[0] if len(parts) == 1 else b"".join(parts)

    def _drop_locked(self, ch, session):
        if session.dropped or session.closed:
            return
        session.dropped = True
        log.warning("[%s] client %s fell too far behind and was dropped", ch.name, session.client_ip)
        self._remove_client_locked(ch, session)

    def _detach(self, session):
        delay = self._server_setting(self._config(), "channel_shutdown_delay", 10)
        with self._lock:
            if session.closed:
                return
            session.closed = True
            ch = session.channel
            if session in ch.clients:
                log.info("[%s] client %s disconnected", ch.name, session.client_ip)
            self._remove_client_locked(ch, session, delay)
            ch.cond.notify_all()

    def _remove_client_locked(self, ch, session, delay=None):
        if session not in ch.clients:
            return
        ch.clients.remove(session)
        if ch.clients or ch.state != RUNNING:
            return
        if delay is None:
            # Called from a drop, under the lock: get_config may be slow, use the last known.
            delay = self._last_delay
        else:
            self._last_delay = delay
        ch.idle_generation += 1
        generation = ch.idle_generation
        if delay <= 0:
            self._stop_locked(ch, "no clients left")
            return
        timer = threading.Timer(delay, self._idle_check, args=(ch, generation))
        timer.daemon = True
        timer.start()

    def _idle_check(self, ch, generation):
        with self._lock:
            if ch.idle_generation == generation and not ch.clients and ch.state == RUNNING:
                self._stop_locked(ch, "no clients for the shutdown delay")

    # ------------------------------------------------------------------ engine

    def _stop_locked(self, ch, why):
        if ch.state != RUNNING:
            return
        log.info("[%s] stopping engine: %s", ch.name, why)
        ch.state = STOPPING
        ch.stop_at = time.monotonic()
        self._signal(ch.proc, signal.SIGTERM)
        ch.cond.notify_all()

    @staticmethod
    def _signal(proc, sig, group=False):
        if proc is None or proc.poll() is not None and not group:
            return
        try:
            if group:
                os.killpg(proc.pid, sig)
            else:
                proc.send_signal(sig)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    def _supervise(self, ch):
        """Run, restart and finally clean up the channel's engine. Never raises."""
        try:
            self._supervise_loop(ch)
        except Exception:
            log.exception("[%s] engine supervisor failed", ch.name)
        finally:
            with self._lock:
                ch.state = ENDED
                self._release_locked(ch)
                if self._channels.get(ch.uuid) is ch:
                    del self._channels[ch.uuid]
                ch.cond.notify_all()
                self._cond.notify_all()
            log.info("[%s] channel closed, slot on %s / %s released", ch.name, ch.provider_name, ch.line_name)

    def _supervise_loop(self, ch):
        while True:
            with self._lock:
                if ch.state != RUNNING:
                    return
                cmd = self._engine_command(ch.fmt, ch.url, ch.user_agent, ch.channel_id)
                try:
                    ch.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                               stderr=subprocess.PIPE, start_new_session=True)
                except OSError as exc:
                    log.error("[%s] could not start the engine: %s", ch.name, exc)
                    ch.proc = None
            proc = ch.proc
            if proc is not None:
                stderr_thread = threading.Thread(target=self._pump_stderr, args=(ch, proc), daemon=True)
                stderr_thread.start()
                self._pump_stdout(ch, proc)
                self._reap(ch, proc)
                stderr_thread.join(2.0)
                code = proc.returncode
            else:
                code = None

            with self._lock:
                if ch.state != RUNNING:
                    return
                if not ch.clients:
                    log.info("[%s] engine exited (code %s) with no clients; not restarting", ch.name, code)
                    return
                now = time.monotonic()
                while ch.restarts and now - ch.restarts[0] > self.restart_window:
                    ch.restarts.popleft()
                if len(ch.restarts) >= self.restart_limit:
                    log.error("[%s] engine exited (code %s); restarted %d times in %.0f minutes, giving up",
                              ch.name, code, len(ch.restarts), self.restart_window / 60)
                    return
                ch.restarts.append(now)
                ch.restart_total += 1
                log.warning("[%s] engine exited (code %s) with %d clients; restarting", ch.name, code,
                            len(ch.clients))
                # Wait out the restart delay, waking early if the channel is stopped.
                end = now + self.restart_delay
                while ch.state == RUNNING and time.monotonic() < end:
                    ch.cond.wait(end - time.monotonic())

    def _pump_stdout(self, ch, proc):
        """Copy engine stdout into the ring in whole packets until EOF or the engine is gone."""
        fd = proc.stdout.fileno()
        carry = b""
        synced = False
        exited_at = None
        while True:
            stop_at = ch.stop_at
            if stop_at is not None and time.monotonic() - stop_at > self.stop_timeout:
                # Stopping, but the engine ignored SIGTERM: kill its whole process group.
                self._signal(proc, signal.SIGKILL, group=True)
            ready, _, _ = select.select([fd], [], [], 0.25)
            if not ready:
                # A dead engine whose orphaned children still hold the pipe would block forever.
                if proc.poll() is not None:
                    exited_at = exited_at or time.monotonic()
                    if time.monotonic() - exited_at > 1.0:
                        return
                continue
            try:
                data = os.read(fd, READ_SIZE)
            except OSError:
                return
            if not data:
                return
            data = carry + data
            if not synced:
                start = data.find(bytes([SYNC_BYTE]))
                if start < 0:
                    carry = b""
                    continue
                data = data[start:]
                synced = True
            whole = len(data) - len(data) % TS_PACKET
            carry = data[whole:]
            if whole:
                self._append(ch, data[:whole])

    def _append(self, ch, chunk):
        with self._lock:
            now = time.monotonic()
            ch.chunks.append(chunk)
            ch.ring_bytes += len(chunk)
            ch.bytes_in += len(chunk)
            if not ch.rate_samples or now - ch.rate_samples[-1][0] >= 0.25:
                ch.rate_samples.append((now, ch.bytes_in))
            else:
                ch.rate_samples[-1] = (ch.rate_samples[-1][0], ch.bytes_in)
            while len(ch.rate_samples) > 2 and now - ch.rate_samples[1][0] > self.rate_window:
                ch.rate_samples.popleft()
            rate = ch.rate()
            if rate is None:
                capacity = self.ring_max_bytes
            else:
                capacity = rate * (ch.behind_seconds + self.ring_margin_seconds)
                capacity = min(self.ring_max_bytes, max(self.ring_min_bytes, capacity))
            while len(ch.chunks) > 1 and ch.ring_bytes > capacity:
                ch.ring_bytes -= len(ch.chunks.popleft())
                ch.first_seq += 1
            # Drop clients the ring has passed now, so they stop counting as viewers.
            for session in [s for s in ch.clients if s.cursor < ch.first_seq]:
                self._drop_locked(ch, session)
            ch.cond.notify_all()

    def _pump_stderr(self, ch, proc):
        try:
            for raw in iter(proc.stderr.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip()
                if line:
                    log.info("[%s] %s", ch.name, line)
        except (OSError, ValueError):
            pass
        finally:
            try:
                proc.stderr.close()
            except OSError:
                pass

    def _reap(self, ch, proc):
        """Wait for the engine to exit; escalate to SIGKILL on its whole process group."""
        try:
            proc.wait(self.stop_timeout)
        except subprocess.TimeoutExpired:
            log.warning("[%s] engine did not exit, killing it", ch.name)
            self._signal(proc, signal.SIGKILL, group=True)
            proc.wait()
        # Clean up any leftover children (ffmpeg) in the engine's session.
        self._signal(proc, signal.SIGKILL, group=True)
        proc.stdout.close()

    # ------------------------------------------------------------------ queries

    def backup_urls(self, channel_id):
        """Every stream of the channel, in order, as URLs the engine can try.

        Streams on the session's provider use the session's line; streams on other
        providers use a currently free line (or their first line when all are busy;
        the engine treats busy answers as retry-and-move-on).
        """
        cfg = self._config()
        channel_cfg = next((c for c in cfg.get("channels") or [] if str(c.get("id")) == str(channel_id)), None)
        if channel_cfg is None:
            return []
        providers = self._providers(cfg)
        plan = []
        with self._lock:
            ch = next((c for c in self._channels.values() if str(c.channel_id) == str(channel_id)
                       and c.state in (RUNNING, STOPPING)), None)
            for stream, provider in self._candidates(channel_cfg, providers):
                lines = provider.get("lines") or []
                if not lines:
                    continue
                line = None
                if ch is not None and provider.get("id") == ch.provider_id:
                    line = next((l for l in lines if l.get("id") == ch.line_id), None)
                if line is None:
                    line = next((l for l in lines if self._line_free_locked(provider, l)), lines[0])
                plan.append((provider, line, str(stream.get("stream_id"))))
        urls = []
        for provider, line, stream_id in plan:
            try:
                url = self._stream_url(provider, line, stream_id)
            except Exception as exc:
                log.warning("backup URL for stream %s on %s failed: %s", stream_id, provider.get("id"), exc)
                continue
            if url:
                urls.append(url)
        return urls

    def status(self):
        """Live channel sessions for the UI."""
        now = time.time()
        with self._lock:
            out = []
            for ch in self._channels.values():
                if ch.state not in (RUNNING, STOPPING):
                    continue
                rate = ch.rate()
                out.append({
                    "channel_id": ch.channel_id,
                    "uuid": ch.uuid,
                    "channel": ch.name,
                    "state": ch.state,
                    "provider_id": ch.provider_id,
                    "provider": ch.provider_name,
                    "line_id": ch.line_id,
                    "line": ch.line_name,
                    "stream_id": ch.stream_id,
                    "uptime": round(now - ch.started_at, 1),
                    "bytes": ch.bytes_in,
                    "bitrate_bps": int(rate * 8) if rate is not None else None,
                    "buffer_bytes": ch.ring_bytes,
                    "restarts": ch.restart_total,
                    "engine_pid": ch.proc.pid if ch.proc is not None and ch.proc.poll() is None else None,
                    "clients": [{
                        "ip": s.client_ip,
                        "user_agent": s.user_agent,
                        "connected": round(now - s.connected_at, 1),
                        "bytes": s.bytes_sent,
                    } for s in ch.clients],
                })
            out.sort(key=lambda item: str(item["channel"]))
            return out

    def line_status(self):
        """Every configured line with used/max slots and the channels holding them."""
        cfg = self._config()
        names = {c.get("id"): c.get("name") for c in cfg.get("channels") or []}
        with self._lock:
            out = []
            for provider in cfg.get("providers") or []:
                for line in provider.get("lines") or []:
                    holders = list(self._slots.get((provider.get("id"), line.get("id")), ()))
                    out.append({
                        "provider_id": provider.get("id"),
                        "provider": provider.get("name") or provider.get("id"),
                        "line_id": line.get("id"),
                        "line": line.get("name") or line.get("id"),
                        "used": len(holders),
                        "max": _int(line.get("max_streams"), 0),
                        "channels": [{"id": cid, "name": names.get(cid)} for cid in holders],
                    })
            return out

    def shutdown(self):
        """Stop every channel (server exit). Clients see end of stream."""
        with self._lock:
            channels = list(self._channels.values())
            for ch in channels:
                if ch.state == RUNNING:
                    self._stop_locked(ch, "server shutting down")
