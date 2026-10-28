"""Daily cleanup of files coax no longer needs.

Engines write small state files in /tmp (one per channel, per shared session) and coax keeps caches in
<data>/cache. Without cleanup these pile up as channels, providers and lineups come and go. Once at start
and then every CLEANUP_INTERVAL, remove:

- /tmp: engine state files (hlsloop-*, tsloop-*) not touched for a day. A running engine rewrites its
  own file every second, so live channels are never affected.
- <data>/cache: half-written temp files older than a day, engine recording-job files older than a day,
  caches for providers, XMLTV sources and lineups that no longer exist, health history for deleted
  channels, and Gracenote region indexes not used for STALE_DAYS.

Config backups are handled by app.snapshots and are not touched here. Never raises.
"""

import logging
import os
import re
import threading
import time

log = logging.getLogger("coax.cleanup")

CLEANUP_INTERVAL = 24 * 3600
DAY = 24 * 3600
STALE_DAYS = 30
TMP_DIR = "/tmp"


def _age(path, now):
    try:
        return now - os.path.getmtime(path)
    except OSError:
        return 0


def _remove(path):
    try:
        os.remove(path)
        return 1
    except OSError:
        return 0


def _ids(items):
    return {str(x.get("id")) for x in items or [] if isinstance(x, dict) and x.get("id") is not None}


def run(cfg, data_dir, tmp_dir=TMP_DIR, now=None):
    """One cleanup pass. Returns the number of files removed."""
    now = time.time() if now is None else now
    removed = 0

    # /tmp: engine state and hardware-probe files.
    try:
        for name in os.listdir(tmp_dir):
            if re.match(r"^(hlsloop|tsloop)-", name):
                path = os.path.join(tmp_dir, name)
                limit = 3600 if "-sync-" in name else DAY
                if os.path.isfile(path) and _age(path, now) > limit:
                    removed += _remove(path)
    except OSError:
        pass

    cache = os.path.join(data_dir, "cache")
    providers = _ids(cfg.get("providers")) | _ids(cfg.get("xmltv_sources"))
    lineups = _ids(cfg.get("lineups"))
    channels = _ids(cfg.get("channels"))
    try:
        names = os.listdir(cache)
    except OSError:
        names = []
    for name in names:
        path = os.path.join(cache, name)
        if not os.path.isfile(path):
            continue
        age = _age(path, now)
        m_cat = re.match(r"^catalog-(.+)\.json$", name)
        m_ids = re.match(r"^xmltv-ids-(.+)\.json$", name)
        m_guide = re.match(r"^xmltv-(.+)\.xml$", name)
        if name.startswith(".tmp") or name.endswith(".tmp"):
            stale = age > DAY
        elif re.match(r"^(hlsloop|tsloop)-jobs-", name):
            stale = age > DAY
        elif m_cat:
            stale = m_cat.group(1) not in providers and age > DAY
        elif m_ids:
            stale = m_ids.group(1) not in providers and age > DAY
        elif m_guide:
            stale = m_guide.group(1) not in lineups and age > DAY
        elif name.startswith("gracenote-index-"):
            stale = age > STALE_DAYS * DAY
        else:
            stale = False
        if stale:
            removed += _remove(path)

    history = os.path.join(cache, "history")
    try:
        for name in os.listdir(history):
            path = os.path.join(history, name)
            m = re.match(r"^(.+)\.jsonl(\.tmp)?$", name)
            if m and os.path.isfile(path):
                gone = m.group(1) not in channels or m.group(2)
                if (gone and _age(path, now) > DAY) or _age(path, now) > STALE_DAYS * DAY:
                    removed += _remove(path)
    except OSError:
        pass
    return removed


def start(store, data_dir, interval=CLEANUP_INTERVAL):
    def loop():
        while True:
            try:
                n = run(store.get(), data_dir)
                if n:
                    log.info("cleanup: removed %d old file%s", n, "" if n == 1 else "s")
            except Exception:  # noqa: BLE001 - cleanup never takes coax down
                log.exception("cleanup failed")
            time.sleep(interval)
    threading.Thread(target=loop, name="coax-cleanup", daemon=True).start()
    log.info("cleanup: daily, for old engine state and caches")
