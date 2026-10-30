"""XMLTV guide refresh.

refresh(config) downloads every provider's XMLTV source (gzip or plain), parses it
with a streaming iterparse that clears each element after use, and writes one file
per lineup, /data/cache/xmltv-<lineup_id>.xml, holding only the channels that
enabled coax channels with guide mode "xmltv" or "both" point at. Channel ids are
rewritten to coax-<channel id> in both <channel id> and <programme channel>. Channels with no guide of their own
whose stream is an event channel get airings read from the stream's name (app.guide.events).

It also caches every channel id and display name each provider offers, in
/data/cache/xmltv-ids-<provider_id>.json, for the UI picker.

Failures are contained: a provider that fails to download or parse keeps its old
id cache, and any lineup that needs that provider keeps its old guide file.
"""

import gzip
import json
import logging
import os
import shutil
import tempfile
import threading
import time
import xml.etree.ElementTree as ET

from app import guide

log = logging.getLogger("coax.guide.xmltv")

GZIP_MAGIC = b"\x1f\x8b"
DOWNLOAD_TIMEOUT = (15, 120)      # connect, read (seconds)
GUIDE_MODES = ("xmltv", "both")

_refresh_lock = threading.Lock()


# ----------------------------------------------------------------------
# Small seams that tests replace
# ----------------------------------------------------------------------

def provider_xmltv_url(provider):
    """The XMLTV URL for a provider, from app.providers when it is available."""
    try:
        from app import providers
        return providers.xmltv_url(provider) or ""
    except (ImportError, AttributeError):
        # Providers module not ready: fall back to an explicitly configured URL.
        return (provider.get("xmltv_url") or "").strip()


def download(url, dest_path, user_agent=None):
    """Stream `url` into `dest_path`. Raises on HTTP or network errors."""
    import requests   # lazy so the module imports without it in minimal test setups

    headers = {"User-Agent": user_agent or __import__("app").USER_AGENT}
    with requests.get(url, headers=headers, stream=True, timeout=DOWNLOAD_TIMEOUT) as response:
        response.raise_for_status()
        with open(dest_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=256 * 1024):
                if chunk:
                    f.write(chunk)


# ----------------------------------------------------------------------
# Paths and file helpers
# ----------------------------------------------------------------------

def guide_path(lineup_id):
    return os.path.join(guide.cache_dir(), f"xmltv-{guide.check_id(lineup_id, 'lineup id')}.xml")


def ids_path(provider_id):
    return os.path.join(guide.cache_dir(),
                        f"xmltv-ids-{guide.check_id(provider_id, 'provider id')}.json")


def _atomic_replace(tmp_path, final_path):
    with open(tmp_path, "rb+") as f:
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, final_path)


def _write_json_atomic(path, data):
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        _atomic_replace(tmp, path)
    except BaseException:
        _unlink(tmp)
        raise


