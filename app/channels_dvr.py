"""Channels DVR integration: add a lineup as a custom M3U source and keep it in sync.

Adapted from ah4c's m3uintegration.go (github.com/sullrich/ah4c; MIT-style license,
Copyright 2023 Fancy Bits, LLC): PUT /providers/m3u/sources/<key>, then POST .../refresh.

add_or_update() creates or updates the DVR source for one lineup, reloads its channels and starts a guide
download. Syncer watches config changes and, a few seconds after the last one, refreshes the DVR source of every
lineup that was added this way (plus a guide download when channels or guide ids changed).

DVR endpoints used (all JSON, values are strings):
  PUT  /providers/m3u/sources/{key}            create or update a source
  GET  /providers/m3u/sources/{key}            read a source
  POST /providers/m3u/sources/{key}/refresh    reload the source's channel list
  GET  /dvr/lineups                            {"M3U-<key>": "<lineup id>", ...}
  PUT  /dvr/lineups/{lineup id}                download guide data for that lineup
"""

import copy
import logging
import re
import threading
import time

import requests

log = logging.getLogger("coax.dvr")

USER_AGENT = "coax"
TIMEOUT = 10
REFRESH_TIMEOUT = 30
LOOKUP_TRIES = 5          # how often to look for the DVR lineup id of a fresh source
LOOKUP_DELAY = 2.0        # seconds between lookups (tests set 0)

# DVR guide options and how they map to lineup["guide"] in the config.
GUIDE_OPTIONS = ("gracenote", "xmltv", "gracenote_xmltv")
GUIDE_TO_LINEUP = {"gracenote": "gracenote", "xmltv": "xmltv", "gracenote_xmltv": "both"}
LINEUP_TO_GUIDE = {"gracenote": "gracenote", "xmltv": "xmltv", "both": "gracenote_xmltv",
                   "per-channel": "gracenote_xmltv"}
NUMBERING_VALUES = ("", "ignore")
LOGO_VALUES = ("", "guide")


class DvrError(Exception):
    """A Channels DVR problem with a message meant for the user. status 400 = coax setup, 502 = DVR side."""

    def __init__(self, message, status=502):
        super().__init__(message)
        self.message = message
        self.status = status


# ---------------------------------------------------------------- small helpers

def source_key(lineup_id):
    """DVR source key for a lineup: 'coax' plus the lineup id with only letters and digits."""
    return "coax" + re.sub(r"[^A-Za-z0-9]", "", str(lineup_id))


def device_id(lineup_id):
    return "M3U-" + source_key(lineup_id)


def _find_lineup(cfg, lineup_id):
    for lineup in cfg.get("lineups") or []:
        if lineup.get("id") == lineup_id:
            return lineup
    raise DvrError("unknown lineup %s" % lineup_id, 404)


def dvr_url(cfg):
    url = ((cfg.get("channels_dvr") or {}).get("url") or "").strip().rstrip("/")
    if not url:
        raise DvrError("set the Channels DVR URL in Settings first", 400)
    return url


def base_url(cfg):
    url = ((cfg.get("server") or {}).get("base_url") or "").strip().rstrip("/")
    if not url:
        raise DvrError("set the coax base URL in Settings first (the DVR needs it to reach coax)", 400)
    return url


def output_urls(cfg, lineup_id):
    base = base_url(cfg)
    return base + "/output/m3u/%s.m3u" % lineup_id, base + "/output/xmltv/%s.xml" % lineup_id


def default_limit(cfg, lineup_id):
    """Total usable streams across the providers of the lineup's channels, or '' when any is unlimited."""
    lineup = _find_lineup(cfg, lineup_id)
    members = set(lineup.get("channels") or [])
    used = set()
    for channel in cfg.get("channels") or []:
        if channel.get("id") in members and channel.get("enabled", True):
            used.update(ref.get("provider") for ref in channel.get("streams") or [])
    total = 0
    for provider in cfg.get("providers") or []:
        if provider.get("id") not in used:
            continue
        lines = [int(line.get("max_streams") or 0) for line in provider.get("lines") or []]
        line_total = 0 if (not lines or 0 in lines) else sum(lines)   # 0 means unlimited
        cap = int(provider.get("max_streams") or 0)
        if line_total and cap:
            streams = min(line_total, cap)
        else:
            streams = line_total or cap
        if not streams:
            return ""
        total += streams
    return str(total) if total else ""


