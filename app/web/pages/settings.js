// Settings: server addresses, transcode preset (help text from the engine settings block), timings, XMLTV sources.
import { api } from "../api.js";
import { $, $$, esc, icon, toast, toastError, busy, confirmDialog } from "../ui.js";
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
    dvr: cfg.channels_dvr?.url || "",
    transcode: cfg.transcode || "off",
    xmltv_sources: JSON.parse(JSON.stringify(cfg.xmltv_sources || [])),
    regions: JSON.parse(JSON.stringify(cfg.gracenote?.regions || [])),
    timezone: cfg.timezone || "",
    autosave: cfg.autosave_minutes ?? 2,
    backups: { enabled: cfg.backups?.enabled !== false, time: cfg.backups?.time || "03:30", keep: cfg.backups?.keep ?? 14 },
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
      <div class="field"><label for="sBase">Base URL</label><input class="input mono" autocomplete="off" autocorrect="off" autocapitalize="off" spellcheck="false" id="sBase" data-k="base_url" value="${esc(d.base_url)}" placeholder="http://192.168.1.50:7780"><span class="hint">How your DVR reaches coax. Used in the playlist stream URLs.</span></div>
      <div class="field"><label for="sDvr">DVR server URL</label><input class="input mono" autocomplete="off" autocorrect="off" autocapitalize="off" spellcheck="false" id="sDvr" data-k="dvr" value="${esc(d.dvr)}" placeholder="http://192.168.1.50:8089"><span class="hint">Needed for Gracenote search, shared viewers and recordings. Port is usually 8089.</span></div>
      <div class="field"><label for="sTz">Time zone</label><input class="input mono" autocomplete="off" spellcheck="false" id="sTz" data-k="timezone" list="tzList" value="${esc(d.timezone)}" placeholder="${esc(cfg.timezone_effective?.name || "UTC")} (automatic)"><datalist id="tzList">${(Intl.supportedValuesOf ? Intl.supportedValuesOf("timeZone") : []).map((z) => `<option value="${esc(z)}">`).join("")}</datalist><span class="hint">Used for the backup schedule and log times. Now ${esc(cfg.timezone_effective?.now || "")}, from ${esc(cfg.timezone_effective?.source || "default")}. Leave empty for automatic.</span></div>
      <div class="field"><label for="sPort">Port</label><input class="input mono" autocomplete="off" autocorrect="off" autocapitalize="off" spellcheck="false" id="sPort" data-k="port" type="number" min="1" max="65535" value="${esc(d.port)}"><span class="hint">Takes effect after a restart.</span></div>
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


    <section class="card"><div class="card-head"><div><h2>Auto-match region</h2><p class="faint small">Where you watch from, for guide matching: Auto-match only trusts Gracenote stations in these areas&#39; lineups. Add more than one if you watch channels from several countries. Backup streams come only from the groups you picked for each provider.</p></div><button class="btn btn-sm" data-radd>${icon("plus")}Add region</button></div>
      <div class="card-body stack" id="rgList" style="gap:10px">${regionsHtml()}</div></section>

    <section class="card"><div class="card-head"><div><h2>Backups</h2><p class="faint small">Copies of every setting, provider, channel and lineup, plus Smart Standby history, the bell and each channel's stream history, kept in <code>/data/backups</code>. Restoring backs up the current state first.</p></div><button class="btn btn-sm" data-bknow>${icon("download")}Back up now</button></div>
      <div class="card-body stack" style="gap:12px">
        <div class="row row-wrap" style="gap:16px;align-items:end">
          <label class="switch"><input type="checkbox" data-bk="enabled" ${d.backups.enabled ? "checked" : ""}><span class="track"></span><span>Daily backup</span></label>
          <div class="field"><label for="sAuto">Autosave checked suggestions every</label><select class="input input-sm" id="sAuto" data-k="autosave">${[[0, "Off"], [1, "1 minute"], [2, "2 minutes"], [5, "5 minutes"], [10, "10 minutes"]].map(([v, t]) => `<option value="${v}" ${Number(d.autosave) === v ? "selected" : ""}>${t}</option>`).join("")}</select></div>
          <div class="field"><label>At</label><input class="input input-sm mono" type="time" data-bk="time" value="${esc(d.backups.time)}"></div>
          <div class="field"><label>Keep</label><input class="input input-sm mono" type="number" min="1" max="365" data-bk="keep" value="${esc(d.backups.keep)}"></div>
          <span class="faint small" id="bkTz"></span></div>
        <div class="stack" id="bkList" style="gap:8px"><span class="skel" style="height:40px"></span></div></div></section>

    <section class="card"><div class="card-head"><div><h2>Standalone XMLTV sources</h2><p class="faint small">Extra guide feeds for the per-channel XMLTV picker, besides each provider's own feed.</p></div><button class="btn btn-sm" data-addsrc>${icon("plus")}Add source</button></div>
      <div class="card-body stack" id="xsList" style="gap:10px">${xsHtml()}</div></section>
  </div>
  <div id="saveSlot"></div>`;
  wire();
  wireExtra();
  drawSave();
}
function xsHtml() {
  if (!draft.xmltv_sources.length) return `<p class="faint small">None. Provider XMLTV feeds are always available.</p>`;
  return draft.xmltv_sources.map((s, i) => `<div class="line-row xs-row" data-xi="${i}">
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
const fixUrl = (u) => String(u || "").trim().replace(/^(https?):\/*/i, "$1://").replace(/\/+$/, "");
const urlOk = (u) => !u || /^https?:\/\/[^\s/]+/.test(u);
async function save() {
  const d = draft;
  d.base_url = fixUrl(d.base_url); d.dvr = fixUrl(d.dvr);
  if (!d.base_url) return toast("Base URL is required", "How Channels DVR reaches coax, e.g. http://192.168.1.50:7780", "warn");
  if (!urlOk(d.base_url)) return toast("Base URL looks wrong", "Use http://IP:PORT", "warn");
  if (!urlOk(d.dvr)) return toast("DVR server URL looks wrong", "Use http://IP:8089", "warn");
  if (d.xmltv_sources.some((s) => !s.url || !urlOk(s.url))) return toast("Check XMLTV sources", "Every source needs an http(s) URL.", "warn");
  const sources = d.xmltv_sources.map((s, i) => ({ id: s.id || `x${i + 1}`, name: s.name || `XMLTV ${i + 1}`, url: s.url, refresh_hours: Number(s.refresh_hours) || 24 }));
  try {
    cfg = await api.putConfig({
      server: { base_url: d.base_url.replace(/\/+$/, ""), port: d.port },
      channels_dvr: { url: d.dvr.replace(/\/+$/, "") },
      transcode: d.transcode,
      xmltv_sources: sources,
      timezone: (d.timezone || "").trim(),
      autosave_minutes: Number(d.autosave) || 0,
      gracenote: { regions: d.regions.filter((r) => r.country && String(r.postal || "").trim()) },
      backups: { enabled: !!d.backups.enabled, time: d.backups.time || "03:30", keep: Number(d.backups.keep) || 14 },
    });
    invalidate("config");
    dirty = false;
    drawSave();
    toast("Settings saved", cfg?.server?.port && cfg.server.port !== location.port * 1 && location.port ? "Restart coax for a port change to take effect." : "", "ok");
  } catch (e) { toastError("Could not save settings", e); }
}

// ---------- Auto-match region and backups ----------
const COUNTRIES = [
  ["USA", "United States", "10001"], ["CAN", "Canada", "M5H2N2"], ["MEX", "Mexico", "06600"], ["PRI", "Puerto Rico", "00901"],
  ["GBR", "United Kingdom", "SW1A"], ["IRL", "Ireland", "D02"], ["DEU", "Germany", "10115"], ["FRA", "France", "75001"],
  ["ESP", "Spain", "28001"], ["ITA", "Italy", "00118"], ["NLD", "Netherlands", "1012"], ["BEL", "Belgium", "1000"],
  ["CHE", "Switzerland", "8001"], ["AUT", "Austria", "1010"], ["SWE", "Sweden", "11120"], ["NOR", "Norway", "0150"],
  ["DNK", "Denmark", "1050"], ["FIN", "Finland", "00100"], ["AUS", "Australia", "2000"], ["KOR", "South Korea", "03000"],
  ["BRA", "Brazil", "01001"], ["ARG", "Argentina", "C1002"], ["CHL", "Chile", "8320000"], ["COL", "Colombia", "110111"],
  ["PER", "Peru", "15001"], ["DOM", "Dominican Republic", "10101"], ["JAM", "Jamaica", "JMAKN01"], ["PAN", "Panama", "0801"],
  ["CRI", "Costa Rica", "10101"], ["BMU", "Bermuda", "HM11"],
];
function regionsHtml() {
  if (!draft.regions.length) return `<p class="faint small">No region set: guide matching searches all of Gracenote, so stations from other countries can show up. Add your country and postal code.</p>`;
  return draft.regions.map((r, i) => `<div class="line-row region-row">
    <div class="field"><label>Country</label><select class="input input-sm" data-rc="${i}">${COUNTRIES.map(([c, n]) => `<option value="${c}" ${r.country === c ? "selected" : ""}>${esc(n)}</option>`).join("")}</select></div>
    <div class="field"><label>Postal code</label><input class="input input-sm mono" data-rp="${i}" value="${esc(r.postal || "")}" placeholder="e.g. 10001"></div>
    <button type="button" class="btn btn-ghost btn-sm btn-icon btn-danger" data-rrm="${i}" aria-label="Remove region">${icon("trash")}</button></div>`).join("");
}
async function loadBackups() {
  const el = $("#bkList", root);
  if (!el) return;
  try {
    const r = await api.backups();
    const tz = $("#bkTz", root); if (tz) tz.textContent = `Container time now: ${r.now} ${r.timezone || ""}`;
    el.innerHTML = r.backups.length ? r.backups.map((b) => `<div class="line-row bk-row">
      <div><div class="mono small">${esc(b.created)}</div><div class="faint small">${esc(b.kind)}${b.channels != null ? ` · ${b.channels} channels · ${b.providers} providers` : ""}</div></div>
      <button class="btn btn-sm" data-bkrestore="${esc(b.name)}">Restore</button>
      <a class="btn btn-sm btn-ghost" href="/api/backups/${encodeURIComponent(b.name)}">${icon("download")}Download</a>
      <button class="btn btn-sm btn-ghost btn-danger" data-bkdel="${esc(b.name)}">${icon("trash")}</button></div>`).join("")
      : `<p class="faint small">No backups yet. Press Back up now, or wait for the daily one.</p>`;
  } catch (e) { el.innerHTML = `<p class="faint small">${esc(e.message)}</p>`; }
}
function wireExtra() {
  const form = $("#sForm", root);
  form.addEventListener("input", (e) => {
    const t = e.target;
    if (t.dataset.rc != null) {
      const i = Number(t.dataset.rc);
      draft.regions[i].country = t.value;
      const def = COUNTRIES.find((c) => c[0] === t.value);
      const p = $(`[data-rp="${i}"]`, root);
      if (def && p && !p.value) { p.value = def[2]; draft.regions[i].postal = def[2]; }
    } else if (t.dataset.rp != null) {
      draft.regions[Number(t.dataset.rp)].postal = t.value.trim();
    } else if (t.dataset.bk) {
      draft.backups[t.dataset.bk] = t.type === "checkbox" ? t.checked : t.type === "number" ? Number(t.value) : t.value;
    }
  });
  form.addEventListener("click", async (e) => {
    if (e.target.closest("[data-radd]")) { draft.regions.push({ country: "USA", postal: "" }); $("#rgList", root).innerHTML = regionsHtml(); markDirty(); return; }
    const rm = e.target.closest("[data-rrm]");
    if (rm) { draft.regions.splice(Number(rm.dataset.rrm), 1); $("#rgList", root).innerHTML = regionsHtml(); markDirty(); return; }
    const now = e.target.closest("[data-bknow]");
    if (now) {
      busy(now, async () => { try { const b = await api.backupNow(); toast("Backup saved", b.name, "ok"); loadBackups(); } catch (err) { toastError("Backup failed", err); } });
      return;
    }
    const rs = e.target.closest("[data-bkrestore]");
    if (rs) {
      const name = rs.dataset.bkrestore;
      const ok = await confirmDialog({ title: "Restore this backup?", message: `coax replaces its current settings, providers, channels and lineups with ${name}. The current state is backed up first, so you can undo this.`, confirm: "Restore", danger: true });
      if (!ok) return;
      try { await api.backupRestore(name); toast("Restored", name, "ok"); setTimeout(() => location.reload(), 700); } catch (err) { toastError("Restore failed", err); }
      return;
    }
    const del = e.target.closest("[data-bkdel]");
    if (del) {
      const ok = await confirmDialog({ title: "Delete this backup?", message: del.dataset.bkdel, confirm: "Delete", danger: true });
      if (!ok) return;
      try { await api.backupDelete(del.dataset.bkdel); loadBackups(); } catch (err) { toastError("Delete failed", err); }
    }
  });
  loadBackups();
}
