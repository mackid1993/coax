// Status: live sessions with sparklines, line usage, refresh results, network, and the log tail. Polls every 3 s.
import { api } from "../api.js";
import { $, esc, flag, icon, logo, emptyState, fmtBytes, fmtRate, fmtDuration, fmtAgo, fmtInt, toastError } from "../ui.js";
import { refreshNetwork } from "../app.js";

const POLL_MS = 3000;
const SPARK_N = 40;
let root, timer, logTimer, history = new Map(), lastBytes = new Map(), logSeq = null, logLines = [], failed = false, follow = true, net = null;

export async function mount(el) {
  root = el;
  history = new Map(); lastBytes = new Map(); logSeq = null; logLines = []; follow = true;
  root.innerHTML = `<div class="page-head"><div><div class="eyebrow">Live</div><h1>Status</h1><p class="sub">Sessions, connection slots and provider health, updated every few seconds.</p></div></div>
    <div class="kpis" id="kpis">${'<div class="card kpi"><span class="skel" style="width:50%"></span><span class="skel" style="width:40%;height:26px;margin-top:10px"></span></div>'.repeat(4)}</div>
    <div class="grid" style="grid-template-columns:minmax(0,2fr) minmax(0,1fr);align-items:start">
      <div class="stack">
        <section><div class="row" style="margin-bottom:10px"><h2 style="font-size:15px;font-weight:620">Now playing</h2></div><div class="grid grid-auto" id="sessions"></div></section>
        <section class="card"><div class="card-head"><h2>Log</h2><label class="switch small"><input type="checkbox" id="follow" checked><span class="track"></span><span>Follow</span></label></div><div class="log" id="log" tabindex="0" aria-label="Log tail" aria-live="off"></div></section>
      </div>
      <div class="stack">
        <section class="card"><div class="card-head"><h2>Network</h2><button class="btn btn-ghost btn-sm btn-icon" id="netRefresh" aria-label="Check public IP again">${icon("refresh")}</button></div><div class="card-body" id="net"><span class="skel" style="width:60%"></span></div></section>
        <section class="card"><div class="card-head"><h2>Lines</h2></div><div class="card-body" id="lines" style="padding-top:6px;padding-bottom:6px"></div></section>
        <section class="card"><div class="card-head"><h2>Last refresh</h2></div><div class="card-body" id="refresh" style="padding-top:6px;padding-bottom:6px"></div></section>
      </div>
    </div>`;
  $("#follow", root).onchange = (e) => { follow = e.target.checked; if (follow) scrollLog(); };
  $("#netRefresh", root).onclick = async (e) => {
    const b = e.currentTarget; b.classList.add("loading");
    try { await refreshNetwork(true); } catch (err) { toastError("Could not check public IP", err); } finally { b.classList.remove("loading"); }
  };
  window.addEventListener("coax:network", onNet);
  refreshNetwork().catch(() => { net = { error: "Could not reach /api/network" }; drawNet(); });
  await poll();
  pollLog();
  timer = setInterval(poll, POLL_MS);
  logTimer = setInterval(pollLog, POLL_MS);
}
export function unmount() { clearInterval(timer); clearInterval(logTimer); window.removeEventListener("coax:network", onNet); }
function onNet(e) { net = e.detail; drawNet(); }

function drawNet() {
  const el = $("#net", root);
  if (!el || !net) return;
  el.innerHTML = `<div class="row" style="align-items:flex-start;gap:14px"><div class="kpi-icon" style="position:static;flex:none">${icon("signal")}</div>
    <div style="min-width:0"><div class="stat-label">Public IP</div><div class="stat-value" style="font-size:18px">${esc(net.ip || "unknown")}</div>
    <div class="faint small">${net.country ? `${flag(net.country)} ${esc(net.country)} · ` : ""}checked ${net.checked_at ? fmtAgo(net.checked_at) : "never"}</div>
    ${net.error ? `<div class="small" style="color:var(--bad);margin-top:4px">${esc(net.error)}</div>` : ""}</div></div>
    <p class="faint small" style="margin-top:10px">Behind a VPN this should be the VPN's address, not your home connection.</p>`;
}

