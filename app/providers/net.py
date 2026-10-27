"""Shared HTTP helpers for provider downloads: retries, backoff, user agent, redaction."""

import logging
import re
import time

import requests

log = logging.getLogger("coax.providers")

# Many IPTV panels reject the default python-requests agent, so a player-like
# agent is used unless the provider sets its own.
DEFAULT_USER_AGENT = "VLC/3.0.21 LibVLC/3.0.21"

# (connect, read) timeouts in seconds.
TIMEOUT = (10, 60)
RETRIES = 3
BACKOFF_SECONDS = 2.0

# Status codes worth retrying: timeouts, rate limits, server errors.
RETRY_STATUSES = {408, 425, 429, 500, 502, 503, 504, 509, 520, 521, 522, 523, 524}

# Overridable by tests so retries do not actually wait.
sleep = time.sleep


class ProviderError(Exception):
    """A provider could not be reached or returned something unusable."""


def user_agent(provider):
    return (provider.get("user_agent") or "").strip() or DEFAULT_USER_AGENT


def secrets_of(provider):
    """Every password configured on the provider, for redaction."""
    return [line.get("password") or "" for line in provider.get("lines") or []]


_QUERY_PASSWORD = re.compile(r"(password=)[^&\s'\"]*", re.IGNORECASE)


def redact(text, secrets=()):
    """Remove passwords from text that may contain URLs (error messages, logs)."""
    text = _QUERY_PASSWORD.sub(r"\1********", str(text))
    for secret in secrets:
        # Very short secrets would mangle unrelated text; the query rule covers most cases.
        if secret and len(secret) >= 4:
            text = text.replace(secret, "********")
    return text


def get(url, provider, params=None, stream=False, label="request"):
    """GET with retries and exponential backoff. Returns a successful Response.

    Raises ProviderError with a redacted, human readable message on final failure.
    """
    secrets = secrets_of(provider)
    headers = {"User-Agent": user_agent(provider)}
    last_error = "unknown error"
    for attempt in range(1, RETRIES + 1):
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=TIMEOUT, stream=stream)
        except requests.RequestException as exc:
            last_error = "%s: %s" % (type(exc).__name__, exc)
        else:
            if resp.status_code < 400:
                return resp
            resp.close()
            last_error = "HTTP %d" % resp.status_code
            if resp.status_code in (401, 403):
                raise ProviderError("%s rejected (HTTP %d): check the username, password and user agent"
                                    % (label, resp.status_code))
            if resp.status_code not in RETRY_STATUSES:
                raise ProviderError("%s failed: HTTP %d" % (label, resp.status_code))
        if attempt < RETRIES:
            delay = BACKOFF_SECONDS * (2 ** (attempt - 1))
            log.warning("%s failed (attempt %d/%d): %s; retrying in %.0fs",
                        label, attempt, RETRIES, redact(last_error, secrets), delay)
            sleep(delay)
    raise ProviderError("%s failed after %d attempts: %s" % (label, RETRIES, redact(last_error, secrets)))
