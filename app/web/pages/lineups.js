// Lineups: named profiles, each its own DVR custom source with its own M3U and XMLTV URLs and guide source.
import { api, routes } from "../api.js";
import { $, $$, esc, icon, toast, toastError, openModal, confirmDialog, emptyState, skeletonCards, busy, copyText, fmtInt, fmtAgo } from "../ui.js";
import { invalidate, loadConfig } from "../state.js";

let root, lineups = [], channels = [], base = "";
let dvr = {}; // lineup id -> GET /api/lineups/<id>/dvr result (or {error})

// Guide options for the Channels DVR source.
const DVR_GUIDES = [
  ["gracenote", "Gracenote only", "Station IDs from the playlist. No XMLTV file."],
  ["xmltv", "XMLTV only", "The DVR ignores station IDs and reads this lineup's XMLTV file."],
  ["gracenote_xmltv", "Gracenote preferred, XMLTV fallback", "Station IDs where a channel has one, XMLTV for the rest."],
];

const GUIDES = [
  ["gracenote", "Gracenote", "Station IDs in the M3U. The DVR fetches the guide itself."],
  ["xmltv", "XMLTV", "No station IDs. Guide comes from this lineup's XMLTV file."],
  ["both", "Both", "Station IDs where a channel has one, XMLTV for the rest."],
  ["per-channel", "Per channel", "Each channel's own guide mode decides."],
];
const guideBadge = (g) => ({ gracenote: "badge-accent", xmltv: "badge-info", both: "badge-copper" }[g] || "");

export async function mount(el) {
  root = el;
  root.innerHTML = `<div class="page-head"><div><div class="eyebrow">Outputs</div><h1>Lineups</h1>
    <p class="sub">Each lineup is added to your DVR as its own custom source, with its own playlist and guide.</p></div>
    <div class="page-actions"><button class="btn btn-primary" data-act="add">${icon("plus")}New lineup</button></div></div>
    <div class="grid grid-auto" id="lList">${skeletonCards(2, 220)}</div>`;
  root.onclick = onClick;
  await load();
}
export function unmount() {}

async function load() {
  try {
    const [l, c, cfg] = await Promise.all([api.listLineups(), api.listChannels().catch(() => []), loadConfig(true).catch(() => null)]);
    lineups = l; channels = c;
    base = (cfg?.server?.base_url || location.origin).replace(/\/+$/, "");
  } catch (e) { toastError("Could not load lineups", e); lineups = []; }
  render();
  loadDvr();
}

// Channels DVR status per lineup, loaded after the cards so a slow DVR never blocks the page.
async function loadDvr() {
  await Promise.all(lineups.map(async (l) => {
    try { dvr[l.id] = await api.lineupDvr(l.id); } catch (e) { dvr[l.id] = { error: e.message }; }
    const slot = $(`[data-id="${CSS.escape(l.id)}"] [data-dvr]`, root);
    if (slot) slot.innerHTML = dvrBlock(l);
    const badge = $(`[data-id="${CSS.escape(l.id)}"] [data-dvr-badge]`, root);
    if (badge) badge.innerHTML = dvr[l.id]?.in_dvr ? `<span class="badge badge-ok"><span class="dot"></span>In Channels DVR</span>` : "";
  }));
}

function dvrBlock(l) {
  const d = dvr[l.id];
  if (!d) return `<span class="skel" style="width:60%;height:32px"></span>`;
  if (d.dvr_url_set === false || d.base_url_set === false) {
    const missing = d.dvr_url_set === false ? "Channels DVR URL" : "coax base URL";
    return `<div class="row" style="justify-content:space-between;gap:12px"><p class="faint small">Set the ${missing} in <a href="#/settings">Settings</a> to add this lineup with one click.</p>
      <button class="btn btn-primary btn-sm" disabled>${icon("plug")}Add to Channels DVR</button></div>`;
  }
  const sync = d.last_sync;
  const syncText = sync
    ? `Last synced with Channels DVR: ${esc(fmtAgo(sync.at))}${sync.ok ? "" : ` <span class="badge badge-bad" title="${esc(sync.error)}">failed</span>`}`
    : d.dvr?.added_at ? `Added to Channels DVR ${esc(fmtAgo(d.dvr.added_at))}` : "";
  const err = d.error && !d.in_dvr ? `<p class="faint small" style="color:var(--bad)">${icon("alert", 'style="width:13px;height:13px;vertical-align:-2px"')} ${esc(d.error)}</p>` : "";
  return `${err}<div class="row" style="justify-content:space-between;gap:12px;flex-wrap:wrap">
    <span class="faint small">${syncText}</span>
    <div class="row" style="flex-wrap:wrap;gap:8px">${d.in_dvr || d.dvr ? `<button class="btn btn-ghost btn-sm" data-act="dvr-sync">${icon("refresh")}Sync now</button>` : ""}
    <button class="btn btn-primary btn-sm" data-act="dvr">${icon("plug")}${d.in_dvr ? "Update in Channels DVR" : "Add to Channels DVR"}</button></div></div>`;
}