async function poll() {
  let s;
  try { s = await api.status(); failed = false; }
  catch (e) {
    if (!failed) toastError("Status unavailable", e);
    failed = true;
    return;
  }
  if (!root.isConnected) return;
  const sessions = s.sessions || [];
  const now = Date.now();
  for (const x of sessions) {
    const key = skey(x);
    let rate = Number(x.bitrate_bps) || 0;
    const prev = lastBytes.get(key);
    if (!rate && prev && x.bytes != null) rate = ((x.bytes - prev.b) * 8) / Math.max(0.5, (now - prev.t) / 1000);
    if (x.bytes != null) lastBytes.set(key, { b: x.bytes, t: now });
    const h = history.get(key) || [];
    h.push(Math.max(0, rate));
    while (h.length > SPARK_N) h.shift();
    history.set(key, h);
  }
  const viewers = sessions.reduce((a, x) => a + (x.clients || []).length, 0);
  const total = sessions.reduce((a, x) => a + (Number(x.bitrate_bps) || (history.get(skey(x)) || []).slice(-1)[0] || 0), 0);
  const lines = s.lines || [];
  const used = lines.reduce((a, l) => a + (l.used || 0), 0);
  const cap = lines.some((l) => !l.max) ? "∞" : lines.reduce((a, l) => a + (l.max || 0), 0);
  $("#kpis", root).innerHTML = [
    kpi("Live channels", fmtInt(sessions.length), "channels", `${fmtInt(s.channels?.enabled ?? 0)} of ${fmtInt(s.channels?.total ?? 0)} enabled`),
    kpi("Viewers", fmtInt(viewers), "users", "across all channels"),
    kpi("Line slots", `${used}<span class="faint" style="font-size:16px"> / ${cap}</span>`, "gauge", `${lines.length} line${lines.length === 1 ? "" : "s"}`),
    kpi("Throughput", fmtRate(total), "signal", `up ${fmtDuration(s.uptime)}${s.hub_running === false ? " · hub stopped" : ""}`),
  ].join("");
  drawSessions(sessions);
  drawLines(lines);
  drawRefresh(s.refresh || {});
}
const kpi = (label, value, ic, sub) => `<div class="card kpi"><div class="kpi-icon">${icon(ic)}</div><div class="stat-label">${label}</div><div class="stat-value">${value}</div><div class="faint small">${esc(sub)}</div></div>`;

