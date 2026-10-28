"""coax entry point: python -m app.main

Loads /data/config.json, starts the stream hub and the provider scheduler, and serves HTTP on server.port.
The data directory defaults to /data; COAX_CONFIG overrides the config path for local tests only.
"""

import importlib
import logging
import os
import signal
import sys
import threading

from app import config as configmod
from app import server as servermod

log = logging.getLogger("coax")


class _Redact(logging.Filter):
    """Masks provider usernames and passwords in every log record, whoever logged it."""

    def filter(self, record):
        from app.hub import _redact
        try:
            msg = record.getMessage()
            clean = _redact(msg)
            if clean != msg:
                record.msg, record.args = clean, None
            if record.exc_info:
                record.exc_text = _redact(logging.Formatter().formatException(record.exc_info))
                record.exc_info = None
        except Exception:  # noqa: BLE001 - logging must never raise
            pass
        return True


def setup_logging():
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    console.addFilter(_Redact())
    root.addHandler(console)
    servermod.LOG_BUFFER.setLevel(logging.INFO)
    servermod.LOG_BUFFER.addFilter(_Redact())
    root.addHandler(servermod.LOG_BUFFER)
    # The request handler logs each request at DEBUG; keep urllib3 chatter out of the UI log.
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def start_hub(store):
    try:
        hub_module = importlib.import_module("app.hub")
        return hub_module.Hub(store.get)
    except Exception:  # noqa: BLE001 - the UI must still come up so the user can see the problem
        log.exception("stream hub failed to start; /stream will answer 503")
        return None


def start_dvr_sync(store):
    """Keep Channels DVR sources of added lineups in step with config changes."""
    try:
        dvr = importlib.import_module("app.channels_dvr")
        return dvr.Syncer(store).register()
    except Exception:  # noqa: BLE001 - coax works without the DVR sync
        log.exception("Channels DVR sync failed to start")
        return None


def set_data_dir(path):
    """Point every module that keeps files under /data at the directory holding config.json."""
    data_dir = os.path.dirname(os.path.abspath(path))
    configmod.DATA_DIR = data_dir
    try:
        guide = importlib.import_module("app.guide")
        guide.CACHE_DIR = os.path.join(data_dir, "cache")
    except Exception as exc:  # noqa: BLE001
        log.warning("guide package not available: %s", exc)


def start_scheduler(store, on_refresh=None):
    try:
        scheduler = importlib.import_module("app.providers.scheduler")
    except Exception as exc:  # noqa: BLE001
        log.warning("provider scheduler not available (%s); refresh only on demand", exc)
        return
    try:
        servermod.call_flexible(scheduler.start, store.get, on_refresh=on_refresh)
        log.info("provider scheduler started")
    except Exception:  # noqa: BLE001
        log.exception("provider scheduler failed to start")


def main(argv=None):
    setup_logging()
    path = os.environ.get("COAX_CONFIG", configmod.DEFAULT_PATH)
    set_data_dir(path)
    store = configmod.ConfigStore(path)
    try:
        cfg = store.load()
    except (configmod.ConfigError, OSError) as exc:
        log.error("cannot load %s: %s", path, exc)
        return 2
    try:
        tzmod = importlib.import_module("app.tz")
        tzmod.apply(cfg)
        store.listeners.append(tzmod.on_change)
    except Exception:  # noqa: BLE001 - never block startup on the time zone
        log.exception("could not set the time zone")
    port = int(cfg["server"].get("port") or 7780)
    hub = start_hub(store)
    if hub is not None:
        hub.port = port
        try:
            importlib.import_module("app.providers.xtream").BUSY_LINES[0] = hub.busy_lines
        except Exception:  # noqa: BLE001 - catalog calls then use the first line
            log.exception("could not tell the provider client which lines are streaming")
    app = servermod.App(store, hub)
    try:
        app.health = importlib.import_module("app.feedhealth").FeedHealth(store, os.path.dirname(os.path.abspath(path)))
        if hub is not None:
            hub.health = app.health
    except Exception:  # noqa: BLE001 - coax streams without feed health
        log.exception("feed health failed to start")
    app.dvr = start_dvr_sync(store)
    try:
        importlib.import_module("app.snapshots").start(store)
    except Exception:  # noqa: BLE001 - backups are optional
        log.exception("config backup scheduler failed to start")
    try:
        importlib.import_module("app.cleanup").start(store, os.path.dirname(os.path.abspath(path)))
    except Exception:  # noqa: BLE001 - cleanup is optional
        log.exception("cleanup scheduler failed to start")
    try:
        httpd = servermod.make_server(app, "", port)
    except OSError as exc:
        log.error("cannot listen on port %d: %s", port, exc)
        return 2

    stopping = threading.Event()

    def stop(signum, _frame):
        if stopping.is_set():
            return
        stopping.set()
        log.info("signal %d received, shutting down", signum)
        # shutdown() blocks until serve_forever returns, so call it from another thread.
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    start_scheduler(store, app.on_refresh)
    log.info("coax listening on port %d (config %s)", port, path)
    try:
        httpd.serve_forever(poll_interval=0.5)
    finally:
        httpd.server_close()
        try:
            scheduler = sys.modules.get("app.providers.scheduler")
            if scheduler is not None and callable(getattr(scheduler, "stop", None)):
                scheduler.stop(2)
        except Exception:  # noqa: BLE001
            log.exception("error stopping the scheduler")
        if hub is not None:
            for name in ("shutdown", "close", "stop"):
                func = getattr(hub, name, None)
                if callable(func):
                    try:
                        func()
                    except Exception:  # noqa: BLE001
                        log.exception("error stopping the hub")
                    break
        log.info("coax stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
