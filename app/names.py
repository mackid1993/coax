"""Clean provider prefixes off channel and group names. Always on; not a setting.

Same rules as the matchers in mackid1993/channels-dvr-iptv-tools:
  LONG_PREFIX      "US| ", "U.S| ", "NFL TEAMS| ", "TV| "  (up to 15 characters before a bar)
  PROVIDER_PREFIX  "[US] ", "US: " (colon only for two-letter country codes, so "MLB: Yankees" stays)
Only the prefix goes; the rest of the name, including tags like HD or RAW, is kept.
"""

import re

LONG_PREFIX = re.compile(r"^\s*[A-Za-z][A-Za-z0-9 .]{0,14}\s*[|\u2502\uFF5C]\s*")
PROVIDER_PREFIX = re.compile(r"^\s*(?:\[[A-Za-z]{2,4}\]|[A-Za-z]{2}\s*:)\s+")


def clean_name(name):
    """'US| COMEDY CENTRAL HD' -> 'COMEDY CENTRAL HD'. Never returns an empty name."""
    original = (name or "").strip()
    text = original
    for _ in range(2):  # 'US| NFL| ...' style double prefixes
        stripped = PROVIDER_PREFIX.sub("", LONG_PREFIX.sub("", text, count=1), count=1).strip()
        if not stripped or stripped == text:
            break
        text = stripped
    return text or original
