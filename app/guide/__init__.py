"""Guide data for coax: Gracenote station IDs, XMLTV refresh, and lineup output.

Modules:
  gracenote  Channels DVR station search and matching suggestions
  xmltv      download, filter and rewrite XMLTV sources into per-lineup files
  output     M3U text and XMLTV file path for a lineup
"""

import re

# Where guide caches live. Tests point this at a temporary directory.
CACHE_DIR = "/data/cache"

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def cache_dir():
    return CACHE_DIR


def check_id(value, what="id"):
    """Return `value` as a string if it is safe to use in a file name, else raise ValueError."""
    text = str(value if value is not None else "")
    if not _SAFE_ID.match(text):
        raise ValueError(f"invalid {what}: {text!r}")
    return text
