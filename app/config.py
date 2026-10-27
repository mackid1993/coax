"""Load, validate, migrate and save /data/config.json.

All state lives in one JSON file. Writes are atomic (temp file, fsync, rename), the file is kept at mode 0600
because it holds provider passwords, and the previous version is kept as config.json.bak.

The ConfigStore hands out the current config dict through get(). That dict is never mutated in place: every
change goes through update(), which works on a deep copy, validates it, saves it and then swaps it in. Readers
(the hub, the engine helpers, the guide) can therefore hold on to a snapshot without locking.
"""

import copy
import json
import logging
import os
import re
import tempfile
import threading
import uuid as uuidlib

log = logging.getLogger("coax.config")

CURRENT_VERSION = 1
MASK = "********"
DEFAULT_PATH = "/data/config.json"
# Directory holding config.json and cache/. main.py sets it from the config path; other modules read it.
DATA_DIR = "/data"

TRANSCODE_VALUES = ("off", "1080p60", "1080p30", "720p60", "720p30", "1080p50", "1080p25", "720p50", "720p25")
PROVIDER_TYPES = ("xtream", "m3u")
STREAM_FORMATS = ("hls", "ts")
GUIDE_MODES = ("gracenote", "xmltv", "both", "none")
LINEUP_GUIDES = ("gracenote", "xmltv", "both", "per-channel")
LINEUP_SORTS = ("manual", "provider", "alpha")  # channel order in the lineup's M3U
ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class ConfigError(ValueError):
    """A validation problem with a message meant for the user."""


def default_config():
    return {
        "version": CURRENT_VERSION,
        "server": {"port": 7780, "base_url": "", "new_client_behind_seconds": 30, "channel_shutdown_delay": 10},
        "channels_dvr": {"url": ""},
        "transcode": "off",
        "providers": [],
        "channels": [],
        "lineups": [{"id": "main", "name": "Main", "guide": "per-channel", "channels": []}],
        "xmltv_sources": [],
        "gracenote": {"regions": []},
        "timezone": "",
        "backups": {"enabled": True, "time": "03:30", "keep": 14},
        "next_channel_id": 1,
    }


def default_provider():
    return {
        "id": "", "name": "", "type": "xtream", "server": "", "m3u_url": "", "xmltv_url": "",
        "user_agent": "", "format": "hls", "refresh_hours": 24, "lines": [], "auto_add": False, "groups": [],
        "max_streams": 0,
    }


def default_line():
    return {"id": "", "name": "", "server": "", "username": "", "password": "", "max_streams": 1, "search": "",
            "replace": ""}


def default_xmltv_source():
    return {"id": "", "name": "", "url": "", "refresh_hours": 24}


def default_channel():
    return {
        "id": 0, "uuid": "", "number": "", "name": "", "group": "", "logo": "", "enabled": True, "streams": [],
        "guide": {"mode": "none", "station_id": "", "xmltv_id": "", "xmltv_provider": "", "reviewed": False},
    }


def _fill(target, defaults):
    """Add missing keys from defaults (recursing into nested dicts). Returns target."""
    for key, value in defaults.items():
        if key not in target or target[key] is None:
            target[key] = copy.deepcopy(value)
        elif isinstance(value, dict) and isinstance(target[key], dict):
            _fill(target[key], value)
    return target


def fill_defaults(cfg):
    """Fill every missing key in place, including inside providers, lines and channels."""
    _fill(cfg, default_config())
    for provider in cfg.get("providers") or []:
        if isinstance(provider, dict):
            _fill(provider, default_provider())
            for line in provider.get("lines") or []:
                if isinstance(line, dict):
                    _fill(line, default_line())
    for channel in cfg.get("channels") or []:
        if isinstance(channel, dict):
            _fill(channel, default_channel())
            if not channel.get("uuid"):
                channel["uuid"] = str(uuidlib.uuid4())
    for lineup in cfg.get("lineups") or []:
        if isinstance(lineup, dict):
            lineup.setdefault("name", lineup.get("id", ""))
            lineup.setdefault("channels", [])
            if not lineup.get("guide"):
                lineup["guide"] = "per-channel"
            if lineup.get("sort") not in LINEUP_SORTS:
                lineup["sort"] = "manual"
            lineup.setdefault("start_number", None)
    for ch in cfg.get("channels") or []:
        g = ch.get("guide") if isinstance(ch, dict) else None
        if isinstance(g, dict) and g.get("mode") in (None, "", "none"):
            if g.get("station_id") or g.get("xmltv_id"):
                g["mode"] = "both" if g.get("station_id") and g.get("xmltv_id") else (
                    "gracenote" if g.get("station_id") else "xmltv")
    if not isinstance(cfg.get("timezone"), str):
        cfg["timezone"] = ""
    if not isinstance(cfg.get("gracenote"), dict):
        cfg["gracenote"] = {}
    cfg["gracenote"].setdefault("regions", [])
    if not isinstance(cfg.get("backups"), dict):
        cfg["backups"] = {}
    for key, value in (("enabled", True), ("time", "03:30"), ("keep", 14)):
        cfg["backups"].setdefault(key, value)
    for source in cfg.get("xmltv_sources") or []:
        if isinstance(source, dict):
            _fill(source, default_xmltv_source())
    # next_channel_id must always stay ahead of every id ever handed out
    ids = [c.get("id") for c in cfg.get("channels") or [] if isinstance(c, dict) and isinstance(c.get("id"), int)]
    if not isinstance(cfg.get("next_channel_id"), int) or cfg["next_channel_id"] <= max(ids, default=0):
        cfg["next_channel_id"] = max(ids, default=0) + 1
    return cfg