def defaults(cfg, lineup_id):
    """Prefilled options for the Add to Channels DVR dialog (stored choices win)."""
    lineup = _find_lineup(cfg, lineup_id)
    stored = lineup.get("dvr") or {}
    return {
        "guide": LINEUP_TO_GUIDE.get(lineup.get("guide"), "gracenote_xmltv"),
        "limit": stored.get("limit", default_limit(cfg, lineup_id)),
        "default_limit": default_limit(cfg, lineup_id),
        "numbering": stored.get("numbering", ""),
        "start_number": stored.get("start_number", ""),
        "logos": stored.get("logos", "guide"),
        "name": stored.get("name") or "coax " + (lineup.get("name") or lineup_id),
    }


def clean_opts(cfg, lineup_id, opts):
    """Validate dialog options and fill defaults. Raises DvrError(400)."""
    opts = dict(opts or {})
    out = defaults(cfg, lineup_id)
    out.pop("default_limit")
    for key in out:
        if opts.get(key) is not None:
            out[key] = str(opts[key]).strip()
    if out["guide"] not in GUIDE_OPTIONS:
        raise DvrError("guide must be one of " + ", ".join(GUIDE_OPTIONS), 400)
    if out["limit"] and not (out["limit"].isdigit() and int(out["limit"]) > 0):
        raise DvrError("stream limit must be a positive whole number or empty", 400)
    if out["numbering"] not in NUMBERING_VALUES:
        raise DvrError("numbering must be empty or 'ignore'", 400)
    if out["numbering"] == "ignore":
        if not out["start_number"].isdigit():
            raise DvrError("start number must be a whole number", 400)
    else:
        out["start_number"] = ""
    if out["logos"] not in LOGO_VALUES:
        raise DvrError("logos must be empty or 'guide'", 400)
    if not out["name"]:
        out["name"] = "coax " + (_find_lineup(cfg, lineup_id).get("name") or lineup_id)
    return out


def source_payload(cfg, lineup_id, opts):
    """JSON body for PUT /providers/m3u/sources/{key}."""
    m3u_url, xml_url = output_urls(cfg, lineup_id)
    guide = opts["guide"]
    return {
        "name": opts["name"], "type": "MPEG-TS", "source": "URL", "url": m3u_url, "text": "",
        "refresh": "24", "limit": opts["limit"], "satip": "",
        "numbering": opts["numbering"], "start_number": opts["start_number"], "logos": opts["logos"],
        "xmltv_url": "" if guide == "gracenote" else xml_url, "xmltv_refresh": "",
        "guide_preference": "xmltv" if guide == "xmltv" else "gracenote",
    }


# ---------------------------------------------------------------- DVR HTTP calls

def _call(cfg, method, path, body=None, timeout=TIMEOUT, missing_ok=False):
    """One DVR request. Returns parsed JSON (or text), None for a 404 when missing_ok. Raises DvrError."""
    url = dvr_url(cfg) + path
    try:
        resp = requests.request(method, url, json=body, timeout=timeout, headers={"User-Agent": USER_AGENT})
    except requests.RequestException as exc:
        raise DvrError("could not reach Channels DVR at %s: %s" % (dvr_url(cfg), exc))
    if resp.status_code == 404 and missing_ok:
        return None
    if not 200 <= resp.status_code < 300:
        detail = (resp.text or "").strip()[:300] or resp.reason
        raise DvrError("Channels DVR answered %s for %s %s: %s" % (resp.status_code, method, path, detail))
    try:
        return resp.json()
    except ValueError:
        return resp.text


