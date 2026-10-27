"""Shared HTTP helpers for provider downloads: retries, backoff, user agent, redaction."""

import logging
import re
import time

import requests

log = logging.getLogger("coax.providers")

# Many IPTV panels reject the default python-requests agent, so a player-like
# agent is used unless the provider sets its own.
from app import USER_AGENT as DEFAULT_USER_AGENT

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
    return DEFAULT_USER_AGENT


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


def _remaining(deadline, label):
    """Seconds left before a monotonic deadline; raises ProviderError once it has passed."""
    if deadline is None:
        return None
    left = deadline - time.monotonic()
    if left <= 0:
        raise ProviderError("%s took too long (overall deadline exceeded)" % label)
    return left


def _timeout(deadline, label):
    left = _remaining(deadline, label)
    if left is None:
        return TIMEOUT
    return (min(TIMEOUT[0], left), min(TIMEOUT[1], left))


def get(url, provider, params=None, stream=False, label="request", deadline=None):
    """GET with retries and exponential backoff. Returns a successful Response.

    deadline: optional time.monotonic() value; retries, backoff and timeouts never go past it.
    Raises ProviderError with a redacted, human readable message on final failure.
    """
    secrets = secrets_of(provider)
    headers = {"User-Agent": user_agent(provider)}
    last_error = "unknown error"
    for attempt in range(1, RETRIES + 1):
        timeout = _timeout(deadline, label)
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=timeout, stream=stream)
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
            left = _remaining(deadline, label) if deadline is not None else None
            if left is not None and delay >= left:
                break
            log.warning("%s failed (attempt %d/%d): %s; retrying in %.0fs",
                        label, attempt, RETRIES, redact(last_error, secrets), delay)
            sleep(delay)
    raise ProviderError("%s failed after %d attempts: %s" % (label, RETRIES, redact(last_error, secrets)))


def iter_body(resp, deadline, label, chunk_size=64 * 1024):
    """Yield decoded body chunks of a streamed Response, stopping at a wall-clock deadline.

    Uses read1 so a trickling server returns small chunks quickly instead of blocking until a
    full chunk arrives; the deadline is checked between chunks, so the overrun is bounded by
    one per-read timeout. Raises ProviderError when the deadline passes.
    """
    raw = getattr(resp, "raw", None)
    read1 = getattr(raw, "read1", None)
    if read1 is None:
        source = resp.iter_content(chunk_size=chunk_size)
    else:
        def source_gen():
            while True:
                chunk = read1(chunk_size, decode_content=True)
                if not chunk:
                    return
                yield chunk
        source = source_gen()
    for chunk in source:
        if deadline is not None and time.monotonic() > deadline:
            resp.close()
            raise ProviderError("%s took too long (overall deadline exceeded)" % label)
        yield chunk


def read_body(resp, deadline, label, limit=None):
    """Read a whole streamed body within the deadline (and an optional byte limit)."""
    parts = []
    size = 0
    for chunk in iter_body(resp, deadline, label):
        parts.append(chunk)
        size += len(chunk)
        if limit is not None and size > limit:
            raise ProviderError("%s response is larger than %d bytes" % (label, limit))
    return b"".join(parts)