function drawSessions(sessions) {
  const el = $("#sessions", root);
  if (!sessions.length) {
    el.style.gridTemplateColumns = "1fr";
    el.innerHTML = `<div class="card">${emptyState({ art: "signal", title: "Nothing is playing", text: "When a client tunes a channel, it appears here with its viewers, line and throughput." })}</div>`;
    return;
  }
  el.style.gridTemplateColumns = "";
  if (!el.querySelector(".session-card")) el.innerHTML = "";
  const keep = new Set();
  for (const x of sessions) {
    const key = skey(x);
    keep.add(key);
    const inner = cardInner(x, key);
    let card = el.querySelector(`.session-card[data-key="${CSS.escape(key)}"]`);
    if (!card) {
      card = document.createElement("article");
      card.className = "card session-card";
      card.dataset.key = key;
      el.appendChild(card);
    }
    if (card._html === inner) continue;
    const det = card.querySelector("details.events");
    if (det) openEvents.set(key, det.open);
    const ol = card.querySelector("details.events ol");
    const top = ol ? ol.scrollTop : 0;
    card.innerHTML = inner;
    const ol2 = card.querySelector("details.events ol");
    if (ol2 && top) ol2.scrollTop = top;
    card._html = inner;
    const det2 = card.querySelector("details.events");
    if (det2 && openEvents.has(key)) det2.open = openEvents.get(key);
  }
  for (const card of [...el.querySelectorAll(".session-card")]) if (!keep.has(card.dataset.key)) card.remove();
}
const openEvents = new Map();
function skey(x) { return `${x.uuid || x.channel_id}~${x.instance || 0}`; }
function cardInner(x, key) {
  {
    const h = history.get(key) || [];
    const clients = x.clients || [];
    const state = (x.state || "live").toLowerCase();
    return `<div class="head">${logo(x.logo, x.channel)}<div style="min-width:0;flex:1"><div class="title">${esc(x.channel || `Channel ${x.channel_id}`)}</div>
        <div class="faint small ellip">${esc(x.provider || x.provider_id || "")}${x.line ? ` · ${esc(x.line)}` : ""}</div></div>
        ${state === "live" || state === "running" ? `<span class="live-pill">LIVE</span>` : `<span class="badge badge-warn">${esc(state)}</span>`}</div>
      ${spark(h, key)}
      <div class="session-stats">
        <div><div class="stat-label">Viewers</div><div class="stat-value">${clients.length}</div></div>
        <div><div class="stat-label">Bitrate</div><div class="stat-value">${fmtRate(h[h.length - 1] || 0)}</div></div>
        <div><div class="stat-label">Uptime</div><div class="stat-value">${fmtDuration(x.uptime)}</div></div>
      </div>
      <div class="row row-wrap" style="gap:6px">${tcBadge(x)}${speedBadge(x)}${feedBadge(x)}${x.degraded ? `<span class="badge badge-warn" title="The last 10 minutes had repeated failovers or long gaps without video. coax is still recovering it on its own."><span class="dot"></span>Unsteady feed</span>` : ""}</div>
      ${fmtHtml(x)}
      ${eventsHtml(x)}
      <div class="faint small" style="display:flex;gap:10px;flex-wrap:wrap"><span>${fmtBytes(x.bytes)} sent</span>${x.restarts ? `<span style="color:var(--warn)">${x.restarts} restart${x.restarts === 1 ? "" : "s"}</span>` : ""}</div>
      ${clients.length ? `<div class="client-list">${clients.map((c) => `<span class="chip" title="${esc(c.user_agent || "")}" style="padding-right:10px">${icon("users", 'style="width:12px;height:12px"')}<span class="num">${esc(c.ip)}</span>${c.connected ? `<span class="faint">${fmtDuration(c.connected > 1e9 ? Date.now() / 1000 - c.connected : c.connected)}</span>` : ""}</span>`).join("")}</div>` : ""}`;
  }
}
function tcBadge(x) {
  const t = x.transcode || {};
  if (t.on) return `<span class="badge badge-ok" title="Encoding with ${esc(t.encoder || "")}"><span class="dot pulse"></span>Transcoding ${esc(t.profile || "")} · ${esc(/NVENC/.test(t.encoder) ? "NVENC" : /VAAPI/.test(t.encoder) ? "VAAPI" : "CPU")}</span>`;
  if (t.profile && t.profile !== "off") return `<span class="badge badge-warn" title="Transcoding is set but the engine has not reported its encoder yet"><span class="dot"></span>Transcode ${esc(t.profile)} starting</span>`;
  return `<span class="badge" title="Streams pass through untouched"><span class="dot" style="opacity:.5"></span>Passthrough</span>`;
}
function fmtHtml(x) {
  const f = x.input || {};
  if (!f.video && !f.audio) return "";
  return `<div class="src-fmt" title="The format the provider sends, before coax touches it. With transcoding on, Channels DVR gets coax's re-encoded stream instead."><span class="badge">From provider</span>
    <div class="src-fmt-lines small num">${f.video ? `<div>${esc(f.video)}</div>` : ""}${f.audio ? `<div>${esc(f.audio)}</div>` : ""}</div></div>`;
}
function feedBadge(x) {
  const n = Number(x.stream_no) || 1;
  return n === 1 ? `<span class="badge" title="Playing the channel's first stream"><span class="dot"></span>Main stream</span>`
    : `<span class="badge badge-warn" title="Playing a backup stream after a failover"><span class="dot pulse"></span>Backup stream ${n}</span>`;
}
const EV_ICON = { failover: "⇄", placeholder: "⛔", avsync: "♪", rejoin: "↻", speed: "⏱", reserve: "▣", stall: "…",
  error: "!", keepalive: "♥", recording: "●", viewers: "👥", transcode: "⚙", restart: "↻" };