def _unlink(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def load_ids(provider_id):
    """Cached picker list for a provider, or None if it was never fetched."""
    try:
        with open(ids_path(provider_id), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def open_source(path):
    """Open a downloaded XMLTV file as a binary stream, gunzipping by magic bytes."""
    with open(path, "rb") as f:
        magic = f.read(2)
    if magic == GZIP_MAGIC:
        return gzip.open(path, "rb")
    return open(path, "rb")


# ----------------------------------------------------------------------
# Planning: which provider ids feed which coax channels and lineups
# ----------------------------------------------------------------------

def _channel_provider(channel):
    """Provider whose XMLTV a channel uses: guide.xmltv_provider, else its first stream's."""
    g = channel.get("guide") or {}
    pid = (g.get("xmltv_provider") or "").strip()
    if not pid:
        streams = channel.get("streams") or []
        pid = (streams[0].get("provider") or "") if streams else ""
    return pid


def build_plan(config):
    """Work out what each provider must supply.

    Returns (needs, lineup_providers):
      needs: {provider_id: {xmltv_id: [coax channel id, ...]}}
      lineup_providers: {lineup_id: set of provider ids its guide depends on}
    """
    channels = {}
    for ch in config.get("channels") or []:
        try:
            channels[int(ch.get("id"))] = ch
        except (TypeError, ValueError):
            continue
    needs = {}
    lineup_providers = {}
    for lineup in config.get("lineups") or []:
        deps = lineup_providers.setdefault(str(lineup.get("id")), set())
        # An xmltv or both lineup uses every channel's XMLTV id; otherwise the channel's own mode decides.
        lineup_wants_xmltv = lineup.get("guide") in GUIDE_MODES
        for cid in lineup.get("channels") or []:
            try:
                ch = channels.get(int(cid))
            except (TypeError, ValueError):
                continue
            if not ch or not ch.get("enabled", True):
                continue
            g = ch.get("guide") or {}
            xmltv_id = (g.get("xmltv_id") or "").strip()
            if not (lineup_wants_xmltv or g.get("mode") in GUIDE_MODES) or not xmltv_id:
                continue
            pid = _channel_provider(ch)
            if not pid:
                continue
            deps.add(pid)
            coax_ids = needs.setdefault(pid, {}).setdefault(xmltv_id, [])
            if int(ch["id"]) not in coax_ids:
                coax_ids.append(int(ch["id"]))
    return needs, lineup_providers


def _lineup_channel_ids(config):
    """{lineup_id: set of coax channel ids in it}."""
    return {str(lu.get("id")): {int(c) for c in lu.get("channels") or [] if str(c).isdigit()}
            for lu in config.get("lineups") or []}


# ----------------------------------------------------------------------
# Streaming parse
# ----------------------------------------------------------------------

class _LineupWriter:
    """Collects <channel> and <programme> fragments for one lineup in temp files."""

    def __init__(self, folder, lineup_id):
        self.lineup_id = lineup_id
        fd, self.channels_path = tempfile.mkstemp(dir=folder, prefix=".tmp-ch-", suffix=".xml")
        self.channels = os.fdopen(fd, "w", encoding="utf-8")
        fd, self.programmes_path = tempfile.mkstemp(dir=folder, prefix=".tmp-pr-", suffix=".xml")
        self.programmes = os.fdopen(fd, "w", encoding="utf-8")
        self.channel_count = 0
        self.programme_count = 0

    def finish(self, final_path):
        """Assemble the lineup file and move it into place atomically."""
        self.channels.close()
        self.programmes.close()
        folder = os.path.dirname(final_path)
        fd, tmp = tempfile.mkstemp(dir=folder, prefix=".tmp-xmltv-", suffix=".xml")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as out:
                out.write('<?xml version="1.0" encoding="UTF-8"?>\n')
                out.write('<tv generator-info-name="coax">\n')
                for part in (self.channels_path, self.programmes_path):
                    with open(part, encoding="utf-8") as f:
                        shutil.copyfileobj(f, out)
                out.write("</tv>\n")
            _atomic_replace(tmp, final_path)
        except BaseException:
            _unlink(tmp)
            raise
        finally:
            self.discard()

    def discard(self):
        for f in (self.channels, self.programmes):
            if not f.closed:
                f.close()
        _unlink(self.channels_path)
        _unlink(self.programmes_path)


def _serialize(elem, coax_id, attribute):
    """Copy of a top-level element with its channel reference renamed to coax-<id>."""
    elem.set(attribute, f"coax-{coax_id}")
    elem.tail = "\n"
    return "  " + ET.tostring(elem, encoding="unicode")


def parse_source(stream, wanted, route):
    """Stream one XMLTV document.

    wanted: {xmltv_id: [coax channel id, ...]}
    route:  {coax channel id: [_LineupWriter, ...]}
    Returns (available, found) where available is the picker list
    [{"id", "names", "icon"}] and found is the set of wanted xmltv ids seen.
    """
    available, seen_ids, found = [], {}, set()
    depth = 0
    root = None
    for event, elem in ET.iterparse(stream, events=("start", "end")):
        if event == "start":
            if root is None:
                root = elem
            depth += 1
            continue
        depth -= 1
        if depth != 1:
            continue   # only top-level <channel> and <programme> matter
        if elem.tag == "channel":
            xid = elem.get("id") or ""
            if xid:
                names = [(n.text or "").strip() for n in elem.findall("display-name")]
                names = [n for n in names if n]
                if xid not in seen_ids:
                    icon = elem.find("icon")
                    seen_ids[xid] = {"id": xid, "names": names,
                                     "icon": icon.get("src", "") if icon is not None else ""}
                    available.append(seen_ids[xid])
                else:
                    have = seen_ids[xid]["names"]
                    have.extend(n for n in names if n not in have)
            if xid in wanted:
                found.add(xid)
                for coax_id in wanted[xid]:
                    text = _serialize(elem, coax_id, "id")
                    for writer in route.get(coax_id, ()):
                        writer.channels.write(text)
                        writer.channel_count += 1
        elif elem.tag == "programme":
            xid = elem.get("channel") or ""
            if xid in wanted:
                for coax_id in wanted[xid]:
                    text = _serialize(elem, coax_id, "channel")
                    for writer in route.get(coax_id, ()):
                        writer.programmes.write(text)
                        writer.programme_count += 1
        # Free the element and drop it from the root so memory stays bounded.
        elem.clear()
        if root is not None:
            root.clear()
    if root is None or root.tag != "tv":
        raise ValueError("not an XMLTV document (root element is not <tv>)")
    return available, found


# ----------------------------------------------------------------------
# Refresh
# ----------------------------------------------------------------------

def refresh(config):
    """Refresh every lineup's XMLTV file and the per-provider picker caches.

    Never raises for provider problems. Returns a summary:
      {"providers": {pid: {"ok": bool, "error": str, "channels": n, "missing": [ids]}},
       "lineups": {lineup_id: {"ok": bool, "error": str, "channels": n, "programmes": n}},
       "finished_at": iso}
    """
    with _refresh_lock:
        return _refresh(config)


def _refresh(config):
    folder = guide.cache_dir()
    os.makedirs(folder, exist_ok=True)
    needs, lineup_providers = build_plan(config)
    lineup_members = _lineup_channel_ids(config)
    summary = {"providers": {}, "lineups": {}, "finished_at": ""}

    writers = {}
    for lineup_id in lineup_providers:
        try:
            guide.check_id(lineup_id, "lineup id")
        except ValueError as e:
            summary["lineups"][lineup_id] = {"ok": False, "error": str(e)}
            continue
        writers[lineup_id] = _LineupWriter(folder, lineup_id)

    failed = set()
    try:
        sources = list(config.get("providers") or [])
        for src in config.get("xmltv_sources") or []:
            # A standalone guide feed behaves like a provider that only has an XMLTV URL.
            sources.append({"id": src.get("id"), "name": src.get("name"), "type": "m3u",
                            "xmltv_url": src.get("url") or "", "lines": []})
        for provider in sources:
            pid = str(provider.get("id") or "")
            result = {"ok": False, "error": "", "channels": 0, "missing": []}
            summary["providers"][pid] = result
            try:
                guide.check_id(pid, "provider id")
                off = provider.get("enabled", True) is False
                url = "" if off else provider_xmltv_url(provider)
                if not url:
                    # Nothing to fetch (no URL, or the provider is switched off): its channels have no XMLTV guide,
                    # but the rest of the lineup still does.
                    why = "switched off" if off else "no XMLTV URL"
                    result.update(ok=True, error=why, missing=sorted(needs.get(pid, {})))
                    if result["missing"]:
                        log.warning("provider %s: %s; %d channels using it get no XMLTV guide",
                                    pid, why, len(result["missing"]))
                    continue
                wanted = needs.get(pid, {})
                route = {}
                for lineup_id, writer in writers.items():
                    for xid_coax in wanted.values():
                        for coax_id in xid_coax:
                            if coax_id in lineup_members.get(lineup_id, ()):
                                route.setdefault(coax_id, []).append(writer)
                available, found = _fetch_and_parse(url, provider, wanted, route, folder)
                _write_json_atomic(ids_path(pid), {
                    "provider": pid,
                    "fetched_at": _now(),
                    "channels": sorted(available, key=lambda c: c["id"].lower()),
                })
                result.update(ok=True, channels=len(available),
                              missing=sorted(set(wanted) - found))
                if result["missing"]:
                    log.warning("XMLTV for provider %s lacks %d channel ids: %s", pid,
                                len(result["missing"]), ", ".join(result["missing"][:10]))
            except Exception as e:   # one provider never stops the others
                log.warning("XMLTV refresh failed for provider %s: %s", pid, e)
                result["error"] = str(e)
                failed.add(pid)

        # A provider a lineup needs but that is not configured counts as failed too.
        configured = set(summary["providers"])
        for lineup_id, deps in lineup_providers.items():
            failed |= {pid for pid in deps if pid not in configured}

        for lineup_id, writer in writers.items():
            bad = sorted(lineup_providers[lineup_id] & failed)
            if bad:
                log.warning("XMLTV guide for lineup %s not rebuilt; kept the previous one (failed: %s)",
                            lineup_id, ", ".join(bad))
                writer.discard()
                summary["lineups"][lineup_id] = {
                    "ok": False, "error": "kept the previous guide; failed: " + ", ".join(bad)}
                continue
            _add_events(config, lineup_id, writer)
            try:
                writer.finish(guide_path(lineup_id))
                summary["lineups"][lineup_id] = {
                    "ok": True, "error": "", "channels": writer.channel_count,
                    "programmes": writer.programme_count}
            except Exception as e:
                log.warning("Could not write XMLTV for lineup %s: %s", lineup_id, e)
                summary["lineups"][lineup_id] = {"ok": False, "error": str(e)}
    finally:
        for writer in writers.values():
            writer.discard()
    summary["finished_at"] = _now()
    return summary


def _add_events(config, lineup_id, writer):
    """Airings read from event channels' stream names (app.guide.events), for channels with no guide of their own.
    A failure leaves them out and keeps the rest of the guide."""
    from app.guide import events
    try:
        channels, programmes = events.entries(config, lineup_id)
    except Exception:  # noqa: BLE001
        log.exception("event guide entries for lineup %s failed", lineup_id)
        return
    for elem, out in [(c, writer.channels) for c in channels] + [(p, writer.programmes) for p in programmes]:
        elem.tail = "\n"
        out.write("  " + ET.tostring(elem, encoding="unicode"))
    writer.channel_count += len(channels)
    writer.programme_count += len(programmes)


def _fetch_and_parse(url, provider, wanted, route, folder):
    """Download to a temp file, then parse it. The temp file is always removed.

    If parsing fails partway, fragments already written to the lineup writers are
    harmless: the provider is marked failed, so those lineups are discarded.
    """
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".tmp-dl-", suffix=".xml")
    os.close(fd)
    try:
        download(url, tmp, __import__("app").USER_AGENT)
        with open_source(tmp) as stream:
            return parse_source(stream, wanted, route)
    finally:
        _unlink(tmp)


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def empty_document():
    """A valid empty XMLTV document, for lineups that were never refreshed."""
    return '<?xml version="1.0" encoding="UTF-8"?>\n<tv generator-info-name="coax"></tv>\n'
