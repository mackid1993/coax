// Settings: server addresses, transcode preset (help text from the engine settings block), timings, XMLTV sources.
import { api } from "../api.js";
import { $, $$, esc, icon, toast, toastError, busy } from "../ui.js";
import { invalidate } from "../state.js";

const PRESETS = [
  ["off", "Off", "Pass through untouched. No extra CPU or GPU."],
  ["1080p60", "1080p60", "59.94 fps, 6 Mbps"],
  ["1080p30", "1080p30", "29.97 fps, 4.5 Mbps"],
  ["720p60", "720p60", "59.94 fps, 4.5 Mbps"],
  ["720p30", "720p30", "29.97 fps, 3 Mbps"],
  ["1080p50", "1080p50", "50 Hz countries"],
  ["1080p25", "1080p25", "50 Hz countries"],
  ["720p50", "720p50", "50 Hz countries"],
  ["720p25", "720p25", "50 Hz countries"],
];

let root, cfg, draft, dirty = false;

export async function mount(el) {
  root = el;
  root.innerHTML = `<div class="page-head"><div><div class="eyebrow">Appliance</div><h1>Settings</h1><p class="sub">Everything is stored in <code>/data/config.json</code>.</p></div></div>
    <div class="stack">${'<div class="card card-pad"><span class="skel" style="width:30%;height:16px"></span><span class="skel" style="margin-top:16px;height:38px"></span><span class="skel" style="margin-top:12px;height:38px"></span></div>'.repeat(2)}</div>`;
  try { cfg = await api.getConfig(); } catch (e) { toastError("Could not load settings", e); cfg = {}; }
  draft = {
    base_url: cfg.server?.base_url || "",
    port: cfg.server?.port ?? 7780,
    behind: cfg.server?.new_client_behind_seconds ?? 30,
    delay: cfg.server?.channel_shutdown_delay ?? 10,
    dvr: cfg.channels_dvr?.url || "",
    transcode: cfg.transcode || "off",
    xmltv_sources: JSON.parse(JSON.stringify(cfg.xmltv_sources || [])),
  };
  dirty = false;
  render();
}
export function unmount() {}