def migrate(cfg):
    """Bring an older config up to CURRENT_VERSION. Unknown newer versions are loaded as they are."""
    version = cfg.get("version")
    if not isinstance(version, int) or version < 1:
        # Pre-release files had no version key; the shape is the same as version 1.
        cfg["version"] = 1
        version = 1
    if version > CURRENT_VERSION:
        log.warning("config.json is version %s, newer than this build (%s); loading it as is",
                    version, CURRENT_VERSION)
    return cfg


def _require(cond, message):
    if not cond:
        raise ConfigError(message)


def _int_in(value, low, high, what):
    _require(isinstance(value, int) and not isinstance(value, bool) and low <= value <= high,
             "%s must be a whole number from %s to %s" % (what, low, high))


def _num_in(value, low, high, what):
    _require(isinstance(value, (int, float)) and not isinstance(value, bool) and low <= value <= high,
             "%s must be a number from %s to %s" % (what, low, high))


def _str(value, what):
    _require(isinstance(value, str), "%s must be text" % what)


def _url(value, what, required=False):
    _str(value, what)
    if required:
        _require(value.strip() != "", "%s is required" % what)
    if value.strip():
        _require(re.match(r"^https?://[^\s/]+", value.strip()) is not None,
                 "%s must start with http:// or https://" % what)