def refresh_source(cfg, lineup_id):
    _call(cfg, "POST", "/providers/m3u/sources/%s/refresh" % source_key(lineup_id), timeout=REFRESH_TIMEOUT)


def lookup_lineup(cfg, lineup_id, tries=None):
    """DVR lineup id for this source (for example 'X-M3U'), or '' when the DVR has not made one yet."""
    tries = LOOKUP_TRIES if tries is None else tries
    for attempt in range(max(1, tries)):
        data = _call(cfg, "GET", "/dvr/lineups")
        if isinstance(data, dict) and data.get(device_id(lineup_id)):
            return str(data[device_id(lineup_id)])
        if attempt < tries - 1:
            time.sleep(LOOKUP_DELAY)
    return ""


def download_guide(cfg, lineup_id, tries=None):
    """Ask the DVR to fetch guide data for this source. Returns the DVR lineup id or '' when not found yet."""
    dvr_lineup = lookup_lineup(cfg, lineup_id, tries)
    if dvr_lineup:
        _call(cfg, "PUT", "/dvr/lineups/%s" % dvr_lineup, timeout=REFRESH_TIMEOUT)
    return dvr_lineup


def status(cfg, lineup_id):
    """{"in_dvr": bool, "source": dict | None, "error": str}. Never raises for DVR problems."""
    _find_lineup(cfg, lineup_id)
    try:
        data = _call(cfg, "GET", "/providers/m3u/sources/%s" % source_key(lineup_id), missing_ok=True)
    except DvrError as exc:
        return {"in_dvr": False, "source": None, "error": exc.message}
    if not isinstance(data, dict) or data.get("error") or not (data.get("url") or data.get("name")):
        return {"in_dvr": False, "source": None, "error": ""}
    return {"in_dvr": True, "source": data, "error": ""}


# ---------------------------------------------------------------- add or update

