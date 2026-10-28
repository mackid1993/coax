"""coax: a small, frozen IPTV appliance."""

import os as _os


def _version():
    """The version stamped into this build by scripts/stamp-version.sh ("dev" when not stamped)."""
    try:
        with open(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "VERSION"), encoding="utf-8") as f:
            return f.read().strip() or "dev"
    except OSError:
        return "dev"


VERSION = _version()

# Every request to a provider (playlists, player_api, XMLTV, streams) uses this
# user agent. It is fixed on purpose: IPTV panels accept it and it never needs tuning.
USER_AGENT = "TiviMate/5.1.6 (Android 12)"