def validate(cfg):
    """Raise ConfigError with a readable message when cfg is not usable."""
    _require(isinstance(cfg, dict), "config must be a JSON object")
    server = cfg.get("server")
    _require(isinstance(server, dict), "server must be an object")
    _int_in(server.get("port"), 1, 65535, "server.port")
    _url(server.get("base_url", ""), "server.base_url")
    _num_in(server.get("new_client_behind_seconds"), 0, 600, "server.new_client_behind_seconds")
    _num_in(server.get("channel_shutdown_delay"), 0, 3600, "server.channel_shutdown_delay")
    _require(isinstance(cfg.get("channels_dvr"), dict), "channels_dvr must be an object")
    _url(cfg["channels_dvr"].get("url", ""), "channels_dvr.url")
    _require(cfg.get("transcode") in TRANSCODE_VALUES,
             "transcode must be one of: %s" % ", ".join(TRANSCODE_VALUES))

    providers = cfg.get("providers")
    _require(isinstance(providers, list), "providers must be a list")
    provider_ids = set()
    for provider in providers:
        _require(isinstance(provider, dict), "each provider must be an object")
        pid = provider.get("id")
        _require(isinstance(pid, str) and ID_PATTERN.match(pid), "provider id %r is not valid" % (pid,))
        _require(pid not in provider_ids, "provider id %s is used twice" % pid)
        provider_ids.add(pid)
        label = "provider %s" % (provider.get("name") or pid)
        _str(provider.get("name"), label + ": name")
        _require(provider["name"].strip() != "", label + ": name is required")
        _require(provider.get("type") in PROVIDER_TYPES, label + ": type must be xtream or m3u")
        _require(provider.get("format") in STREAM_FORMATS, label + ": format must be hls or ts")
        if provider["type"] == "xtream":
            _url(provider.get("server", ""), label + ": server", required=True)
        else:
            _url(provider.get("m3u_url", ""), label + ": M3U URL", required=True)
        _url(provider.get("xmltv_url", ""), label + ": XMLTV URL")
        _str(provider.get("user_agent"), label + ": user agent")
        _int_in(provider.get("max_streams", 0), 0, 1000, label + ": max streams")
        _num_in(provider.get("refresh_hours"), 0, 24 * 30, label + ": refresh hours (0 = manual only)")
        _require(isinstance(provider.get("auto_add"), bool), label + ": auto_add must be true or false")
        groups = provider.get("groups")
        _require(isinstance(groups, list) and all(isinstance(g, str) for g in groups),
                 label + ": groups must be a list of names")
        lines = provider.get("lines")
        _require(isinstance(lines, list) and lines, label + ": at least one line is required")
        line_ids = set()
        for line in lines:
            _require(isinstance(line, dict), label + ": each line must be an object")
            lid = line.get("id")
            _require(isinstance(lid, str) and ID_PATTERN.match(lid), label + ": line id %r is not valid" % (lid,))
            _require(lid not in line_ids, label + ": line id %s is used twice" % lid)
            line_ids.add(lid)
            for key in ("name", "server", "username", "password", "search", "replace"):
                _str(line.get(key), "%s line %s: %s" % (label, lid, key))
            _int_in(line.get("max_streams"), 0, 1000, "%s line %s: max streams" % (label, lid))
            if provider["type"] == "xtream":
                _require(line["username"].strip() != "", "%s line %s: username is required" % (label, lid))
                _require(line["password"] != "", "%s line %s: password is required" % (label, lid))
            if line["search"]:
                try:
                    re.compile(line["search"])
                except re.error as exc:
                    raise ConfigError("%s line %s: search is not a valid regular expression (%s)"
                                      % (label, lid, exc))

    sources = cfg.get("xmltv_sources")
    _require(isinstance(sources, list), "xmltv_sources must be a list")
    source_ids = set()
    for source in sources:
        _require(isinstance(source, dict), "each XMLTV source must be an object")
        sid = source.get("id")
        _require(isinstance(sid, str) and ID_PATTERN.match(sid), "XMLTV source id %r is not valid" % (sid,))
        _require(sid not in source_ids and sid not in provider_ids,
                 "XMLTV source id %s is already used by another source or provider" % sid)
        source_ids.add(sid)
        label = "XMLTV source %s" % (source.get("name") or sid)
        _str(source.get("name"), label + ": name")
        _require(source["name"].strip() != "", label + ": name is required")
        _url(source.get("url", ""), label + ": URL", required=True)
        _num_in(source.get("refresh_hours"), 1, 24 * 30, label + ": refresh hours")
    guide_sources = provider_ids | source_ids

    channels = cfg.get("channels")
    _require(isinstance(channels, list), "channels must be a list")
    channel_ids, uuids = set(), set()
    for channel in channels:
        _require(isinstance(channel, dict), "each channel must be an object")
        cid = channel.get("id")
        _int_in(cid, 1, 10 ** 9, "channel id")
        _require(cid not in channel_ids, "channel id %s is used twice" % cid)
        channel_ids.add(cid)
        label = "channel %s" % (channel.get("name") or cid)
        _require(isinstance(channel.get("uuid"), str) and channel["uuid"], label + ": uuid is missing")
        _require(channel["uuid"] not in uuids, label + ": uuid is used twice")
        uuids.add(channel["uuid"])
        for key in ("number", "name", "group", "logo"):
            _str(channel.get(key), "%s: %s" % (label, key))
        _require(channel["name"].strip() != "", "channel %s: name is required" % cid)
        if channel["number"].strip():
            _require(re.match(r"^\d+(\.\d+)?$", channel["number"].strip()) is not None,
                     label + ": number must look like 101 or 101.1")
        _require(isinstance(channel.get("enabled"), bool), label + ": enabled must be true or false")
        streams = channel.get("streams")
        _require(isinstance(streams, list), label + ": streams must be a list")
        for ref in streams:
            _require(isinstance(ref, dict) and isinstance(ref.get("provider"), str)
                     and isinstance(ref.get("stream_id"), str) and ref["stream_id"],
                     label + ": each stream needs provider and stream_id")
            _require(ref["provider"] in provider_ids,
                     label + ": stream refers to unknown provider %s" % ref["provider"])
        guide = channel.get("guide")
        _require(isinstance(guide, dict), label + ": guide must be an object")
        _require(guide.get("mode") in GUIDE_MODES, label + ": guide mode must be one of " + ", ".join(GUIDE_MODES))
        for key in ("station_id", "xmltv_id", "xmltv_provider"):
            _str(guide.get(key), "%s: guide.%s" % (label, key))
        if guide["xmltv_provider"]:
            _require(guide["xmltv_provider"] in guide_sources,
                     label + ": guide refers to unknown provider or XMLTV source %s" % guide["xmltv_provider"])
    for i, region in enumerate((cfg.get("gracenote") or {}).get("regions") or []):
        _require(isinstance(region, dict) and re.match(r"^[A-Za-z]{3}$", str(region.get("country", "")))
                 and str(region.get("postal", "")).strip(),
                 "Gracenote region %d needs a 3-letter country code and a postal code" % (i + 1))
    backups = cfg.get("backups") or {}
    _require(re.match(r"^([01]\d|2[0-3]):[0-5]\d$", str(backups.get("time", ""))), "backup time must look like 03:30")
    _int_in(backups.get("keep"), 1, 365, "backups to keep")
    _int_in(cfg.get("next_channel_id"), 1, 10 ** 9 + 1, "next_channel_id")
    _require(cfg["next_channel_id"] > max(channel_ids, default=0), "next_channel_id must be above every channel id")

    lineups = cfg.get("lineups")
    _require(isinstance(lineups, list), "lineups must be a list")
    lineup_ids = set()
    for lineup in lineups:
        _require(isinstance(lineup, dict), "each lineup must be an object")
        lid = lineup.get("id")
        _require(isinstance(lid, str) and ID_PATTERN.match(lid),
                 "lineup id %r must use letters, digits, - or _" % (lid,))
        _require(lid not in lineup_ids, "lineup id %s is used twice" % lid)
        lineup_ids.add(lid)
        _str(lineup.get("name"), "lineup %s: name" % lid)
        sn = lineup.get("start_number")
        _require(sn in (None, "") or (isinstance(sn, int) and not isinstance(sn, bool) and 0 <= sn <= 10 ** 7),
                 "lineup %s: start number must be a whole number" % lid)
        _require(lineup.get("guide") in LINEUP_GUIDES,
                 "lineup %s: guide must be one of %s" % (lid, ", ".join(LINEUP_GUIDES)))
        members = lineup.get("channels")
        _require(isinstance(members, list), "lineup %s: channels must be a list" % lid)
        for cid in members:
            _require(cid in channel_ids, "lineup %s: unknown channel %s" % (lid, cid))
        _require(len(set(members)) == len(members), "lineup %s: a channel is listed twice" % lid)
        # Optional Channels DVR source settings written by app.channels_dvr (kept as is).
        _require(isinstance(lineup.get("dvr", {}), dict), "lineup %s: dvr must be an object" % lid)
    return cfg