function render() {
  const list = $("#lList", root);
  if (!lineups.length) {
    list.style.gridTemplateColumns = "1fr";
    list.innerHTML = `<div class="card">${emptyState({ art: "tv", title: "No lineups yet", text: "Create a lineup, put channels in it, then add its M3U URL to your DVR as a custom source.", actions: `<button class="btn btn-primary" data-act="add">${icon("plus")}New lineup</button>` })}</div>`;
    return;
  }
  list.style.gridTemplateColumns = "";
  list.innerHTML = lineups.map((l) => {
    const m3u = base + routes.outputM3u(l.id), xml = base + routes.outputXmltv(l.id);
    const n = (l.channels || []).length;
    const enabled = channels.filter((c) => (l.channels || []).includes(c.id) && c.enabled !== false).length;
    const g = l.guide || "per-channel";
    const hint = g === "xmltv"
      ? `In the DVR add a custom M3U source with the playlist URL, and set its guide to the XMLTV URL.`
      : g === "gracenote"
        ? `In the DVR add a custom M3U source with the playlist URL. Station IDs map the guide automatically.`
        : `Add a custom M3U source with the playlist URL, and set its XMLTV guide URL for channels without a station ID.`;
    return `<article class="card lineup-card" data-id="${esc(l.id)}">
      <div class="card-head lineup-head"><div class="lineup-title"><h3 class="ellip" data-pop="${esc(l.name)}">${esc(l.name)}</h3><div class="row" style="gap:6px;flex-wrap:wrap"><span class="badge ${guideBadge(g)}">${esc(g)}</span><span data-dvr-badge>${dvr[l.id]?.in_dvr ? `<span class="badge badge-ok"><span class="dot"></span>In Channels DVR</span>` : ""}</span></div></div>
        <div class="row"><button class="btn btn-ghost btn-sm btn-icon" data-act="edit" aria-label="Edit ${esc(l.name)}">${icon("edit")}</button><button class="btn btn-ghost btn-sm btn-icon btn-danger" data-act="delete" aria-label="Delete ${esc(l.name)}">${icon("trash")}</button></div></div>
      <div class="card-body stack" style="gap:12px">
        <div class="row" style="gap:24px"><div><div class="stat-label">Channels</div><div class="stat-value">${fmtInt(n)}</div></div><div><div class="stat-label">Enabled</div><div class="stat-value">${fmtInt(enabled)}</div></div><div><div class="stat-label">Id</div><div class="stat-value" style="font-size:14px;margin-top:5px">${esc(l.id)}</div></div></div>
        <div class="copy-field"><span class="tag">M3U</span><code title="${esc(m3u)}">${esc(m3u)}</code><button class="btn btn-ghost btn-sm btn-icon" data-copy="${esc(m3u)}" aria-label="Copy M3U URL">${icon("copy")}</button></div>
        <div class="copy-field"><span class="tag">XMLTV</span><code title="${esc(xml)}">${esc(xml)}</code><button class="btn btn-ghost btn-sm btn-icon" data-copy="${esc(xml)}" aria-label="Copy XMLTV URL">${icon("copy")}</button></div>
        <p class="faint small">${icon("info", 'style="width:13px;height:13px;vertical-align:-2px"')} ${hint}</p>
        <div class="stack" style="gap:8px;border-top:1px solid var(--border);padding-top:12px" data-dvr>${dvrBlock(l)}</div>
      </div></article>`;
  }).join("");
}