def add_or_update(store, lineup_id, opts):
    """Create or update the DVR source for a lineup, reload its channels and start a guide download.

    store is the ConfigStore (the lineup's guide choice and the 'dvr' block are saved into the config).
    Returns {"key", "name", "created", "refreshed", "guide_lineup", "guide_started", "url", "xmltv_url"}.
    Raises DvrError.
    """
    cfg = store.get()
    opts = clean_opts(cfg, lineup_id, opts)
    dvr_url(cfg)
    base_url(cfg)

    # Save the guide choice first so the playlist the DVR is about to read matches it.
    wanted_guide = GUIDE_TO_LINEUP[opts["guide"]]
    if _find_lineup(cfg, lineup_id).get("guide") != wanted_guide:
        def set_guide(c):
            _find_lineup(c, lineup_id)["guide"] = wanted_guide
        store.update(set_guide)
        cfg = store.get()

    key = source_key(lineup_id)
    existed = status(cfg, lineup_id)["in_dvr"]
    payload = source_payload(cfg, lineup_id, opts)
    _call(cfg, "PUT", "/providers/m3u/sources/%s" % key, payload)
    log.info("%s Channels DVR source %s for lineup %s", "updated" if existed else "added", key, lineup_id)
    refresh_source(cfg, lineup_id)
    dvr_lineup = ""
    try:
        dvr_lineup = download_guide(cfg, lineup_id)
    except DvrError as exc:
        log.warning("guide download for %s did not start: %s", key, exc.message)
    if not dvr_lineup:
        log.info("Channels DVR has no guide lineup for %s yet; it will fetch the guide on its own", key)

    def save(c):
        lineup = _find_lineup(c, lineup_id)
        previous = lineup.get("dvr") or {}
        lineup["dvr"] = {"key": key, "name": opts["name"], "limit": opts["limit"], "numbering": opts["numbering"],
                         "start_number": opts["start_number"], "logos": opts["logos"],
                         "added_at": previous.get("added_at") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    store.update(save)
    return {"key": key, "name": opts["name"], "created": not existed, "refreshed": True,
            "guide_lineup": dvr_lineup, "guide_started": bool(dvr_lineup),
            "url": payload["url"], "xmltv_url": payload["xmltv_url"]}


# ---------------------------------------------------------------- automatic sync

def _snapshot(cfg, lineup_id, m3u_func):
    """What the DVR cares about for one lineup: playlist text, member channels and guide fields."""
    lineup = _find_lineup(cfg, lineup_id)
    by_id = {c.get("id"): c for c in cfg.get("channels") or []}
    # Only channels that are in the playlist: adding a disabled channel (auto-add) changes nothing for the DVR.
    members = [cid for cid in lineup.get("channels") or [] if (by_id.get(cid) or {}).get("enabled", True)]
    guide_fields = []
    for cid in members:
        g = (by_id.get(cid) or {}).get("guide") or {}
        guide_fields.append((cid, g.get("mode"), g.get("station_id"), g.get("xmltv_id"), g.get("xmltv_provider")))
    try:
        text = m3u_func(cfg, lineup_id)
    except Exception as exc:  # noqa: BLE001 - a broken playlist must not stop the syncer
        text = "error: %s" % exc
    return {"m3u": text, "channels": set(members), "guide": lineup.get("guide"), "guide_fields": guide_fields,
            "base_url": (cfg.get("server") or {}).get("base_url")}


def _default_m3u(cfg, lineup_id):
    from app.guide import output
    return output.m3u(cfg, lineup_id)


def _default_xmltv_refresh(cfg):
    from app.guide import xmltv
    return xmltv.refresh(cfg)


class Syncer:
    """Keeps Channels DVR sources in step with coax. Register with store.listeners.append(syncer.on_change)."""

    def __init__(self, store, debounce=3.0, m3u_func=None, xmltv_refresh=None, start=True):
        self.store = store
        self.debounce = debounce
        self.m3u_func = m3u_func or _default_m3u
        self.xmltv_refresh = xmltv_refresh or _default_xmltv_refresh
        self.lock = threading.RLock()        # one DVR conversation at a time (sync or add)
        self._state_lock = threading.Lock()
        self._wake = threading.Event()
        self._due = None
        self._stop = False
        self.baseline = {}                   # lineup id -> snapshot last synced
        self.results = {}                    # lineup id -> last sync result for the UI
        try:
            self._mark_all(store.get())
        except Exception:  # noqa: BLE001
            log.exception("could not read the config for the Channels DVR syncer")
        self.thread = None
        if start:
            self.thread = threading.Thread(target=self._run, name="dvr-sync", daemon=True)
            self.thread.start()

    def register(self):
        self.store.listeners.append(self.on_change)
        return self

    # -- change hook
    def on_change(self, old_cfg, new_cfg):
        """Config listener: (re)start the debounce timer."""
        with self._state_lock:
            self._due = time.monotonic() + self.debounce
        self._wake.set()

    def stop(self):
        self._stop = True
        self._wake.set()

    def _run(self):
        while not self._stop:
            self._wake.wait()
            self._wake.clear()
            # Wait until no change arrived for `debounce` seconds.
            while not self._stop:
                with self._state_lock:
                    due = self._due
                wait = (due or 0) - time.monotonic()
                if wait <= 0:
                    break
                self._wake.wait(wait)
                self._wake.clear()
            if self._stop:
                return
            with self._state_lock:
                self._due = None
            self.sync_all()

    # -- syncing
    def _mark_all(self, cfg):
        for lineup in cfg.get("lineups") or []:
            if lineup.get("dvr"):
                self.baseline[lineup["id"]] = _snapshot(cfg, lineup["id"], self.m3u_func)

    def mark_synced(self, lineup_id, cfg=None):
        cfg = cfg or self.store.get()
        with self.lock:
            self.baseline[lineup_id] = _snapshot(cfg, lineup_id, self.m3u_func)

    def sync_all(self):
        """Compare every DVR lineup with its last synced state and push what changed. Never raises."""
        try:
            cfg = self.store.get()
            lineups = [l["id"] for l in cfg.get("lineups") or [] if l.get("dvr")]
        except Exception:  # noqa: BLE001
            log.exception("Channels DVR sync could not read the config")
            return
        for lineup_id in list(self.baseline):
            if lineup_id not in lineups:
                self.baseline.pop(lineup_id, None)
        for lineup_id in lineups:
            self._sync(cfg, lineup_id, force=False)

    def sync_now(self, lineup_id):
        """Refresh the DVR source and download its guide right away. Returns the result dict."""
        cfg = self.store.get()
        _find_lineup(cfg, lineup_id)
        return self._sync(cfg, lineup_id, force=True)

    def _sync(self, cfg, lineup_id, force):
        with self.lock:
            result = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "ok": True, "error": "",
                      "refreshed": False, "guide_started": False, "source_updated": False}
            try:
                lineup = _find_lineup(cfg, lineup_id)
                new = _snapshot(cfg, lineup_id, self.m3u_func)
                old = self.baseline.get(lineup_id)
                if old is None and not force:
                    # First sight of this lineup (for example added in another way): just remember it.
                    self.baseline[lineup_id] = new
                    return None
                old = old or {}
                settings_changed = any(old.get(k) != new[k] for k in ("guide", "base_url"))
                playlist_changed = force or old.get("m3u") != new["m3u"]
                guide_changed = (force or settings_changed or bool(new["channels"] - old.get("channels", set()))
                                 or old.get("guide_fields") != new["guide_fields"])
                if not (settings_changed or playlist_changed or guide_changed):
                    return None
                if settings_changed and lineup.get("dvr"):
                    # Guide mode or URLs changed: rewrite the source with the stored options.
                    stored = dict(lineup["dvr"], guide=LINEUP_TO_GUIDE.get(lineup.get("guide"), "gracenote_xmltv"))
                    opts = clean_opts(cfg, lineup_id, stored)
                    _call(cfg, "PUT", "/providers/m3u/sources/%s" % source_key(lineup_id),
                          source_payload(cfg, lineup_id, opts))
                    result["source_updated"] = True
                refresh_source(cfg, lineup_id)
                result["refreshed"] = True
                log.info("Channels DVR source %s reloaded", source_key(lineup_id))
                if guide_changed:
                    if lineup.get("guide") != "gracenote":
                        # Rebuild the lineup's XMLTV file so the DVR downloads the new channels' guide.
                        try:
                            self.xmltv_refresh(cfg)
                        except Exception as exc:  # noqa: BLE001
                            log.warning("XMLTV refresh before the DVR guide download failed: %s", exc)
                    result["guide_lineup"] = download_guide(cfg, lineup_id)
                    result["guide_started"] = bool(result["guide_lineup"])
                    if result["guide_started"]:
                        log.info("Channels DVR guide download started for %s", source_key(lineup_id))
                self.baseline[lineup_id] = new
            except DvrError as exc:
                result.update(ok=False, error=exc.message)
                log.warning("Channels DVR sync for lineup %s failed: %s", lineup_id, exc.message)
            except Exception as exc:  # noqa: BLE001 - the syncer must never raise
                result.update(ok=False, error=str(exc))
                log.exception("Channels DVR sync for lineup %s failed", lineup_id)
            self.results[lineup_id] = result
            return copy.deepcopy(result)

    def add(self, lineup_id, opts):
        """add_or_update under the sync lock, then remember the synced state so the hook does not repeat it."""
        with self.lock:
            result = add_or_update(self.store, lineup_id, opts)
            self.mark_synced(lineup_id)
            self.results[lineup_id] = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "ok": True,
                                       "error": "", "refreshed": True, "guide_started": result["guide_started"],
                                       "source_updated": True}
            return result

    def last_result(self, lineup_id):
        return copy.deepcopy(self.results.get(lineup_id))
