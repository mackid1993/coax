"""Config backups: daily snapshots of config.json, manual backups, and restore.

Backups are plain copies of config.json in /data/backups (mode 0600, they hold provider
passwords). Restoring first saves the current config as a "pre-restore" backup, so a restore
can always be undone.
"""

import json
import logging
import os
import re
import threading
import time

from app import config as configmod

log = logging.getLogger("coax.backups")
NAME = re.compile(r"^config-\d{8}-\d{6}(?:-\d+)?-(auto|manual|pre-restore)\.json$")
_lock = threading.Lock()


def folder(store):
    return os.path.join(store.data_dir, "backups")


def settings(cfg):
    b = cfg.get("backups") or {}
    return {"enabled": b.get("enabled", True) is not False, "time": b.get("time") or "03:30",
            "keep": int(b.get("keep") or 14)}


def _info(path):
    name = os.path.basename(path)
    kind = NAME.match(name).group(1)
    channels = providers = None
    try:
        with open(path, "rb") as handle:
            data = json.loads(handle.read().decode("utf-8-sig"))
        channels, providers = len(data.get("channels") or []), len(data.get("providers") or [])
    except (OSError, ValueError):
        pass
    st = os.stat(path)
    return {"name": name, "kind": kind, "size": st.st_size, "channels": channels, "providers": providers,
            "created": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime))}


def list_backups(store):
    d = folder(store)
    try:
        names = [n for n in os.listdir(d) if NAME.match(n)]
    except FileNotFoundError:
        return []
    paths = sorted((os.path.join(d, n) for n in names), key=os.path.getmtime, reverse=True)
    return [_info(p) for p in paths]


def create(store, kind="manual"):
    with _lock:
        d = folder(store)
        os.makedirs(d, exist_ok=True)
        base = time.strftime("config-%Y%m%d-%H%M%S")
        name, n = "%s-%s.json" % (base, kind), 1
        while os.path.exists(os.path.join(d, name)):
            n += 1
            name = "%s-%d-%s.json" % (base, n, kind)
        configmod.atomic_write(os.path.join(d, name), json.dumps(store.snapshot(), indent=2).encode("utf-8"))
    log.info("config backup saved: %s", name)
    return _info(os.path.join(d, name))


def _path(store, name):
    if not NAME.match(name or ""):
        raise ValueError("not a coax backup name: %s" % name)
    path = os.path.join(folder(store), name)
    if not os.path.isfile(path):
        raise FileNotFoundError(name)
    return path


def read(store, name):
    with open(_path(store, name), "rb") as handle:
        return handle.read()


def delete(store, name):
    os.remove(_path(store, name))
    log.info("config backup deleted: %s", name)


def restore(store, name):
    """Replace the live config with a backup. The current config is backed up first."""
    data = json.loads(read(store, name).decode("utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("%s is not a coax config" % name)
    safety = create(store, "pre-restore")
    store.replace(data)
    log.warning("config restored from %s (previous config saved as %s)", name, safety["name"])
    return {"restored": name, "pre_restore": safety["name"]}


def prune(store, keep):
    autos = [b for b in list_backups(store) if b["kind"] == "auto"]
    for old in autos[keep:]:
        try:
            os.remove(os.path.join(folder(store), old["name"]))
            log.info("old automatic backup removed: %s", old["name"])
        except OSError:
            pass


def _done_today(store):
    today = time.strftime("config-%Y%m%d-")
    return any(b["name"].startswith(today) and b["kind"] == "auto" for b in list_backups(store))


def start(store, interval=30):
    """Daily backup at the configured time (container local time). Runs once per day; if coax was
    down at that time, it runs on the next check after it. Never raises."""
    def loop():
        while True:
            try:
                s = settings(store.get())
                if s["enabled"] and time.strftime("%H:%M") >= s["time"] and not _done_today(store):
                    create(store, "auto")
                    prune(store, s["keep"])
            except Exception:  # noqa: BLE001 - backups never take coax down
                log.exception("scheduled config backup failed")
            time.sleep(interval)
    threading.Thread(target=loop, name="coax-backups", daemon=True).start()
    log.info("config backups: daily, kept in %s", folder(store))