# ---------------------------------------------------------------- password masking

def _line_index(cfg):
    index = {}
    for provider in cfg.get("providers") or []:
        for line in provider.get("lines") or []:
            index[(provider.get("id"), line.get("id"))] = line
    return index


def mask(cfg):
    """Deep copy with every line password replaced by MASK (empty passwords stay empty)."""
    out = copy.deepcopy(cfg)
    for provider in out.get("providers") or []:
        for line in provider.get("lines") or []:
            if line.get("password"):
                line["password"] = MASK
    return out


def mask_provider(provider):
    return mask({"providers": [provider]})["providers"][0]


def unmask(new_cfg, old_cfg):
    """Replace MASK passwords in new_cfg with the stored value for the same provider and line (in place)."""
    old = _line_index(old_cfg)
    for provider in new_cfg.get("providers") or []:
        for line in provider.get("lines") or []:
            if line.get("password") == MASK:
                previous = old.get((provider.get("id"), line.get("id")))
                if previous is None:
                    raise ConfigError("line %s of provider %s has a masked password but no stored password; "
                                      "enter the password again" % (line.get("id"), provider.get("id")))
                line["password"] = previous.get("password", "")
    return new_cfg


# ---------------------------------------------------------------- file I/O

def _fsync_dir(path):
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write(path, data, mode=0o600):
    """Write bytes to path atomically: temp file in the same directory, fsync, chmod, rename."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _fsync_dir(directory)


class ConfigStore:
    """Thread-safe owner of the config file."""

    def __init__(self, path=DEFAULT_PATH):
        self.path = path
        self.data_dir = os.path.dirname(os.path.abspath(path))
        self._lock = threading.RLock()
        self._cfg = None
        # Change hooks: each is called as listener(old_cfg, new_cfg) after update() or replace() swaps in a config.
        self.listeners = []

    # -- reading
    def load(self):
        """Read the file (or create defaults), migrate, fill defaults and validate. Returns the config."""
        with self._lock:
            if os.path.exists(self.path):
                with open(self.path, "rb") as handle:
                    raw = handle.read()
                try:
                    cfg = json.loads(raw.decode("utf-8-sig"))
                except ValueError as exc:
                    try:
                        with open(self.path + ".bak", "rb") as handle:
                            cfg = json.loads(handle.read().decode("utf-8-sig"))
                        log.error("%s is not valid JSON (%s); using %s.bak", self.path, exc, self.path)
                        atomic_write(self.path, json.dumps(cfg, indent=2).encode("utf-8"))
                    except (OSError, ValueError):
                        raise ConfigError("%s is not valid JSON: %s" % (self.path, exc))
                created = False
            else:
                cfg = default_config()
                created = True
            original = copy.deepcopy(cfg)
            cfg = fill_defaults(migrate(cfg))
            validate(cfg)
            self._cfg = cfg
            if created or cfg != original:
                self._write(cfg)
                log.info("%s %s", "created" if created else "updated defaults in", self.path)
            else:
                self._ensure_mode()
            return cfg

    def get(self):
        """Current config. Treat it as read-only; it is replaced, never mutated."""
        with self._lock:
            if self._cfg is None:
                return self.load()
            return self._cfg

    def snapshot(self):
        with self._lock:
            return copy.deepcopy(self.get())

    def masked(self):
        return mask(self.get())

    # -- writing
    def update(self, mutate):
        """Run mutate(cfg_copy) under the lock, then validate, save and swap in. Returns mutate's result."""
        with self._lock:
            cfg = copy.deepcopy(self.get())
            result = mutate(cfg)
            cfg = fill_defaults(cfg)
            validate(cfg)
            self._write(cfg)
            old, self._cfg = self._cfg, cfg
        self._notify(old, cfg)
        return result

    def replace(self, new_cfg):
        """Replace the whole config (passwords that are MASK keep the stored value)."""
        with self._lock:
            new_cfg = copy.deepcopy(new_cfg)
            unmask(new_cfg, self.get())
            new_cfg["version"] = CURRENT_VERSION
            new_cfg["next_channel_id"] = max(int(new_cfg.get("next_channel_id") or 0),
                                             self.get().get("next_channel_id", 1))
            new_cfg = fill_defaults(new_cfg)
            validate(new_cfg)
            self._write(new_cfg)
            old, self._cfg = self._cfg, new_cfg
        self._notify(old, new_cfg)
        return new_cfg

    def _notify(self, old_cfg, new_cfg):
        """Tell every listener about a new config. A failing listener is logged, never raised."""
        for listener in list(self.listeners):
            try:
                listener(old_cfg, new_cfg)
            except Exception:  # noqa: BLE001
                log.exception("config listener failed")

    def _ensure_mode(self):
        try:
            if os.stat(self.path).st_mode & 0o077:
                os.chmod(self.path, 0o600)
        except OSError:
            pass

    def _write(self, cfg):
        data = (json.dumps(cfg, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        if os.path.exists(self.path):
            # Keep one rolling backup of the previous good file.
            try:
                with open(self.path, "rb") as handle:
                    previous = handle.read()
                if previous != data:
                    atomic_write(self.path + ".bak", previous)
            except OSError as exc:
                log.warning("could not write %s.bak: %s", self.path, exc)
        atomic_write(self.path, data)


# ---------------------------------------------------------------- helpers used by the API

def new_channel(cfg, fields):
    """Create a channel dict with a fresh id and uuid, consuming next_channel_id (mutates cfg)."""
    channel = default_channel()
    for key in ("number", "name", "group", "logo", "enabled", "streams"):
        if key in fields:
            channel[key] = fields[key]
    if isinstance(fields.get("guide"), dict):
        channel["guide"].update(fields["guide"])
    channel["id"] = cfg["next_channel_id"]
    cfg["next_channel_id"] += 1
    channel["uuid"] = str(uuidlib.uuid4())
    return channel


def next_id(prefix, existing):
    """Next free id like p1, p2 (or l1, l2) not in existing."""
    numbers = [int(m.group(1)) for m in (re.match(r"^%s(\d+)$" % re.escape(prefix), e) for e in existing) if m]
    n = max(numbers, default=0) + 1
    while "%s%d" % (prefix, n) in existing:
        n += 1
    return "%s%d" % (prefix, n)