async function onClick(e) {
  const cp = e.target.closest("[data-copy]");
  if (cp) { copyText(cp.dataset.copy); return; }
  const a = e.target.closest("[data-act]");
  if (!a) return;
  const l = lineups.find((x) => x.id === a.closest("[data-id]")?.dataset.id);
  if (a.dataset.act === "add") editModal(null);
  else if (a.dataset.act === "dvr") dvrModal(l);
  else if (a.dataset.act === "dvr-sync") {
    await busy(a, async () => {
      try {
        const r = await api.syncLineupDvr(l.id);
        toast("Synced with Channels DVR", r?.guide_started ? "Channels reloaded and guide download started." : "Channels reloaded.", "ok");
      } catch (err) { toastError("Sync failed", err); }
    });
    await loadDvr();
  }
  else if (a.dataset.act === "edit") editModal(l);
  else if (a.dataset.act === "delete") {
    if (!(await confirmDialog({ title: `Delete ${l.name}?`, message: "Channels stay; only this lineup and its URLs go away. Remove the source from your DVR too.", confirm: "Delete", danger: true }))) return;
    try { await api.deleteLineup(l.id); invalidate("lineups"); toast("Lineup deleted", l.name, "ok"); await load(); } catch (err) { toastError("Delete failed", err); }
  }
}

function editModal(l) {
  let guide = l?.guide || "gracenote";
  const m = openModal({
    title: l ? `Edit ${l.name}` : "New lineup",
    body: `<div class="stack">
      <div class="field"><label for="lName">Name</label><input class="input" id="lName" value="${esc(l?.name || "")}" placeholder="Cable, Sports, NFL…" autofocus></div>
      <div class="field"><span class="label">Guide source</span><div class="preset-grid" style="grid-template-columns:repeat(2,1fr)">${GUIDES.map(([v, n, d]) => `<label class="preset"><input type="radio" name="lg" value="${v}" ${guide === v ? "checked" : ""}><span class="p-name">${n}</span><span class="p-desc">${d}</span></label>`).join("")}</div></div>
      <div class="field"><label for="lSort">Channel order in Channels DVR</label><select class="input" id="lSort">
        <option value="manual" ${(l?.sort || "manual") === "manual" ? "selected" : ""}>Coax order (as arranged on the Channels page)</option>
        <option value="provider" ${l?.sort === "provider" ? "selected" : ""}>Provider channel number</option>
        <option value="alpha" ${l?.sort === "alpha" ? "selected" : ""}>Alphabetical</option></select>
        <span class="hint">Matters when Channels DVR numbers the source itself; auto-number for this lineup follows it too.</span></div>
      <div class="field"><label for="lStart">Start numbering at <span class="faint">(optional)</span></label><input class="input mono" id="lStart" inputmode="numeric" value="${esc(l?.start_number ?? "")}" placeholder="Use each channel's own number">
        <span class="hint">Numbers this lineup's channels from here, in the order above (e.g. A to Z from 1000). Only affects this lineup.</span></div>
      ${l ? "" : `<label class="switch"><input type="checkbox" id="lAll"><span class="track"></span><span>Start with every channel</span></label>`}
      <p class="faint small">Add channels to lineups from the Channels page (the Lineups column or a bulk selection).</p></div>`,
    foot: `<button class="btn" data-close>Cancel</button><button class="btn btn-primary" data-save>${l ? "Save" : "Create lineup"}</button>`,
  });
  $$('input[name="lg"]', m.el).forEach((r) => (r.onchange = () => (guide = r.value)));
  $("[data-save]", m.el).onclick = (e) => busy(e.currentTarget, async () => {
    const name = $("#lName", m.el).value.trim();
    if (!name) { toast("Name is required", "", "warn"); return; }
    try {
      const sort = $("#lSort", m.el).value;
      const startRaw = $("#lStart", m.el).value.trim();
      if (startRaw && !/^\d+$/.test(startRaw)) { toast("Start number must be a whole number", "", "warn"); return; }
      const start_number = startRaw ? Number(startRaw) : null;
      if (l) await api.updateLineup(l.id, { name, guide, sort, start_number });
      else await api.createLineup({ name, guide, sort, start_number, all_channels: $("#lAll", m.el).checked });
      invalidate("lineups");
      m.close();
      toast(l ? "Lineup saved" : "Lineup created", name, "ok");
      await load();
    } catch (err) { toastError("Could not save lineup", err); }
  });
}

