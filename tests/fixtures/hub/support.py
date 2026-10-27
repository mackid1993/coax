"""Shared helpers for the hub tests: config builder, fake URL builder, fake engine command."""

import logging
import os
import struct
import sys
import threading
import time

FAKE_ENGINE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_engine.py")

logging.getLogger("coax.hub").setLevel(logging.CRITICAL)


def fake_stream_url(provider, line, stream_id):
    return "fake://%s/%s/%s" % (provider["id"], line["id"], stream_id)


def provider(pid, *line_limits, fmt="hls"):
    return {
        "id": pid, "name": "Provider " + pid, "type": "xtream", "server": "http://x", "format": fmt,
        "user_agent": "", "lines": [{"id": "l%d" % (i + 1), "name": "Line %d" % (i + 1), "username": "u",
                                     "password": "p", "max_streams": n} for i, n in enumerate(line_limits)],
    }


def channel(cid, streams, enabled=True):
    return {"id": cid, "uuid": "uuid-%d" % cid, "name": "Channel %d" % cid, "enabled": enabled,
            "streams": [{"provider": p, "stream_id": s} for p, s in streams]}


def config(providers, channels, behind=0, delay=0):
    return {"server": {"new_client_behind_seconds": behind, "channel_shutdown_delay": delay},
            "providers": providers, "channels": channels}


class EngineCommand:
    """Engine command builder that records calls and passes extra fake-engine flags."""

    def __init__(self, *flags):
        self.flags = list(flags)
        self.calls = []
        self.lock = threading.Lock()

    def __call__(self, fmt, url, user_agent, channel_id):
        with self.lock:
            self.calls.append((fmt, url, user_agent, channel_id))
        return [sys.executable, "-u", FAKE_ENGINE, url, user_agent, str(channel_id)] + self.flags

    def count(self):
        with self.lock:
            return len(self.calls)


class Reader(threading.Thread):
    """Drains a session in the background, checking packet alignment."""

    def __init__(self, session, delay=0.0):
        super().__init__(daemon=True)
        self.session = session
        self.delay = delay
        self.bytes = 0
        self.chunks = 0
        self.misaligned = 0
        self.first_counter = None
        self.last_counter = None
        self.finished = threading.Event()
        self.stop_flag = threading.Event()

    def run(self):
        try:
            for data in self.session.iter_bytes():
                if len(data) % 188 or data[0] != 0x47:
                    self.misaligned += 1
                if self.first_counter is None:
                    self.first_counter = struct.unpack(">I", data[1:5])[0]
                self.last_counter = struct.unpack(">I", data[-187:-183])[0]
                self.bytes += len(data)
                self.chunks += 1
                if self.stop_flag.is_set():
                    break
                if self.delay:
                    time.sleep(self.delay)
        finally:
            self.session.close()
            self.finished.set()

    def stop(self):
        self.stop_flag.set()
        self.session.close()


def wait_for(predicate, timeout=5.0, interval=0.02):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()
