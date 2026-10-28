"""Backups: daily snapshots of coax's data, manual backups, and restore.

A backup is one .tar.gz in /data/backups (mode 0600, it holds provider passwords) with:
  config.json            settings, providers, channels, lineups
  feedhealth.json        Smart Standby history and blocks (cache/feedhealth.json)
  notifications.json     the bell
  history/*.jsonl        each channel's 10-minute stream history (cache/history)
Caches coax rebuilds on its own (catalogs, guides, Gracenote index) are left out.

Restoring first saves the current state as a "pre-restore" backup, so a restore can always be undone.
Older backups that are a plain config.json are still listed and restore the config only.
"""

import io
import json
import logging
import os
import re
import tarfile
import threading
import time

from app import config as configmod

log = logging.getLogger("coax.backups")
NAME = re.compile(r"^config-\d{8}-\d{6}(?:-\d+)?-(auto|manual|pre-restore)\.(json|tar\.gz)$")
_lock = threading.Lock()


def folder(store):
    return os.path.join(store.data_dir, "backups")


def settings(cfg):
    b = cfg.get("backups") or {}
    return {"enabled": b.get("enabled", True) is not False, "time": b.get("time") or "03:30",
            "keep": int(b.get("keep") or 14)}


def _extras(store):
    """(name in the archive, path on disk) for the data files that go with the config."""
    d = store.data_dir
    out = [("feedhealth.json", os.path.join(d, "cache", "feedhealth.json")),
           ("notifications.json", os.path.join(d, "notifications.json"))]
    hist = os.path.join(d, "cache", "history")
    try:
        for n in sorted(os.listdir(hist)):
            if n.endswith(".jsonl"):
                out.append(("history/" + n, os.path.join(hist, n)))
    except OSError:
        pass
    return [(a, p) for a, p in out if os.path.isfile(p)]


def _config_of(path):
    if path.endswith(".json"):
        with open(path, "rb") as handle:
            return json.loads(handle.read().decode("utf-8-sig"))
    with tarfile.open(path, "r:gz") as tar:
        return json.loads(tar.extractfile("config.json").read().decode("utf-8-sig"))


def _info(path):
    name = os.path.basename(path)
    kind = NAME.match(name).group(1)
    channels = providers = None
    try:
        data = _config_of(path)
        channels, providers = len(data.get("channels") or []), len(data.get("providers") or [])
    except (OSError, ValueError, KeyError, tarfile.TarError, AttributeError):
        pass
    st = os.stat(path)
    return {"name": name, "kind": kind, "size": st.st_size, "channels": channels, "providers": providers,
            "full": name.endswith(".tar.gz"),
            "created": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime))}


def list_backups(store):
    d = folder(store)
    try:
        names = [n for n in os.listdir(d) if NAME.match(n)]
    except FileNotFoundError:
        return []
    paths = sorted((os.path.join(d, n) for n in names), key=os.path.getmtime, reverse=True)
    return [_info(p) for p in paths]


def _add(tar, arcname, data):
    ti = tarfile.TarInfo(arcname)
    ti.size = len(data)
    ti.mtime = int(time.time())
    ti.mode = 0o600
    tar.addfile(ti, io.BytesIO(data))


def create(store, kind="manual"):
    with _lock:
        d = folder(store)
        os.makedirs(d, exist_ok=True)
        base = time.strftime("config-%Y%m%d-%H%M%S")
        name, n = "%s-%s.tar.gz" % (base, kind), 1
        while os.path.exists(os.path.join(d, name)):
            n += 1
            name = "%s-%d-%s.tar.gz" % (base, n, kind)
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            _add(tar, "config.json", json.dumps(store.snapshot(), indent=2).encode("utf-8"))
            for arc, path in _extras(store):
                try:
                    with open(path, "rb") as handle:
                        _add(tar, arc, handle.read())
                except OSError:
                    pass
        configmod.atomic_write(os.path.join(d, name), buf.getvalue())
    log.info("backup saved: %s", name)
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
    log.info("backup deleted: %s", name)


def _write_file(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".restore-tmp"
    with open(tmp, "wb") as handle:
        handle.write(data)
    os.replace(tmp, path)


def restore(store, name, on_data_restored=None):
    """Replace the live data with a backup. The current state is backed up first."""
    path = _path(store, name)
    data = _config_of(path)
    if not isinstance(data, dict):
        raise ValueError("%s is not a coax config" % name)
    extras = {}
    if path.endswith(".tar.gz"):
        with tarfile.open(path, "r:gz") as tar:
            for m in tar.getmembers():
                arc = m.name
                if not m.isfile():
                    continue
                if arc in ("feedhealth.json", "notifications.json") or re.match(r"^history/[0-9A-Za-z_-]+\.jsonl$", arc):
                    extras[arc] = tar.extractfile(m).read()
    safety = create(store, "pre-restore")
    store.replace(data)
    d = store.data_dir
    for arc, blob in extras.items():
        if arc == "feedhealth.json":
            target = os.path.join(d, "cache", "feedhealth.json")
        elif arc == "notifications.json":
            target = os.path.join(d, "notifications.json")
        else:
            target = os.path.join(d, "cache", arc)
        try:
            _write_file(target, blob)
        except OSError as exc:
            log.warning("could not restore %s: %s", arc, exc)
    if extras and on_data_restored is not None:
        try:
            on_data_restored()
        except Exception:  # noqa: BLE001
            log.exception("reloading restored data failed")
    log.warning("restored from %s (previous state saved as %s)", name, safety["name"])
    return {"restored": name, "pre_restore": safety["name"], "files": sorted(["config.json"] + list(extras))}


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
                log.exception("scheduled backup failed")
            time.sleep(interval)
    threading.Thread(target=loop, name="coax-backups", daemon=True).start()
    log.info("backups: daily, kept in %s", folder(store))
