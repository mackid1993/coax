// Logs: live, verbose view of everything coax is doing (engine, hub, providers, guide, DVR sync).
import { api } from "../api.js";
import { $, esc, icon } from "../ui.js";

let root, timer, seq = null, lines = [], follow = true, paused = false;
const view = { level: "all", source: "all", q: "" };
const MAX = 10000;
const POLL_MS = 1500;

export async function mount(el) {
  root = el;
  seq = null; lines = []; follow = true; paused = false;
  root.innerHTML = `<div class="page-head"><div><div class="eyebrow">Diagnostics</div><h1>Logs</h1>
    <p class="sub">Everything coax is doing, live: tunes, engine output, provider and guide refreshes, Channels DVR sync. Passwords are masked.</p></div>
    <div class="page-actions">
      <button class="btn" data-pause>${icon("pause")}<span>Pause</span></button>
      <button class="btn" data-clear>${icon("trash")}Clear view</button>
      <a class="btn" href="/api/log/download">${icon("download")}Download</a>
    </div></div>
  <section class="card">
    <div class="toolbar" style="padding:12px 14px;border-bottom:1px solid var(--border)">
      <label class="search" style="flex:1 1 220px"><span class="sr-only">Filter log</span>${icon("search")}<input class="input input-sm" id="lQ" placeholder="Filter, e.g. a channel name, error, failover"></label>
      <select class="input input-sm" id="lLevel" style="width:auto"><option value="all">All levels</option><option value="warn">Warnings and errors</option><option value="error">Errors only</option></select>
      <select class="input input-sm" id="lSource" style="width:auto"><option value="all">All sources</option><option value="coax.hub">Streams and engine</option><option value="coax.providers">Providers</option><option value="coax.guide">Guide</option><option value="coax.dvr">Channels DVR sync</option><option value="coax.server">Web and settings</option></select>
      <span class="faint small num" id="lCount"></span>
    </div>
    <div class="log" id="log" style="height:calc(100vh - 290px);min-height:320px;border-radius:0 0 var(--radius-l) var(--radius-l)"></div>
  </section>`;
  const log = $("#log", root);
  log.addEventListener("scroll", () => { follow = log.scrollTop + log.clientHeight >= log.scrollHeight - 30; });
  $("#lQ", root).addEventListener("input", (e) => { view.q = e.target.value.trim().toLowerCase(); draw(); });
  $("#lLevel", root).addEventListener("change", (e) => { view.level = e.target.value; draw(); });
  $("#lSource", root).addEventListener("change", (e) => { view.source = e.target.value; draw(); });
  $("[data-clear]", root).onclick = () => { lines = []; draw(); };
  $("[data-pause]", root).onclick = (e) => {
    paused = !paused;
    e.currentTarget.innerHTML = `${icon(paused ? "play" : "pause")}<span>${paused ? "Resume" : "Pause"}</span>`;
    if (!paused) draw();
  };
  await poll();
  timer = setInterval(poll, POLL_MS);
}
export function unmount() { clearInterval(timer); }

async function poll() {
  let r;
  try { r = await api.log(seq ?? undefined, seq == null ? 2000 : 2000); } catch { return; }
  if (!root || !root.isConnected) return;
  const got = Array.isArray(r) ? r : r?.lines || [];
  if (r?.last_seq != null) seq = r.last_seq;
  if (got.length) {
    lines = lines.concat(got).slice(-MAX);
    if (paused) return;
    if (view.level === "all" && view.source === "all" && !view.q && lines.length < MAX) append(got);
    else draw();
  }
  else if (!lines.length) draw();
}

const lvOf = (l) => String(l.level || "").toUpperCase();
function keep(l) {
  const lv = lvOf(l);
  if (view.level === "warn" && !/WARN|ERROR|CRIT/.test(lv)) return false;
  if (view.level === "error" && !/ERROR|CRIT/.test(lv)) return false;
  if (view.source !== "all" && !String(l.name || "").startsWith(view.source)) return false;
  if (view.q && !`${l.name} ${l.msg}`.toLowerCase().includes(view.q)) return false;
  return true;
}
const lineHtml = (l) => {
  const lv = lvOf(l);
  const cls = /ERROR|CRIT/.test(lv) ? "lv-err" : /WARN/.test(lv) ? "lv-warn" : "";
  const t = l.ts ? String(l.ts).replace("T", " ").slice(5, 23) : "";
  const tag = ({ WARNING: "WARN", CRITICAL: "CRIT" })[lv] || lv;
  return `<span class="ts">${esc(t)}</span> <span class="${cls}">${esc(tag.padEnd(5).slice(0, 5))}</span> <span class="ts">${esc(String(l.name || "").replace(/^coax\./, ""))}</span> <span class="${cls}">${esc(l.msg ?? "")}</span>`;
};
function append(got) {
  const el = $("#log", root);
  if (!el) return;
  if (el.dataset.empty) { el.textContent = ""; delete el.dataset.empty; }
  el.insertAdjacentHTML("beforeend", (el.childNodes.length ? "\n" : "") + got.map(lineHtml).join("\n"));
  $("#lCount", root).textContent = `${lines.length} lines`;
  if (follow) el.scrollTop = el.scrollHeight;
}
function draw() {
  const el = $("#log", root);
  if (!el) return;
  const shown = lines.filter(keep);
  $("#lCount", root).textContent = `${shown.length} of ${lines.length} lines${paused ? " · paused" : ""}`;
  if (!shown.length) { el.innerHTML = `<span class="ts">${lines.length ? "No lines match the filter." : "Waiting for log lines…"}</span>`; el.dataset.empty = "1"; return; }
  delete el.dataset.empty;
  el.innerHTML = shown.map((l) => {
    const lv = lvOf(l);
    const cls = /ERROR|CRIT/.test(lv) ? "lv-err" : /WARN/.test(lv) ? "lv-warn" : "";
    const t = l.ts ? String(l.ts).replace("T", " ").slice(5, 23) : "";
    const tag = ({ WARNING: "WARN", CRITICAL: "CRIT" })[lv] || lv;
    return `<span class="ts">${esc(t)}</span> <span class="${cls}">${esc(tag.padEnd(5).slice(0, 5))}</span> <span class="ts">${esc(String(l.name || "").replace(/^coax\./, ""))}</span> <span class="${cls}">${esc(l.msg ?? "")}</span>`;
  }).join("\n");
  if (follow) el.scrollTop = el.scrollHeight;
}