// Add (or update) the lineup as a custom M3U source in Channels DVR.
function dvrModal(l) {
  const d = dvr[l.id] || {};
  const def = d.defaults || {};
  const src = d.source || {};
  let guide = def.guide || "gracenote_xmltv";
  const numbering = (d.dvr?.numbering ?? src.numbering ?? def.numbering) || "";
  const start = d.dvr?.start_number || src.start_number || def.start_number || "";
  const logos = d.dvr?.logos ?? src.logos ?? def.logos ?? "guide";
  const radio = (name, value, checked, title, desc) => `<label class="preset"><input type="radio" name="${name}" value="${esc(value)}" ${checked ? "checked" : ""}><span class="p-name">${esc(title)}</span><span class="p-desc">${esc(desc)}</span></label>`;
  const m = openModal({
    title: d.in_dvr ? `Update ${l.name} in Channels DVR` : `Add ${l.name} to Channels DVR`,
    body: `<div class="stack">
      <div class="field"><span class="label">Guide data</span><div class="preset-grid" style="grid-template-columns:1fr">${DVR_GUIDES.map(([v, n, desc]) => radio("dg", v, guide === v, n, desc)).join("")}</div></div>
      <div class="field"><label for="dLimit">Stream limit</label><input class="input" id="dLimit" inputmode="numeric" value="${esc(def.limit ?? "")}" placeholder="No limit">
        <span class="hint">How many channels the DVR may tune at once. ${def.default_limit ? `Your providers allow ${esc(def.default_limit)}.` : "Your providers set no limit."}</span></div>
      <div class="field"><span class="label">Channel numbers</span><div class="preset-grid" style="grid-template-columns:repeat(2,1fr)">
        ${radio("dn", "", numbering !== "ignore", "Use coax numbers", "The numbers you set on the Channels page.")}
        ${radio("dn", "ignore", numbering === "ignore", "Ignore playlist numbers", "The DVR numbers channels from a start number.")}</div>
        <div class="row" id="dStartRow" style="gap:8px;${numbering === "ignore" ? "" : "display:none"}"><label for="dStart" class="small">Start at</label><input class="input" id="dStart" inputmode="numeric" style="max-width:140px" value="${esc(start || "1000")}"></div></div>
      <div class="field"><span class="label">Channel logos</span><div class="preset-grid" style="grid-template-columns:repeat(2,1fr)">
        ${radio("dl", "guide", logos === "guide", "Use guide logos", "Logos from the guide data.")}
        ${radio("dl", "", logos !== "guide", "Use playlist logos", "The logos set in coax.")}</div></div>
      <div class="field"><label for="dName">Source name</label><input class="input" id="dName" value="${esc(def.name || "coax " + l.name)}"><span class="hint">Shown in the DVR's Sources list. The source key is ${esc(d.key || "")}.</span></div>
      <p class="faint small">coax reloads this source in the DVR automatically whenever you change its channels.</p></div>`,
    foot: `<button class="btn" data-close>Cancel</button><button class="btn btn-primary" data-save>${icon("plug")}${d.in_dvr ? "Update source" : "Add to Channels DVR"}</button>`,
  });
  $$('input[name="dg"]', m.el).forEach((r) => (r.onchange = () => (guide = r.value)));
  $$('input[name="dn"]', m.el).forEach((r) => (r.onchange = () => { $("#dStartRow", m.el).style.display = r.value === "ignore" ? "" : "none"; }));
  $("[data-save]", m.el).onclick = (e) => busy(e.currentTarget, async () => {
    const num = $('input[name="dn"]:checked', m.el).value;
    const opts = {
      guide,
      limit: $("#dLimit", m.el).value.trim(),
      numbering: num,
      start_number: num === "ignore" ? $("#dStart", m.el).value.trim() : "",
      logos: $('input[name="dl"]:checked', m.el).value,
      name: $("#dName", m.el).value.trim(),
    };
    if (opts.limit && !/^[1-9]\d*$/.test(opts.limit)) { toast("Stream limit must be a whole number", "", "warn"); return; }
    if (num === "ignore" && !/^\d+$/.test(opts.start_number)) { toast("Start number must be a whole number", "", "warn"); return; }
    try {
      const r = await api.addLineupToDvr(l.id, opts);
      m.close();
      const parts = [r.created ? "Source added" : "Source updated", "channels reloaded", r.guide_started ? "guide download started" : "the DVR will fetch the guide shortly"];
      toast(r.created ? "Added to Channels DVR" : "Updated in Channels DVR", parts.join(", ") + ".", "ok", 6000);
      invalidate("lineups");
      await load();
    } catch (err) { toastError("Channels DVR did not accept the source", err); }
  });
}