function eventsHtml(x) {
  const ev = (x.events || []).slice().reverse();
  if (!ev.length) return `<div class="ev-empty faint small">No corrections needed so far.</div>`;
  const time = (t) => new Date(t * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" });
  return `<details class="events" ${ev.some((e) => e.level !== "info" && Date.now() / 1000 - e.t < 600) ? "open" : ""}>
    <summary><span>What coax corrected</span><span class="faint small">${ev.length} event${ev.length === 1 ? "" : "s"}</span></summary>
    <ol>${ev.map((e) => `<li class="ev ev-${esc(e.level)}"><span class="ev-t num">${esc(time(e.t))}</span><span class="ev-i">${EV_ICON[e.kind] || "•"}</span><span class="ev-x">${esc(e.text)}</span></li>`).join("")}</ol></details>`;
}
function speedBadge(x) {
  const s = x.speed || {};
  const r = s.reserve != null ? ` · ${s.reserve}s reserve` : "";
  if (s.empty) return `<span class="badge badge-bad" title="The reserve is empty: video plays as fast as the provider sends it, so stalls show on screen"><span class="dot"></span>Live edge, no reserve</span>`;
  if (s.slow) return `<span class="badge badge-warn" title="Refilling the reserve: video and audio play 3% slower until it is back up"><span class="dot pulse"></span>Behind real time, 3% slower${esc(r)}</span>`;
  return `<span class="badge badge-ok" title="Playing at real speed"><span class="dot"></span>Real time${esc(r)}</span>`;
}
function spark(vals, key) {
  const w = 300, h = 44;
  if (vals.length < 2) return `<svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" aria-hidden="true"><line x1="0" y1="${h - 2}" x2="${w}" y2="${h - 2}" stroke="var(--border-strong)" stroke-dasharray="3 4"/></svg>`;
  const max = Math.max(...vals, 1);
  const step = w / (SPARK_N - 1);
  const off = (SPARK_N - vals.length) * step;
  const pts = vals.map((v, i) => [off + i * step, h - 3 - (v / max) * (h - 8)]);
  const line = pts.map((p, i) => `${i ? "L" : "M"}${p[0].toFixed(1)} ${p[1].toFixed(1)}`).join(" ");
  const id = `sg-${String(key).replace(/[^a-z0-9]/gi, "")}`;
  return `<svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" role="img" aria-label="Throughput over the last two minutes">
    <defs><linearGradient id="${id}" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="var(--accent)" stop-opacity=".35"/><stop offset="1" stop-color="var(--accent)" stop-opacity="0"/></linearGradient>
    <linearGradient id="${id}l" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#5EEAD4"/><stop offset=".55" stop-color="#22D3EE"/><stop offset="1" stop-color="#6366F1"/></linearGradient></defs>
    <path d="${line} L${pts[pts.length - 1][0]} ${h} L${pts[0][0]} ${h} Z" fill="url(#${id})"/>
    <path d="${line}" fill="none" stroke="url(#${id}l)" stroke-width="1.8" stroke-linejoin="round" stroke-linecap="round" vector-effect="non-scaling-stroke"/>
    <circle cx="${pts[pts.length - 1][0]}" cy="${pts[pts.length - 1][1]}" r="2.6" fill="var(--copper)"/></svg>`;
}

function drawLines(lines) {
  const el = $("#lines", root);
  if (!lines.length) { el.innerHTML = `<p class="faint small" style="padding:14px 0">No provider lines yet. <a href="#/providers">Add a provider</a>.</p>`; return; }
  el.innerHTML = lines.map((l) => {
    const max = Number(l.max) || 0, used = Number(l.used) || 0;
    const meter = max && max <= 12
      ? `<div class="meter">${Array.from({ length: max }, (_, i) => `<span class="seg-slot ${i < used ? (used >= max ? "full" : "on") : ""}"></span>`).join("")}</div>`
      : `<div class="meter fill"><span style="width:${max ? Math.min(100, (used / max) * 100) : used ? 100 : 0}%;${!max ? "opacity:.5" : ""}"></span></div>`;
    return `<div class="line-usage"><div class="row"><span style="font-weight:600" class="ellip">${esc(l.provider_name || l.provider)}</span><span class="faint small ellip">${esc(l.line_name || l.line)}</span><span class="spacer"></span><span class="num small ${max && used >= max ? "copper" : ""}">${used} / ${max || "∞"}</span></div>
      ${meter}${(l.channels || []).length ? `<div class="faint small ellip">${l.channels.map((c) => esc(c.name || c.id)).join(", ")}</div>` : ""}</div>`;
  }).join("");
}

function drawRefresh(map) {
  const el = $("#refresh", root);
  const items = Object.values(map || {});
  if (!items.length) { el.innerHTML = `<p class="faint small" style="padding:14px 0">No refresh has run since startup.</p>`; return; }
  el.innerHTML = items.map((r) => {
    const st = r.state === "running" ? `<span class="badge badge-info"><span class="dot"></span>running</span>` : r.ok === false || r.state === "error" ? `<span class="badge badge-bad"><span class="dot"></span>failed</span>` : `<span class="badge badge-ok"><span class="dot"></span>ok</span>`;
    return `<div class="refresh-row"><div style="flex:1;min-width:0"><div class="row" style="gap:8px"><span style="font-weight:600" class="ellip">${esc(r.name || r.provider)}</span>${st}</div>
      <div class="faint small">${r.state === "error" || r.ok === false ? esc(r.error || "") : `${fmtInt(r.streams)} streams · +${fmtInt(r.added || 0)} new · ${fmtInt(r.vanished_count || 0)} vanished${r.auto_added ? ` · ${fmtInt(r.auto_added)} auto-added` : ""}`}</div></div>
      <span class="faint small num" title="${esc(r.finished_at || r.at || "")}">${fmtAgo(r.finished_at || r.at || r.started_at)}</span></div>`;
  }).join("");
}

async function pollLog() {
  let r;
  try { r = await api.log(logSeq ?? undefined, logSeq == null ? 300 : 500); } catch { return; }
  if (!root.isConnected) return;
  const lines = Array.isArray(r) ? r : r?.lines || [];
  if (r?.last_seq != null) logSeq = r.last_seq;
  if (!lines.length && logLines.length) return;
  logLines = logLines.concat(lines).slice(-1000);
  const el = $("#log", root);
  if (!logLines.length) { el.innerHTML = `<span class="ts">Waiting for log lines…</span>`; return; }
  el.innerHTML = logLines.map((l) => {
    if (typeof l === "string") return esc(l);
    const lv = ({ WARNING: "WARN", CRITICAL: "CRIT", ERROR: "ERROR" })[String(l.level || "").toUpperCase()] || String(l.level || "").toUpperCase();
    const cls = lv.startsWith("ERR") || lv === "CRIT" ? "lv-err" : lv.startsWith("WARN") ? "lv-warn" : "";
    const ts = l.ts ? (typeof l.ts === "number" ? new Date(l.ts * 1000) : new Date(l.ts)).toLocaleTimeString([], { hour12: false }) : "";
    return `<span class="ts">${esc(ts)}</span> <span class="${cls}">${esc(lv.padEnd(5).slice(0, 5))}</span> ${l.name ? `<span class="ts">${esc(l.name)}</span> ` : ""}<span class="${cls}">${esc(l.msg ?? "")}</span>`;
  }).join("\n");
  if (follow) scrollLog();
}
function scrollLog() { const el = $("#log", root); if (el) el.scrollTop = el.scrollHeight; }
