"""Time zone for coax (backup schedule, log timestamps).

Order: the timezone saved in Settings, else the container's TZ variable, else UTC.
The web UI fills Settings from the browser's zone the first time it opens, if nothing is set.
"""

import logging
import os
import time

log = logging.getLogger("coax.tz")
_ENV_TZ = os.environ.get("TZ", "")


def valid(name):
    if not name:
        return False
    if name in ("UTC", "Etc/UTC"):
        return True
    return os.path.isfile(os.path.join("/usr/share/zoneinfo", name)) and ".." not in name


def effective(cfg):
    saved = (cfg.get("timezone") or "").strip()
    if valid(saved):
        return saved, "settings"
    if valid(_ENV_TZ):
        return _ENV_TZ, "environment"
    return "UTC", "default"


def apply(cfg):
    name, source = effective(cfg)
    if os.environ.get("TZ") != name:
        os.environ["TZ"] = name
        if hasattr(time, "tzset"):
            time.tzset()
        log.info("time zone: %s (from %s)", name, source)
    return name, source


def on_change(old, new):
    if (old or {}).get("timezone") != (new or {}).get("timezone"):
        apply(new)