function render() {
  const d = draft;
  root.innerHTML = `<div class="page-head"><div><div class="eyebrow">Appliance</div><h1>Settings</h1><p class="sub">Everything is stored in <code>/data/config.json</code>. coax is for your LAN only: never expose it to the internet.</p></div></div>
  <div class="stack" id="sForm">
    <section class="card"><div class="card-head"><h2>Addresses</h2></div><div class="card-body form-grid">
      <div class="field"><label for="sBase">Base URL</label><input class="input mono" id="sBase" data-k="base_url" value="${esc(d.base_url)}" placeholder="http://192.168.1.50:7780"><span class="hint">How your DVR reaches coax. Used in the playlist stream URLs.</span></div>
      <div class="field"><label for="sDvr">DVR server URL</label><input class="input mono" id="sDvr" data-k="dvr" value="${esc(d.dvr)}" placeholder="http://192.168.1.50:8089"><span class="hint">Needed for Gracenote search, shared viewers and recordings. Port is usually 8089.</span></div>
      <div class="field"><label for="sPort">Port</label><input class="input mono" id="sPort" data-k="port" type="number" min="1" max="65535" value="${esc(d.port)}"><span class="hint">Takes effect after a restart.</span></div>
    </div></section>

    <section class="card"><div class="card-head"><h2>Transcode</h2><span class="badge badge-accent num">${esc(d.transcode)}</span></div><div class="card-body grid" style="grid-template-columns:minmax(0,1.3fr) minmax(0,1fr);gap:20px">
      <div><div class="preset-grid">${PRESETS.map(([v, n, desc]) => `<label class="preset"><input type="radio" name="tc" value="${v}" ${d.transcode === v ? "checked" : ""}><span class="p-name">${n}</span><span class="p-desc">${desc}</span></label>`).join("")}</div></div>
      <div class="help">
        <h4>Which frame rate?</h4><p>Pick the highest one your channels use.</p>
        <ul><li><b>60</b>: sports, news and most US broadcast channels. Smooth motion. A 30 fps channel simply shows each frame twice, so nothing is lost.</li>
        <li><b>30</b>: fine if your channels are mostly movies and entertainment, and easier on weak hardware, but 60 fps sports will look choppier.</li>
        <li><b>50 or 25</b>: the same choice for 50 Hz countries (Europe, UK, Australia and others). Do not use 60 or 30 there, it makes motion judder.</li></ul>
        <h4>Which size?</h4><p>1080p if you have a GPU. 720p for weak hardware or when encoding on the CPU. The bitrates match what IPTV providers send (about 3 to 6 Mbps), so quality holds without bloating the stream.</p>
        <h4>Why transcode?</h4><p>Every channel leaves coax in the same format, so a backup with a different frame rate never upsets the player, and the video plays at its real speed, except while the reserve refills: if a provider problem keeps the reserve below half for a while, video and audio play 3% slower together (pitch unchanged) until it is back to three quarters. Costs a few seconds more delay and some GPU time. Give the container a GPU (/dev/dri for Intel or AMD, the NVIDIA runtime for NVIDIA); without one it falls back to the CPU.</p>
      </div></div></section>

    <section class="card"><div class="card-head"><h2>Tuner sharing</h2></div><div class="card-body grid grid-2" style="gap:24px">
      <div class="field"><label for="sBehind">Join behind live</label><div class="range-row"><input type="range" id="sBehind" data-k="behind" min="0" max="120" step="5" value="${esc(d.behind)}"><span class="num" id="vBehind">${esc(d.behind)} s</span></div><span class="hint">A new viewer on a channel that is already playing starts this many seconds back in the shared buffer, so playback starts instantly with a cushion.</span></div>
      <div class="field"><label for="sDelay">Shutdown delay</label><div class="range-row"><input type="range" id="sDelay" data-k="delay" min="0" max="120" step="5" value="${esc(d.delay)}"><span class="num" id="vDelay">${esc(d.delay)} s</span></div><span class="hint">Keep a channel's engine running this long after the last viewer leaves, so quick channel flips back are instant.</span></div>
    </div></section>

    <section class="card"><div class="card-head"><div><h2>Standalone XMLTV sources</h2><p class="faint small">Extra guide feeds for the per-channel XMLTV picker, besides each provider's own feed.</p></div><button class="btn btn-sm" data-addsrc>${icon("plus")}Add source</button></div>
      <div class="card-body stack" id="xsList" style="gap:10px">${xsHtml()}</div></section>
  </div>
  <div id="saveSlot"></div>`;
  wire();
  drawSave();
}
function xsHtml() {
  if (!draft.xmltv_sources.length) return `<p class="faint small">None. Provider XMLTV feeds are always available.</p>`;
  return draft.xmltv_sources.map((s, i) => `<div class="line-row" style="grid-template-columns:1fr 2fr 110px 36px" data-xi="${i}">
    <div class="field"><label>Name</label><input class="input input-sm" data-xf="name" value="${esc(s.name || "")}"></div>
    <div class="field"><label>URL (gzip or plain)</label><input class="input input-sm mono" data-xf="url" value="${esc(s.url || "")}" placeholder="https://example.com/guide.xml.gz"></div>
    <div class="field"><label>Refresh (h)</label><input class="input input-sm mono" type="number" min="1" data-xf="refresh_hours" value="${esc(s.refresh_hours ?? 24)}"></div>
    <button class="btn btn-ghost btn-sm btn-icon btn-danger" data-rmsrc="${i}" aria-label="Remove source">${icon("trash")}</button></div>`).join("");
}
function wire() {
  const form = $("#sForm", root);
  form.addEventListener("input", (e) => {
    const t = e.target;
    if (t.dataset.k) {
      draft[t.dataset.k] = t.type === "range" || t.type === "number" ? Number(t.value) : t.value;
      if (t.id === "sBehind") $("#vBehind", root).textContent = `${t.value} s`;
      if (t.id === "sDelay") $("#vDelay", root).textContent = `${t.value} s`;
    } else if (t.dataset.xf) {
      const i = Number(t.closest("[data-xi]").dataset.xi);
      draft.xmltv_sources[i][t.dataset.xf] = t.type === "number" ? Number(t.value) : t.value;
    } else if (t.name === "tc") {
      draft.transcode = t.value;
      $(".card-head .badge-accent", root).textContent = t.value;
    }
    markDirty();
  });
  form.addEventListener("click", (e) => {
    if (e.target.closest("[data-addsrc]")) {
      draft.xmltv_sources.push({ id: "", name: "", url: "", refresh_hours: 24 });
      $("#xsList", root).innerHTML = xsHtml(); markDirty();
      $$("#xsList [data-xf='name']", root).pop()?.focus();
    }
    const rm = e.target.closest("[data-rmsrc]");
    if (rm) { draft.xmltv_sources.splice(Number(rm.dataset.rmsrc), 1); $("#xsList", root).innerHTML = xsHtml(); markDirty(); }
  });
}
function markDirty() { if (!dirty) { dirty = true; drawSave(); } }
function drawSave() {
  $("#saveSlot", root).innerHTML = dirty ? `<div class="save-bar">${icon("info", 'style="width:18px;height:18px;color:var(--accent)"')}<span>You have unsaved changes.</span><span class="spacer"></span><button class="btn btn-ghost" data-revert>Revert</button><button class="btn btn-primary" data-save>${icon("check")}Save settings</button></div>` : "";
  if (!dirty) return;
  $("[data-revert]", root).onclick = () => mount(root);
  $("[data-save]", root).onclick = (e) => busy(e.currentTarget, save);
}
const urlOk = (u) => !u || /^https?:\/\/[^\s/]+/.test(u);
async function save() {
  const d = draft;
  if (!urlOk(d.base_url)) return toast("Base URL looks wrong", "Use http://IP:PORT", "warn");
  if (!urlOk(d.dvr)) return toast("DVR server URL looks wrong", "Use http://IP:8089", "warn");
  if (d.xmltv_sources.some((s) => !s.url || !urlOk(s.url))) return toast("Check XMLTV sources", "Every source needs an http(s) URL.", "warn");
  const sources = d.xmltv_sources.map((s, i) => ({ id: s.id || `x${i + 1}`, name: s.name || `XMLTV ${i + 1}`, url: s.url, refresh_hours: Number(s.refresh_hours) || 24 }));
  try {
    cfg = await api.putConfig({
      server: { base_url: d.base_url.replace(/\/+$/, ""), port: d.port, new_client_behind_seconds: d.behind, channel_shutdown_delay: d.delay },
      channels_dvr: { url: d.dvr.replace(/\/+$/, "") },
      transcode: d.transcode,
      xmltv_sources: sources,
    });
    invalidate("config");
    dirty = false;
    drawSave();
    toast("Settings saved", cfg?.server?.port && cfg.server.port !== location.port * 1 && location.port ? "Restart coax for a port change to take effect." : "", "ok");
  } catch (e) { toastError("Could not save settings", e); }
}
