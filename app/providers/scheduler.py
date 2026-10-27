"""Background refresh: provider catalogs on their refresh_hours schedule, then the guide.

Everything runs on one daemon thread and every error is caught and logged; a provider outage
only means the last good catalog keeps being served.
"""

import datetime
import logging
import threading
import time

from . import catalog

log = logging.getLogger("coax.providers")

TICK_SECONDS = 60           # how often the loop checks what is due
RETRY_AFTER_FAILURE = 1800  # wait this long before retrying a failed provider (capped by refresh_hours)
GUIDE_REFRESH_HOURS = 12    # the guide also refreshes on its own at this interval
DEFAULT_REFRESH_HOURS = 24

_state_lock = threading.Lock()
_results = {}  # provider id -> last refresh result summary (for the Status page)
_thread = None
_stop = threading.Event()
_wake = threading.Event()

# Overridable in tests.
clock = time.time


def _refresh_hours(provider):
    try:
        hours = float(provider.get("refresh_hours") or DEFAULT_REFRESH_HOURS)
    except (TypeError, ValueError):
        hours = DEFAULT_REFRESH_HOURS
    return max(hours, 0.25)


def last_results():
    """Copy of the last refresh outcome per provider id."""
    with _state_lock:
        return {pid: dict(result) for pid, result in _results.items()}


def _record(provider_id, result):
    with _state_lock:
        _results[str(provider_id)] = result


def refresh_provider(provider, on_refresh=None):
    """Refresh one provider, record the outcome, never raise. Returns the summary dict."""
    pid = provider.get("id")
    started = clock()
    summary = {"provider": pid, "at": catalog.now_iso()}
    try:
        result = catalog.refresh(provider)
    except Exception as exc:  # noqa: BLE001 - a refresh must never take the server down
        from .net import redact, secrets_of
        message = redact(exc, secrets_of(provider))
        log.error("provider %s refresh failed, keeping the last good catalog: %s", pid, message)
        summary.update(ok=False, error=message, seconds=round(clock() - started, 1))
        _record(pid, summary)
        return summary
    summary.update(
        ok=True, error="", seconds=round(clock() - started, 1),
        streams=len(result["streams"]), groups=len(result["groups"]),
        added=result["added"], removed=result["removed"],
        added_selected=result["added_selected"], removed_selected=result["removed_selected"],
        updated=result["updated"],
    )
    _record(pid, summary)
    if on_refresh is not None:
        # Hook for the web layer, e.g. auto_add of new streams as disabled channels.
        try:
            on_refresh(provider, result)
        except Exception:  # noqa: BLE001
            log.exception("on_refresh hook failed for provider %s", pid)
    return summary


def refresh_guide(config):
    """Run the guide refresh if the guide module exists. Never raises."""
    try:
        from app.guide import xmltv
    except ImportError:
        return False
    try:
        xmltv.refresh(config)
    except Exception:  # noqa: BLE001
        log.exception("guide refresh failed")
        return False
    return True


def _is_due(provider, now, next_retry):
    """Due when the cache is missing or older than refresh_hours, and not in failure backoff."""
    pid = str(provider.get("id"))
    if now < next_retry.get(pid, 0):
        return False
    cached = catalog.load(pid)
    if cached is None:
        return True
    age = catalog.age_hours(cached)
    return age is None or age >= _refresh_hours(provider)


def run_once(get_config, state, on_refresh=None):
    """One scheduler pass. `state` carries backoff and guide timing between passes."""
    try:
        config = get_config() or {}
    except Exception:  # noqa: BLE001
        log.exception("scheduler could not read config")
        return
    now = clock()
    next_retry = state.setdefault("next_retry", {})
    refreshed = False
    for provider in config.get("providers") or []:
        if state.get("stop") is not None and state["stop"].is_set():
            return
        if not isinstance(provider, dict) or not provider.get("id"):
            continue
        try:
            if not _is_due(provider, now, next_retry):
                continue
            summary = refresh_provider(provider, on_refresh)
        except Exception:  # noqa: BLE001
            log.exception("scheduler error for provider %s", provider.get("id"))
            continue
        pid = str(provider["id"])
        if summary.get("ok"):
            next_retry.pop(pid, None)
            refreshed = True
        else:
            wait = min(RETRY_AFTER_FAILURE, _refresh_hours(provider) * 3600)
            next_retry[pid] = clock() + wait

    guide_due = clock() - state.get("guide_at", 0) >= GUIDE_REFRESH_HOURS * 3600
    if refreshed or guide_due or state.pop("guide_requested", False):
        state["guide_at"] = clock()
        try:
            refresh_guide(get_config() or config)
        except Exception:  # noqa: BLE001
            log.exception("guide refresh failed")


def _loop(get_config, on_refresh, state):
    log.info("refresh scheduler started")
    stop_event = state["stop"]
    while not stop_event.is_set():
        try:
            run_once(get_config, state, on_refresh)
        except Exception:  # noqa: BLE001 - belt and braces; run_once already catches
            log.exception("scheduler pass failed")
        _wake.wait(TICK_SECONDS)
        _wake.clear()
    log.info("refresh scheduler stopped")


def start(get_config, on_refresh=None):
    """Start the daemon thread once. Never raises. Returns the thread (or None on failure).

    get_config: callable returning the current config dict.
    on_refresh: optional callable(provider, result) after each successful refresh.
    """
    global _thread, _stop
    try:
        with _state_lock:
            if _thread is not None and _thread.is_alive():
                return _thread
            _stop = threading.Event()
            _wake.clear()
            _thread = threading.Thread(target=_loop, args=(get_config, on_refresh, {"stop": _stop}),
                                       name="coax-refresh", daemon=True)
            _thread.start()
            return _thread
    except Exception:  # noqa: BLE001
        log.exception("could not start the refresh scheduler")
        return None


def wake():
    """Ask the scheduler to check now (for example after a provider was added)."""
    _wake.set()


def stop(timeout=5):
    """Stop the thread (used by tests and clean shutdown)."""
    global _thread
    _stop.set()
    _wake.set()
    thread = _thread
    if thread is not None:
        thread.join(timeout)
    _thread = None
