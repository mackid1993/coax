// Channels: the main screen. Catalog on the left, one row per channel with Guide and Backups cells,
// one-click Auto-match that fills pending suggestions, and "Save checked" to save them in one request. Unsaved
// suggestions (checks included) are kept on the server until you Save or Discard them; nothing is applied for you.
import { api, ApiError } from "../api.js";
import { $, $$, esc, icon, toast, toastError, openModal, confirmDialog, logo, emptyState, skeletonRows, debounce, busy, switchHtml, checkHtml, confidenceHtml, fmtInt } from "../ui.js";
import { loadProviders, loadLineups, loadConfig, invalidate, normalizeGroups, normalizePage, providerName, pct, HIGH, LOW } from "../state.js";

const PER_PAGE = 100;

let S; // page state
let root;

function freshState() {
  return {
    channels: [],
    providers: [],
    lineups: [],
    config: null,
    selected: new Set(),
    pending: new Map(), // id -> {guide: {cands, idx, checked, skipped}, backups: [{..., checked}]}
    filter: "all",
    lineup: "",
    group: "",
    sort: "manual",
    q: "",
    cat: { open: true, provider: "", group: "", q: "", offset: 0, total: 0, items: [], selected: new Map(), loading: false, groups: [] },
    loading: true,
  };
}

export async function mount(el) {
  root = el;
  S = freshState();
  try { S.cat.open = localStorage.getItem("coax.catalog") !== "0"; } catch {}
  draft = { loaded: false, rev: 0, job: null, sent: "", busy: false, again: false };
  renderFrame();
  if (await reload()) { await loadDraft(); renderFrame(); }
  offerUnsaved();
  watchAutoMatch();
  loadCatalogGroups().then(() => loadCatalog());
}
export function unmount() { clearTimeout(amTimer); S = null; }

// ---------- Unsaved suggestions ----------
// S.pending is kept on the server as you work, checks included, so a reload, another device or a restart never
// loses it. It is only a holding area: nothing reaches a channel until you press Save. `rev` stops a stale page
// (another tab) from overwriting newer work; on a conflict the page takes the server's copy.
let draft = { loaded: false, rev: 0, job: null, sent: "", busy: false, again: false };
const jsonOf = (m) => JSON.stringify([...m].sort((a, b) => a[0] - b[0]));
const pendingJson = () => jsonOf(S.pending);
async function loadDraft() {
  if (!S?.channelsLoaded) return;   // never filter the server copy against a channel list that failed to load
  let d;
  try { d = await api.suggestions(); } catch (e) { toastError("Could not load unsaved suggestions", e); return; }
  if (!S) return;
  const all = new Map(Object.entries(d.pending || {}).map(([k, v]) => [Number(k), v]));
  S.pending = new Map([...all].filter(([id]) => chById(id)).map(([id, p]) => [id, structuredClone(p)]));
  settleHeld(true);   // loadDraft always follows a reload()
  draft = { loaded: true, rev: d.rev || 0, job: d.job_id || null, sent: jsonOf(all), sentJob: d.job_id || null, busy: false, again: false };
  saveDraft();   // drops suggestions for channels deleted since (a no-op when nothing changed)
}
// Held suggestions that no longer apply: a guide saved since (here or on another device) or "left as is", and
// backups the channel already has. Run after the channel list is (re)loaded; afterReload also drops backups from a
// provider deleted since (S.providers is fresh only right after reload(); the server drops them on delete anyway).
function settleHeld(afterReload = false) {
  for (const [id, p] of [...S.pending]) {
    const c = chById(id);
    if (!c) { S.pending.delete(id); continue; }
    // Only touch what changes, so a page opening never rewrites (and re-versions) an unchanged copy.
    if (p.guide?.cands?.length && (hasGuide(c) || leftAsIs(c))) p.guide = null;
    const own = new Set((c.streams || []).map(refKey));
    // Drop backups the channel already has, and backups from a provider that was deleted (they cannot be saved).
    const gone = (b) => own.has(refKey(b)) || (afterReload && S.providers.length && !S.providers.some((x) => x.id === b.provider));
    if (p.backups?.some(gone)) p.backups = p.backups.filter((b) => !gone(b));
    if (!p.guide?.cands?.length && !p.backups?.length) S.pending.delete(id);
  }
}
function saveDraft() {
  if (!S || !draft.loaded) return;
  const json = pendingJson();
  if (json === draft.sent && draft.job === draft.sentJob) return;
  if (draft.busy) { draft.again = true; return; }
  draft.busy = true;
  const st = S, job = draft.job, d = draft;
  api.saveSuggestions({ rev: draft.rev, job_id: job, pending: Object.fromEntries(S.pending) })
    .then((r) => { if (S !== st || d !== draft) return; d.rev = r.rev; d.sent = json; d.sentJob = job; d.warned = false; })
    .catch(async (e) => {
      if (S !== st) return;
      if (e?.status === 409) {
        // Newer work exists elsewhere: refresh the channels first (new channels from another device must not be
        // treated as deleted), then take the server copy.
        if (await reload()) await loadDraft();
        if (d === draft) {   // could not take the server copy: try again later instead of resending at once
          d.again = false;
          setTimeout(() => { if (S === st && d === draft) saveDraft(); }, 5000);
          return;
        }
        if (S === st) { toast("Suggestions updated", "They were changed in another window; showing the latest.", "info"); renderFrame(); watchAutoMatch(); }
      } else {
        if (!draft.warned) { draft.warned = true; toastError("Could not keep the unsaved suggestions on the server; retrying", e); }
        setTimeout(() => { if (S === st) saveDraft(); }, 5000);
      }
    })
    .finally(() => {
      d.busy = false;
      if (d === draft && d.again && S === st) { d.again = false; saveDraft(); }
    });
}

async function reload() {
  let ok = false;
  try {
    const [channels, providers, lineups, config] = await Promise.all([
      api.listChannels(),
      loadProviders(true).catch(() => []),
      loadLineups(true).catch(() => []),
      loadConfig(true).catch(() => null),
    ]);
    if (!S) return;
    S.channels = channels;
    S.providers = providers;
    S.lineups = lineups;
    S.config = config;
    S.channelsLoaded = ok = true;
    if (draft.loaded) settleHeld(true);   // before drawing, so rows and held suggestions always match
  } catch (e) {
    toastError("Could not load channels", e);
  }
  if (!S) return false;
  S.loading = false;
  renderFrame();
  return ok;
}

// ---------- Derived ----------
const chById = (id) => S.channels.find((c) => c.id === id);
const backupsOf = (c) => (c.streams || []).slice(1);
// Backups that can actually play: enabled streams still in the catalog, after the first one.
const liveBackups = (c) => (c.streams || []).filter((s) => !s.disabled && !s.missing).slice(1);
// Unsaved Auto-match suggestions on this channel (a guide station or backup streams), sure ones included.
const hasSuggestions = (c) => { const p = S.pending.get(c.id); return !!(p && (p.guide?.cands?.length || p.backups?.length)); };
const leftAsIs = (c) => !!(c.guide || {}).reviewed;
// Which guide a channel needs, from its lineups: a Gracenote-only lineup needs a station ID,
// an XMLTV-only lineup needs an XMLTV ID, anything else is happy with either.
function guideNeed(c) {
  const ls = lineupsOf(c).map((id) => S.lineups.find((l) => l.id === id)).filter(Boolean);
  if (ls.length && ls.every((l) => l.guide === "gracenote")) return "gracenote";
  if (ls.length && ls.every((l) => l.guide === "xmltv")) return "xmltv";
  return "either";
}
// A usable guide for the lineups this channel is in.
const hasGuide = (c) => {
  const g = c.guide || {};
  const need = guideNeed(c);
  if (need === "gracenote") return !!g.station_id;
  if (need === "xmltv") return !!g.xmltv_id;
  // Otherwise the channel's own mode decides: a provider tvg-id beside an unmatched Gracenote channel is only
  // a hint for Auto-match, not a guide.
  return g.mode === "gracenote" ? !!g.station_id : g.mode === "xmltv" ? !!g.xmltv_id : false;
};
// Like the original matcher: Matched (sure, pre-checked) needs nothing; Review (only weaker candidates)
// and Unmatched (nothing found) need you. A guide you saved yourself is never second-guessed.
function guideStatus(c) {
  if (hasGuide(c)) return "ok";
  const p = S.pending.get(c.id);
  const st = p?.guide?.status;
  if (p?.guide?.cands?.length) {
    if (st) return st === "matched" ? "matched" : "review";
    return pct(p.guide.cands[p.guide.idx]?.score ?? p.guide.cands[p.guide.idx]?.confidence) >= HIGH ? "matched" : "review";
  }
  if (hasGuide(c)) return "ok";
  return "unmatched";
}
// Unmatched = no saved guide, unless you left it as is (a suggestion is not a guide until it is saved).
const unmatched = (c) => !hasGuide(c) && !leftAsIs(c);
// Needs attention = suggestions to review, or no guide. Missing backups have their own filter.
const needsAttention = (c) => hasSuggestions(c) || unmatched(c);
function lineupsOf(c) {
  if (Array.isArray(c.lineups)) return c.lineups;
  return S.lineups.filter((l) => (l.channels || []).includes(c.id)).map((l) => l.id);
}
// Gracenote is skipped when every lineup the channel belongs to takes its guide from XMLTV.
function xmltvOnly(c) {
  const ls = lineupsOf(c).map((id) => S.lineups.find((l) => l.id === id)).filter(Boolean);
  return ls.length > 0 && ls.every((l) => l.guide === "xmltv");
}
// The lineup and group pickers set the scope; the counters count within it.
const inScope = (c) => (!S.lineup || lineupsOf(c).includes(S.lineup)) && (!S.group || (c.group || "") === S.group);
function visibleChannels() {
  const q = S.q.trim().toLowerCase();
  const words = q ? q.split(/\s+/) : [];
  return S.channels.filter((c) => {
    if (!inScope(c)) return false;
    if (S.filter === "attention" && !needsAttention(c)) return false;
    if (S.filter === "unmatched" && !unmatched(c)) return false;
    if (S.filter === "nobackups" && liveBackups(c).length) return false;
    if (S.filter === "review" && !hasSuggestions(c)) return false;
    if (S.filter === "left" && !leftAsIs(c)) return false;
    if (S.filter === "missing" && !c.missing_streams) return false;
    if (words.length) {
      const hay = `${c.number} ${c.name} ${c.group} ${c.guide?.station_id || ""} ${c.guide?.xmltv_id || ""}`.toLowerCase();
      if (!words.every((w) => hay.includes(w))) return false;
    }
    return true;
  }).sort(sorter(S.sort));
}
function sorter(mode) {
  const num = (v) => { const n = parseFloat(v); return Number.isFinite(n) ? n : Infinity; };
  const byName = (a, b) => String(a.name || "").localeCompare(String(b.name || ""), undefined, { sensitivity: "base", numeric: true });
  if (mode === "alpha") return byName;
  if (mode === "provider") return (a, b) => (num(a.provider_num) - num(b.provider_num)) || byName(a, b);
  return () => 0;
}
function counters() {
  const all = S.channels.filter(inScope);
  return {
    total: all.length,
    enabled: all.filter((c) => c.enabled !== false).length,
    guide: all.filter(hasGuide).length,
    backups: all.filter((c) => liveBackups(c).length).length,
    missing: all.filter((c) => c.missing_streams).length,
    review: all.filter(hasSuggestions).length,
    checked: pendingCheckedCount(),   // Save applies every checked suggestion, whatever the scope
  };
}
function pendingCheckedCount() {
  let n = 0;
  for (const p of S.pending.values()) {
    if (p.guide && p.guide.checked && p.guide.cands?.length) n++;
    n += (p.backups || []).filter((b) => b.checked).length;
  }
  return n;
}

// ---------- Frame ----------
function renderFrame() {
  if (!S) return;
  normalizeFilters();
  saveDraft();
  // Rebuilding the page resets scroll; keep the table and window where they were (e.g. after a save).
  const tw = $(".table-wrap", root);
  const keep = { table: tw ? tw.scrollTop : 0, win: window.scrollY };
  renderFrameInner();
  const tw2 = $(".table-wrap", root);
  if (tw2) tw2.scrollTop = keep.table;
  window.scrollTo(0, keep.win);
}
// A filter whose rows are all gone falls back to All, and a group that no longer exists is cleared, so the page
// never sits on an empty, unhighlighted filter.
function normalizeFilters() {
  if (S.filter === "review" && !S.channels.some((c) => inScope(c) && hasSuggestions(c))) S.filter = "all";
  if (S.filter === "missing" && !S.channels.some((c) => inScope(c) && c.missing_streams)) S.filter = "all";
  if (S.group && !S.channels.some((c) => (c.group || "") === S.group)) S.group = "";
  if (S.lineup && !S.lineups.some((l) => l.id === S.lineup)) S.lineup = "";
}
function renderFrameInner() {
  const k = counters();
  const lineupOpts = S.lineups.map((l) => `<option value="${esc(l.id)}" ${S.lineup === l.id ? "selected" : ""}>${esc(l.name)}</option>`).join("");
  root.innerHTML = `
  <div class="page-head">
    <div>
      <div class="eyebrow">Channel setup</div>
      <h1>Channels</h1>
      <p class="sub">Pick streams, match the guide and attach backups in one pass.</p>
    </div>
    <div class="page-actions">
      <button class="btn" data-act="toggle-catalog" aria-pressed="${S.cat.open}">${icon("layers")}${S.cat.open ? "Hide catalog" : "Show catalog"}</button>
      <button class="btn" data-act="autonumber">${icon("hash")}Auto-number</button>
      <button class="btn btn-primary" data-act="automatch" title="Suggests a guide match (Gracenote) and backup streams (similar channels from your providers) for the selected rows, or all visible">${icon("wand")}Auto-match</button>
    </div>
  </div>
  ${S.config && !(S.config.server?.base_url || "").trim() ? `<div class="callout" style="margin-bottom:14px">${icon("alert")}<div><b>Set the Base URL first.</b> Channels DVR needs it to reach coax. <a href="#/settings">Open Settings</a></div></div>` : ""}
  ${countersHtml(k)}
  <div class="setup ${S.cat.open ? "with-catalog" : ""}">
    ${S.cat.open ? `<section class="card catalog" aria-label="Catalog">${catalogHtml()}</section>` : ""}
    <section class="card channels-card" aria-label="Channels">
      <div class="toolbar">
        <label class="search" style="flex:1 1 180px"><span class="sr-only">Filter channels</span>${icon("search")}<input class="input input-sm" data-search data-ch-q placeholder="Filter channels" value="${esc(S.q)}"><kbd>/</kbd></label>
        <div class="seg" role="group" aria-label="Filter">
          ${segBtn("all", "All")}${segBtn("attention", "Needs attention")}${segBtn("unmatched", "Unmatched guide")}${segBtn("nobackups", "No backups")}${segBtn("review", "To review")}${segBtn("left", "Left as is")}
        </div>
        <span class="spacer"></span>
        <label class="sr-only" for="lineupFilter">Lineup</label>
        <select class="input input-sm" id="lineupFilter" style="width:auto;min-width:130px">${`<option value="">All lineups</option>${lineupOpts}`}</select>
        <label class="sr-only" for="sortSel">Order</label>
        <select class="input input-sm" id="sortSel" style="width:auto" title="Display order">
          <option value="manual" ${S.sort === "manual" ? "selected" : ""}>Coax order</option>
          <option value="provider" ${S.sort === "provider" ? "selected" : ""}>Provider number</option>
          <option value="alpha" ${S.sort === "alpha" ? "selected" : ""}>A to Z</option></select>
        <label class="sr-only" for="groupFilter">Group</label>
        <select class="input input-sm" id="groupFilter" style="width:auto;min-width:130px;max-width:220px">${groupOpts()}</select>
      </div>
      ${S.pending.size ? pendingBar() : ""}
      ${S.selected.size ? selectionBar() : ""}
      <div class="table-wrap ch-wrap">
        <table class="table ch-table">
          <thead><tr>
            <th class="w-check">${checkHtml(false, 'data-check-all aria-label="Select all visible"')}</th>
            <th style="width:28px"><span class="sr-only">Order</span></th>
            <th class="w-num">No.</th>
            <th>Channel</th>
            <th style="width:64px">On</th>
            <th style="min-width:170px">Guide</th>
            <th style="min-width:190px">Backups</th>
            <th class="w-act"><span class="sr-only">Actions</span></th>
          </tr></thead>
          <tbody id="chBody">${S.loading ? skeletonRows(10, 8) : ""}</tbody>
        </table>
        <div id="chEmpty"></div>
      </div>
    </section>
  </div>`;
  if (!S.loading) renderRows();
  wireFrame();
}
function countersHtml(k) {
  return `<div class="counters ${k.missing ? "five" : ""}">
    ${counter("Channels", k.total, `${fmtInt(k.enabled)} enabled`)}
    ${counter("Guide matched", k.guide, pctOf(k.guide, k.total), "unmatched")}
    ${counter("With backups", k.backups, pctOf(k.backups, k.total), "nobackups")}
    ${counter("To review", k.review, k.review ? `${fmtInt(k.checked)} checked, not saved yet` : "run Auto-match", k.review ? "review" : "")}
    ${k.missing ? counter("Vanished streams", k.missing, "flagged by refresh", "missing", "bad") : ""}
  </div>`;
}
const pctOf = (a, b) => (b ? `${Math.round((a / b) * 100)}% of channels` : "nothing yet");
function counter(label, value, sub, filter = "", tone = "") {
  return `<button class="card counter ${tone} ${filter && S.filter === filter ? "active" : ""}" ${filter ? `data-filter="${filter}"` : "disabled"}>
    <span class="stat-label">${esc(label)}</span><span class="stat-value">${fmtInt(value)}</span><span class="faint small">${esc(sub)}</span></button>`;
}
const segBtn = (id, label) => `<button type="button" data-filter="${id}" aria-pressed="${S.filter === id}">${esc(label)}</button>`;

function amSummary() {
  let sure = 0, review = 0, backups = 0;
  for (const p of S.pending.values()) {
    if (p.guide?.cands?.length) (p.guide.status === "matched" || (!p.guide.status && p.guide.checked) ? sure++ : review++);
    backups += (p.backups || []).length;
  }
  return { sure, review, backups };
}
function pendingBar() {
  const n = pendingCheckedCount();
  const a = amSummary();
  return `<div class="selection-bar pending-bar">${icon("wand")}<span>Auto-match found <b>${a.sure}</b> sure guide match${a.sure === 1 ? "" : "es"} (checked), <b>${a.review}</b> to review, and <b>${a.backups}</b> backup stream${a.backups === 1 ? "" : "s"}. Kept until you Save or Discard; nothing is applied until you press Save.</span>
    <span class="spacer"></span>
    <button class="btn btn-sm btn-ghost" data-act="check-high">Check high confidence</button>
    <button class="btn btn-sm btn-ghost" data-act="download-unsaved" title="Save a copy of the checked suggestions to a file">${icon("download")}Download unsaved</button>
    <button class="btn btn-sm btn-ghost" data-act="discard">Discard</button>
    <button class="btn btn-sm btn-primary" data-act="accept" ${n && !accepting ? "" : "disabled"}>${icon("check")}Save ${n} checked</button></div>`;
}
function selectionBar() {
  return `<div class="selection-bar">${S.selected.size} selected<span class="spacer"></span>
    <button class="btn btn-sm btn-ghost" data-act="sel-enable">Enable</button>
    <button class="btn btn-sm btn-ghost" data-act="sel-disable">Disable</button>
    <button class="btn btn-sm btn-ghost" data-act="sel-lineup">${icon("lineups")}Lineups…</button>
    <button class="btn btn-sm btn-ghost btn-danger" data-act="sel-delete">${icon("trash")}Delete</button>
    <button class="btn btn-sm btn-ghost" data-act="sel-clear">Clear</button></div>`;
}

function groupOpts() {
  const counts = new Map();
  for (const c of S.channels) counts.set(c.group || "", (counts.get(c.group || "") || 0) + 1);
  const names = [...counts.keys()].sort((a, b) => a.localeCompare(b));
  return `<option value="">All groups</option>` + names.map((g) =>
    `<option value="${esc(g)}" ${S.group === g ? "selected" : ""}>${esc(g || "(no group)")} (${counts.get(g)})</option>`).join("");
}

// ---------- Rows ----------
function renderRows() {
  const body = $("#chBody", root);
  const empty = $("#chEmpty", root);
  if (!body) return;
  const rows = visibleChannels();
  if (!S.channels.length) {
    body.innerHTML = "";
    empty.innerHTML = emptyState({
      art: "tv",
      title: "No channels yet",
      text: S.providers.length
        ? "Search the catalog on the left, tick the streams you want and press <b>Add selected</b>. Then run <b>Auto-match</b> to fill in the guide and backups."
        : "Add a provider first, choose its groups, then come back here to pick channels.",
      actions: S.providers.length ? (S.cat.open ? "" : `<button class="btn btn-primary" data-act="toggle-catalog">${icon("layers")}Open catalog</button>`) : `<a class="btn btn-primary" href="#/providers">${icon("plus")}Add a provider</a>`,
    });
    return;
  }
  if (!rows.length) {
    pruneSelection(rows);
    body.innerHTML = "";
    empty.innerHTML = emptyState({ art: "signal", title: "Nothing matches", text: "No channel fits these filters.", actions: `<button class="btn" data-act="clear-filters">Clear filters</button>` });
    return;
  }
  empty.innerHTML = "";
  pruneSelection(rows);
  body.innerHTML = rows.map(rowHtml).join("");
  const all = $("[data-check-all]", root);
  if (all) {
    const n = rows.filter((c) => S.selected.has(c.id)).length;
    all.checked = n > 0 && n === rows.length;
    all.indeterminate = n > 0 && n < rows.length;
  }
}
// Bulk actions act on the selection, so a row a filter or search hides is never kept selected.
function pruneSelection(rows) {
  const shown = new Set(rows.map((c) => c.id));
  let gone = false;
  for (const id of [...S.selected]) if (!shown.has(id)) { S.selected.delete(id); gone = true; }
  if (!gone) return;
  const bar = $(".selection-bar:not(.pending-bar)", root);
  if (bar && !S.selected.size) bar.remove();
  else if (bar) bar.firstChild.textContent = `${S.selected.size} selected`;
}
function rowHtml(c) {
  const sel = S.selected.has(c.id);
  const p = S.pending.get(c.id);
  return `<tr data-id="${c.id}" class="${sel ? "selected" : ""} ${c.enabled === false ? "disabled" : ""} ${p ? "has-pending" : ""}">
    <td class="keep">${checkHtml(sel, `data-sel aria-label="Select ${esc(c.name)}"`)}</td>
    <td class="keep">${S.sort !== "manual" ? "" : `<button class="drag-handle" draggable="true" data-drag aria-label="Reorder ${esc(c.name)}, Alt plus arrow keys" title="Drag to reorder (Alt+↑/↓)">${icon("grip")}</button>`}</td>
    <td><input class="input num-input" data-num value="${esc(c.number ?? "")}" aria-label="Channel number for ${esc(c.name)}" inputmode="decimal"></td>
    <td><div class="cell-name">${logo(c.logo, c.name)}<div style="min-width:0"><div class="row" style="gap:4px;flex-wrap:nowrap"><div class="name">${esc(c.name)}</div><button class="btn btn-ghost btn-sm btn-icon edit-ch" data-act="edit" aria-label="Edit name, number, group and logo of ${esc(c.name)}" data-pop="Edit channel" data-pop-sub="Name, number, group and logo" data-pop-always>${icon("edit")}</button></div><div class="meta">${lineupCell(c)}${c.missing_streams ? ` <span class="badge badge-bad" title="A stream vanished from the provider catalog">${icon("alert", 'style="width:12px;height:12px"')}${c.missing_streams} vanished</span>` : ""}</div></div></div></td>
    <td class="keep">${switchHtml("", c.enabled !== false, "", `data-enabled aria-label="Enabled"`)}</td>
    <td>${guideCell(c, p)}</td>
    <td>${backupsCell(c, p)}</td>
    <td class="w-act keep"></td>
  </tr>`;
}

function stationLabel(g) {
  const call = g.call_sign || g.station_name || "";
  return call ? `${esc(call)} <span class="faint num">${esc(g.station_id)}</span>` : `<span class="num">#${esc(g.station_id)}</span>`;
}
function guideCell(c, p) {
  if (p?.guide?.skipped) {
    return `${currentGuide(c)}<div class="faint small">XMLTV lineup: Gracenote skipped</div>`;
  }
  if (p?.guide?.cands?.length) {
    const cand = p.guide.cands[p.guide.idx];
    const name = cand.call_sign || cand.name || cand.station_id;
    return `<div class="sugg ${p.guide.checked ? "on" : ""}">
      ${checkHtml(p.guide.checked, `data-gcheck aria-label="Accept guide suggestion ${esc(name)}"`)}
      <button class="sugg-body" data-act="guide" title="${esc(cand.reason || "")}">
        <span class="sugg-title">${esc(name)} <span class="faint num">${esc(cand.station_id)}</span></span>
        <span class="row" style="gap:6px">${confidenceHtml(cand.score ?? cand.confidence)}${p.guide.cands.length > 1 ? `<span class="faint small">+${p.guide.cands.length - 1} more</span>` : ""}</span>
      </button></div>`;
  }
  if (p && p.guide && !p.guide.cands?.length && !hasGuide(c)) return `${currentGuide(c)}<div class="faint small">No confident match</div>`;
  return currentGuide(c);
}
function currentGuide(c) {
  const g = c.guide || {};
  const mode = g.mode || "none";
  let inner;
  if (mode === "gracenote" && g.station_id) {
    inner = `<span class="badge badge-accent">GN</span>${stationLabel(g)}${g.confidence != null ? confidenceHtml(g.confidence) : ""}`;
  } else if (mode === "xmltv" && g.xmltv_id) {
    inner = `<span class="badge badge-info">XMLTV</span><span class="mono small ellip" data-pop="${esc(g.xmltv_id)}" data-pop-sub="XMLTV id">${esc(g.xmltv_id)}</span>`;
  } else {
    inner = `<span class="faint">${mode === "none" ? "No guide" : "Not matched"}</span>`;
  }
  return `<button class="cell-btn" data-act="guide" aria-label="Edit guide for ${esc(c.name)}">${inner}</button>`;
}
function backupsCell(c, p) {
  const b = backupsOf(c);
  const more = b.length > 2 ? `<span class="badge" data-pop="${esc(b.slice(2).map((s) => s.name || s.stream_id).join(" · "))}" data-pop-sub="More backups" data-pop-always>+${b.length - 2}</span>` : "";
  const chips = b.slice(0, 2).map((s, i) => `<span class="bchip ${s.missing ? "missing" : ""}${s.disabled ? " off" : ""}" data-pop="${esc(s.name || s.stream_id)}" data-pop-sub="${esc(`Backup ${i + 1} · ${providerName(S.providers, s.provider)}${s.missing ? " · vanished from the provider" : ""}${s.disabled ? " · disabled" : ""}`)}" data-pop-always><span class="ord">${i + 1}</span><span class="ellip">${esc(s.name || s.stream_id)}</span><span class="prov">${esc(providerName(S.providers, s.provider))}</span></span>`).join("");
  const pend = (p?.backups || []).map((s, i) => `<label class="bchip pending ${s.checked ? "on" : ""}" data-pop="${esc(s.name || s.stream_id)}" data-pop-sub="${esc(`Suggested · ${providerName(S.providers, s.provider)} · ${pct(s.score)}%${s.reason ? " · " + s.reason : ""}`)}" data-pop-always>${checkHtml(s.checked, `data-bcheck="${i}" aria-label="Accept backup ${esc(s.name)}"`)}<span class="ellip">${esc(s.name || s.stream_id)}</span><span class="prov">${esc(providerName(S.providers, s.provider))}</span><span class="num small">${pct(s.score)}%</span></label>`).join("");
  const none = !b.length && !pend ? `<span class="faint">None</span>` : "";
  return `<div class="bchips"><button class="btn btn-sm edit-bk" data-act="streams" aria-label="Edit backup streams for ${esc(c.name)}" data-pop="Edit backups" data-pop-sub="Add, search, reorder or remove backup streams" data-pop-always>${icon("layers")}Backups</button>${chips}${more}${pend}${none}</div>`;
}
function lineupCell(c) {
  const ids = lineupsOf(c);
  const chips = ids.map((id) => {
    const l = S.lineups.find((x) => x.id === id);
    return `<span class="lchip">${esc(l?.name || id)}</span>`;
  }).join("");
  return `<button class="cell-btn lineup-btn" data-act="lineups" aria-label="Lineups for ${esc(c.name)}" title="Lineups (click to change)">${chips || `<span class="lchip none">no lineup</span>`}</button>`;
}

// ---------- Frame wiring ----------
function wireFrame() {
  root.onclick = onClick;
  root.onchange = onChange;
  const q = $("[data-ch-q]", root);
  if (q) q.oninput = debounce(() => { S.q = q.value; renderRows(); }, 120);
  const lf = $("#lineupFilter", root);
  if (lf) lf.onchange = () => { S.lineup = lf.value; renderFrame(); };
  const so = $("#sortSel", root);
  if (so) so.onchange = () => { S.sort = so.value; renderRows(); };   // drag handles show only in Coax order
  const gf = $("#groupFilter", root);
  if (gf) gf.onchange = () => { S.group = gf.value; S.selected.clear(); renderFrame(); };
  wireDrag();
  wireCatalog();
}

async function onClick(e) {
  const t = e.target;
  const f = t.closest("[data-filter]");
  if (f && !f.disabled) { S.filter = S.filter === f.dataset.filter && f.classList.contains("counter") ? "all" : f.dataset.filter; renderFrame(); return; }
  const a = t.closest("[data-act]");
  if (!a) return;
  const tr = a.closest("tr[data-id]");
  const c = tr ? chById(Number(tr.dataset.id)) : null;
  switch (a.dataset.act) {
    case "toggle-catalog":
      S.cat.open = !S.cat.open;
      try { localStorage.setItem("coax.catalog", S.cat.open ? "1" : "0"); } catch {}
      renderFrame();
      if (S.cat.open) loadCatalog();
      break;
    case "clear-filters": S.filter = "all"; S.q = ""; S.lineup = ""; S.group = ""; renderFrame(); break;
    case "autonumber": autoNumberModal(); break;
    case "automatch": autoMatch(); break;
    case "accept": busy(a, acceptChecked); break;
    case "check-high": checkHigh(); break;
    case "download-unsaved": downloadUnsaved(); break;
    case "discard": discardPending(); break;
    case "guide": if (c) guideModal(c); break;
    // The page loads once; take the channel's current streams first, so a Smart Standby promotion or a Disable
    // from the Status card since then is not saved over.
    case "streams": if (c) busy(a, async () => { const st = S; try { Object.assign(c, await api.getChannel(c.id)); } catch {} if (S === st) streamsModal(c); }); break;
    case "lineups": if (c) lineupModal([c.id]); break;
    case "edit": if (c) editModal(c); break;
    case "sel-enable": bulkSet({ enabled: true }); break;
    case "sel-disable": bulkSet({ enabled: false }); break;
    case "sel-lineup": lineupModal([...S.selected]); break;
    case "sel-delete": deleteSelected(); break;
    case "sel-clear": S.selected.clear(); renderFrame(); break;
    case "cat-add": busy(a, addFromCatalog); break;
    case "cat-prev": S.cat.offset = Math.max(0, S.cat.offset - PER_PAGE); loadCatalog(); break;
    case "cat-next": S.cat.offset += PER_PAGE; loadCatalog(); break;
    case "cat-clear": S.cat.selected.clear(); renderCatalogList(); break;
  }
}

async function onChange(e) {
  const t = e.target;
  const tr = t.closest("tr[data-id]");
  const id = tr ? Number(tr.dataset.id) : null;
  if (t.matches("[data-check-all]")) {
    const ids = visibleChannels().map((c) => c.id);
    ids.forEach((x) => (t.checked ? S.selected.add(x) : S.selected.delete(x)));
    renderFrame();
  } else if (t.matches("[data-sel]")) {
    t.checked ? S.selected.add(id) : S.selected.delete(id);
    const hadBar = !!$(".selection-bar:not(.pending-bar)", root);
    if (hadBar !== S.selected.size > 0) renderFrame();
    else { tr.classList.toggle("selected", t.checked); const bar = $(".selection-bar:not(.pending-bar)", root); if (bar) bar.firstChild.textContent = `${S.selected.size} selected`; }
  } else if (t.matches("[data-enabled]")) {
    const c = chById(id);
    try {
      const v = await api.updateChannel(id, { enabled: t.checked });
      Object.assign(c, v && typeof v === "object" ? v : { enabled: t.checked });
      tr.classList.toggle("disabled", !t.checked);
    } catch (err) { t.checked = !t.checked; toastError("Could not update channel", err); }
  } else if (t.matches("[data-num]")) {
    const c = chById(id);
    const v = t.value.trim();
    if (v && !/^\d+(\.\d+)?$/.test(v)) { toast("Invalid number", "Use a number like 101 or 101.1", "warn"); t.value = c.number ?? ""; return; }
    try { const r = await api.updateChannel(id, { number: v }); c.number = r?.number ?? v; t.value = c.number; }
    catch (err) { t.value = c.number ?? ""; toastError("Could not save number", err); }
  } else if (t.matches("[data-gcheck]")) {
    const p = S.pending.get(id); p.guide.checked = t.checked; t.closest(".sugg").classList.toggle("on", t.checked); refreshPendingBar();
  } else if (t.matches("[data-bcheck]")) {
    const p = S.pending.get(id); p.backups[Number(t.dataset.bcheck)].checked = t.checked; t.closest(".bchip").classList.toggle("on", t.checked); refreshPendingBar();
  } else if (t.matches("[data-cat-provider]")) {
    S.cat.provider = t.value; S.cat.group = ""; S.cat.offset = 0; renderCatalogFilters(); loadCatalog();
  } else if (t.matches("[data-cat-group]")) {
    S.cat.group = t.value; S.cat.offset = 0; loadCatalog();
  } else if (t.matches("[data-cat-all]")) {
    for (const s of S.cat.items) { const k = refKey(s); t.checked ? S.cat.selected.set(k, s) : S.cat.selected.delete(k); }
    renderCatalogList();
  }
}
function refreshPendingBar() {
  const bar = $(".pending-bar", root);
  if (bar) bar.outerHTML = pendingBar();
  const cs = $(".counters", root);
  if (cs) cs.outerHTML = countersHtml(counters());
  saveDraft();
}

// ---------- Drag to reorder ----------
function wireDrag() {
  const body = $("#chBody", root);
  if (!body) return;
  let dragId = null;
  body.addEventListener("dragstart", (e) => {
    const h = e.target.closest("[data-drag]");
    if (!h) return;
    const tr = h.closest("tr");
    dragId = Number(tr.dataset.id);
    tr.classList.add("dragging");
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", String(dragId));
    e.dataTransfer.setDragImage(tr, 20, 20);
  });
  body.addEventListener("dragover", (e) => {
    if (dragId == null) return;
    const tr = e.target.closest("tr[data-id]");
    if (!tr) return;
    e.preventDefault();
    $$(".drop-before,.drop-after", body).forEach((x) => x.classList.remove("drop-before", "drop-after"));
    const r = tr.getBoundingClientRect();
    tr.classList.add(e.clientY < r.top + r.height / 2 ? "drop-before" : "drop-after");
  });
  body.addEventListener("dragend", () => {
    dragId = null;
    $$(".dragging,.drop-before,.drop-after", body).forEach((x) => x.classList.remove("dragging", "drop-before", "drop-after"));
  });
  body.addEventListener("drop", (e) => {
    const tr = e.target.closest("tr[data-id]");
    if (!tr || dragId == null) return;
    e.preventDefault();
    const before = tr.classList.contains("drop-before");
    moveChannel(dragId, Number(tr.dataset.id), before);
  });
  body.addEventListener("keydown", (e) => {
    const h = e.target.closest("[data-drag]");
    if (!h || !e.altKey || !["ArrowUp", "ArrowDown"].includes(e.key)) return;
    e.preventDefault();
    const tr = h.closest("tr");
    const sib = e.key === "ArrowUp" ? tr.previousElementSibling : tr.nextElementSibling;
    if (!sib) return;
    moveChannel(Number(tr.dataset.id), Number(sib.dataset.id), e.key === "ArrowUp", true);
  });
}
async function moveChannel(fromId, toId, before, refocus = false) {
  if (fromId === toId) return;
  const prev = S.channels.slice();
  const from = S.channels.findIndex((c) => c.id === fromId);
  const [item] = S.channels.splice(from, 1);
  let to = S.channels.findIndex((c) => c.id === toId);
  if (!before) to += 1;
  S.channels.splice(to, 0, item);
  renderRows();
  if (refocus) $(`tr[data-id="${fromId}"] [data-drag]`, root)?.focus();
  try {
    await api.reorder(S.channels.map((c) => c.id));
  } catch (e) {
    S.channels = prev;
    renderRows();
    toastError("Could not save the new order", e);
  }
}

// ---------- Bulk ----------
async function bulkSet(set) {
  const ids = [...S.selected];
  try {
    await api.bulkUpdate(ids, set);
    ids.forEach((id) => Object.assign(chById(id) || {}, set));
    toast("Updated", `${ids.length} channel${ids.length === 1 ? "" : "s"}`, "ok");
    renderFrame();
  } catch (e) { toastError("Bulk update failed", e); }
}
async function deleteSelected() {
  const ids = [...S.selected];
  const ok = await confirmDialog({ title: `Delete ${ids.length} channel${ids.length === 1 ? "" : "s"}?`, message: "They disappear from every lineup. Channel ids are never reused.", confirm: "Delete", danger: true });
  if (!ok) return;
  try {
    await api.bulkDelete(ids);
    S.channels = S.channels.filter((c) => !S.selected.has(c.id));
    ids.forEach((id) => S.pending.delete(id));
    S.selected.clear();
    toast("Deleted", `${ids.length} channel${ids.length === 1 ? "" : "s"}`, "ok");
    renderFrame();
  } catch (e) { toastError("Delete failed", e); }
}

function autoNumberModal() {
  const sel = S.selected.size;
  const lineupChoices = S.lineups.map((l) => `<option value="${esc(l.id)}" ${S.lineup === l.id ? "selected" : ""}>${esc(l.name)}</option>`).join("");
  const m = openModal({
    title: "Auto-number channels",
    body: `<div class="form-grid">
      <div class="field full"><label for="anLineup">Lineup</label><select class="input" id="anLineup" ${sel ? "disabled" : ""}><option value="">${sel ? "Selected channels" : "Visible channels"}</option>${lineupChoices}</select><span class="hint" id="anShared"></span></div>
      <div class="field"><label for="anStart">Start at</label><input class="input mono" id="anStart" value="${esc(S.channels[0]?.number || "1")}" inputmode="decimal" autofocus></div>
      <div class="field"><label for="anStep">Step</label><input class="input mono" id="anStep" value="1" inputmode="numeric"></div>
      <div class="field full">${switchHtml("", false, "Only enabled channels", 'id="anOnly"')}</div>
      <p class="full faint small">Numbers follow the current order${sel ? ` of the ${sel} selected channels` : S.lineup || S.filter !== "all" || S.q ? " of the visible channels" : ""}.</p>
    </div>`,
    foot: `<button class="btn" data-close>Cancel</button><button class="btn btn-primary" data-ok>${icon("hash")}Renumber</button>`,
  });
  const pickIds = () => {
    if (sel) return visibleChannels().filter((c) => S.selected.has(c.id)).map((c) => c.id);
    const lid = $("#anLineup", m.el).value;
    if (!lid) return visibleChannels().map((c) => c.id);
    const lineup = S.lineups.find((l) => l.id === lid);
    const order = new Map((lineup?.channels || []).map((id, i) => [id, i]));
    return S.channels.filter((c) => order.has(c.id)).sort(sorter(lineup?.sort || "manual")).map((c) => c.id);
  };
  const note = () => {
    const ids = new Set(pickIds());
    const lid = $("#anLineup", m.el).value;
    const shared = S.channels.filter((c) => ids.has(c.id) && lineupsOf(c).some((x) => x !== lid)).length;
    $("#anShared", m.el).textContent = `${ids.size} channels.` + (lid && shared ? ` ${shared} are also in another lineup and will get these numbers there too.` : "");
  };
  $("#anLineup", m.el).onchange = note;
  note();
  $("[data-ok]", m.el).onclick = (e) => busy(e.currentTarget, async () => {
    const ids = pickIds();
    try {
      const r = await api.autonumber({ start: Number($("#anStart", m.el).value) || 1, step: Number($("#anStep", m.el).value) || 1, ids, only_enabled: $("#anOnly", m.el).checked });
      const nums = r?.numbers || {};
      for (const [id, n] of Object.entries(nums)) { const c = chById(Number(id)); if (c) c.number = n; }
      toast("Renumbered", `${Object.keys(nums).length} channels`, "ok");
      m.close();
      renderRows();
    } catch (err) { toastError("Auto-number failed", err); }
  });
}

// ---------- Auto-match ----------
let amTimer = null;
async function autoMatch() {
  const ids = S.selected.size ? [...S.selected] : visibleChannels().map((c) => c.id);
  if (!ids.length) { toast("Nothing to match", "Add channels first.", "warn"); return; }
  try { await api.automatchStart(ids); } catch (e) { toastError("Auto-match failed", e); return; }
  watchAutoMatch();
}
// Polls the server-side job. Runs on mount too, so leaving the page never loses a match in progress.
function watchAutoMatch() {
  clearTimeout(amTimer);
  const tick = async () => {
    if (!S) return;
    let j;
    try { j = await api.automatchJob(false); } catch { amTimer = setTimeout(tick, 2000); return; }
    if (!S) return;
    if (j.state === "running") { drawAmProgress(j); amTimer = setTimeout(tick, 1000); return; }
    drawAmProgress(null);
    if (j.state === "error") { toastError("Auto-match failed", new Error(j.error || "unknown error")); return; }
    // A finished match is applied once: the server copy of the unsaved suggestions remembers which job it holds.
    if (j.state === "done" && !draft.loaded) { await loadDraft(); if (!S) return; if (!draft.loaded) { amTimer = setTimeout(tick, 5000); return; } renderFrame(); }
    if (j.state === "done" && j.id && draft.loaded && draft.job !== j.id) {
      let full;
      try { full = await api.automatchJob(true); } catch (e) { toastError("Auto-match failed", e); return; }
      if (!S || draft.job === j.id) return;
      draft.job = j.id;
      applyAutoMatch(full.ids || [], full.result || {});
    }
  };
  tick();
}
function drawAmProgress(j) {
  let el = $("#amProgress", root);
  if (!j) { el?.remove(); const b = $('[data-act="automatch"]', root); if (b) b.disabled = false; return; }
  const pct = j.total ? Math.min(100, Math.round((j.done / j.total) * 100)) : 0;
  const n = (j.ids || []).length;
  const label = j.phase === "backups" ? "Finding backup streams" : "Matching Gracenote stations";
  if (!el) {
    const host = $(".counters", root);
    if (!host) return;
    host.insertAdjacentHTML("beforebegin", `<div class="card am-progress" id="amProgress" role="status" aria-live="polite">
      <div class="row" style="gap:10px"><span class="spinner"></span><b>Auto-match</b><span class="faint small am-label"></span><span class="spacer"></span><span class="num am-pct"></span></div>
      <div class="am-bar"><span></span></div></div>`);
    el = $("#amProgress", root);
  }
  el.querySelector(".am-label").textContent = `${label} · ${n} channel${n === 1 ? "" : "s"} · runs in the background, you can leave this page`;
  el.querySelector(".am-pct").textContent = `${pct}%`;
  el.querySelector(".am-bar span").style.width = `${pct}%`;
  const b = $('[data-act="automatch"]', root); if (b) b.disabled = true;
}
function applyAutoMatch(ids, res) {
  const results = res?.results || res || {};
  let guideN = 0, backN = 0, guideErr = "", backErr = "";
  for (const id of ids) {
    const r = results[id] || results[String(id)];
    if (!r) continue;
    const c = chById(id);
    if (!c) continue;
    const ownKeys = new Set((c.streams || []).map(refKey));
    // A saved guide, a channel you left as is and an XMLTV-only channel get no guide suggestions.
    const skip = xmltvOnly(c) || hasGuide(c) || leftAsIs(c);
    const cands = skip ? [] : (r.guide || r.guide_candidates || []).filter((x) => x && x.station_id);
    const prev = S.pending.get(id);
    const gErr = !!(r.guide_error || r.guide_status === "error");
    // A failed step keeps what was already held for review instead of wiping it.
    const backups = r.backups_error ? (prev?.backups || []) : (r.backups || []).filter((b) => !ownKeys.has(refKey(b))).map((b) => ({ ...b, checked: pct(b.score) >= HIGH }));
    const status = skip ? "" : (r.guide_status || "");
    const sure = status ? status === "matched" : cands.length > 0 && pct(cands[0].score ?? cands[0].confidence) >= HIGH;
    const guide = gErr && prev?.guide ? prev.guide : { cands, idx: 0, status, checked: cands.length > 0 && sure };
    if (r.guide_error || r.guide_status === "error") guideErr = r.guide_error || r.guide_reason || "Gracenote search failed";
    if (r.backups_error) backErr = r.backups_error;
    if (!guide.cands?.length && !backups.length) { S.pending.delete(id); continue; }
    if (guide.cands?.length) guideN++;
    backN += backups.length;
    S.pending.set(id, { guide, backups });
  }
  if (guideErr || backErr) toast("Auto-match was incomplete", [guideErr && `Guide: ${guideErr}`, backErr && `Backups: ${backErr}`].filter(Boolean).join(". "), "warn", 12000);
  if (!S.pending.size) { if (!guideErr && !backErr) toast("Auto-match finished", "No new suggestions. Everything already looks matched.", "info"); }
  else {
    const a = amSummary();
    toast("Auto-match done", `${a.sure} sure, ${a.review} to review, ${a.backups} backups. They are under To review; press Save to keep the checked ones.`, "ok");
    S.filter = "review";
  }
  renderFrame();
}
function checkHigh() {
  for (const [id, p] of S.pending) {
    const c = chById(id);
    // Only a station the server called a sure match (not a name shared by several stations, not one outside
    // your region), and never on a channel that has a guide or that you left as is.
    if (p.guide?.cands?.length) {
      const cand = p.guide.cands[p.guide.idx];
      p.guide.checked = !!c && !hasGuide(c) && !leftAsIs(c) && (p.guide.status ? p.guide.status === "matched" && p.guide.idx === 0 : pct(cand.score ?? cand.confidence) >= HIGH);
    }
    (p.backups || []).forEach((b) => (b.checked = pct(b.score) >= HIGH));
  }
  renderFrame();
}
function guideFromCandidate(c, cand) {
  // A guide is one source: a Gracenote station replaces the XMLTV id.
  return { ...(c.guide || {}), mode: "gracenote", xmltv_id: "", xmltv_provider: "", station_id: String(cand.station_id), call_sign: cand.call_sign || "", station_name: cand.name || "", confidence: cand.score ?? cand.confidence ?? null };
}
async function acceptChecked() {
  if (accepting) return;
  accepting = true;
  try { return await saveChecked(); } finally { accepting = false; if (S) refreshPendingBar(); }
}
let accepting = false;   // one Save at a time; the page redraws during a save, so the button alone cannot guard it
async function saveChecked() {
  // Fresh channel data first (reload settles the held suggestions against it), so a guide saved on another
  // device is never replaced by a held suggestion.
  if (!(await reload())) return;
  const out = [];
  const sentGuide = new Map();   // channel id -> the guide candidate sent, to clear exactly what was saved
  for (const [id, p] of S.pending) {
    const c = chById(id);
    if (!c) continue;
    const entry = { id };
    // A guide saved meanwhile (here or on another device) or "left as is" is never replaced by a suggestion.
    if (p.guide?.checked && p.guide.cands?.length) {
      entry.guide = guideFromCandidate(c, p.guide.cands[p.guide.idx]);
      sentGuide.set(id, p.guide.cands[p.guide.idx]);
    }
    const add = (p.backups || []).filter((b) => b.checked).map((b) => ({ provider: b.provider, stream_id: String(b.stream_id) }));
    if (add.length) entry.add_streams = add;
    if (entry.guide || entry.add_streams) out.push(entry);
  }
  if (!out.length) return;
  const st = S;
  const saved = [];
  let failed = [];
  try {
    await api.applyChannels(out);
    saved.push(...out);
  } catch (e) {
    // The all-at-once save failed: save channel by channel so one bad channel can't block the rest.
    for (const x of out) {
      try {
        await api.applyChannels([x]);
        saved.push(x);
      } catch (err) { failed.push({ x, err }); }
    }
  }
  // What was saved leaves the holding area; unchecked suggestions on the same channels stay to review later.
  // Pruned by what was sent, not by the checks now: rows stay clickable while a save runs.
  for (const x of saved) {
    const p = st.pending.get(x.id);
    if (!p) continue;
    if (x.guide && p.guide?.cands?.[p.guide.idx] === sentGuide.get(x.id)) p.guide = null;
    if (x.add_streams) { const sent = new Set(x.add_streams.map(refKey)); p.backups = (p.backups || []).filter((b) => !sent.has(refKey(b))); }
    if (!p.guide?.cands?.length && !p.backups?.length) st.pending.delete(x.id);
  }
  if (S !== st) return;
  if (failed.length) {
    const names = failed.slice(0, 3).map((f) => chById(f.x.id)?.name || `#${f.x.id}`).join(", ");
    toast(`${saved.length} saved, ${failed.length} not saved`,
      `Not saved: ${names}${failed.length > 3 ? ` and ${failed.length - 3} more` : ""}. They stay checked and are kept, so press Save again. Reason: ${failed[0].err?.message || "unknown"}`, "warn", 15000);
    if (saved.length) await reload();
    else renderFrame();
    return;
  }
  // Drop remaining suggestions on channels we just saved only if nothing is left unchecked.
  toast("Saved", `${out.length} channel${out.length === 1 ? "" : "s"} updated`, "ok");
  await reload();
}
async function discardPending() {
  const n = S.pending.size;
  if (!n) return;
  const ok = await confirmDialog({ title: `Discard suggestions for ${n} channel${n === 1 ? "" : "s"}?`, message: "Unsaved Auto-match suggestions are removed. Run Auto-match again to get them back.", confirm: "Discard", danger: true });
  if (!ok || !S) return;
  S.pending.clear();
  renderFrame();
}

async function offerUnsaved() {
  let saved;
  try { saved = JSON.parse(localStorage.getItem("coax.unsaved") || "null"); } catch { saved = null; }
  if (!saved?.entries?.length) return;
  const ok = await confirmDialog({
    title: "Unsaved changes found",
    message: `${saved.entries.length} channel change${saved.entries.length === 1 ? "" : "s"} from ${new Date(saved.at).toLocaleString()} never reached coax. Save them now?`,
    confirm: "Save them",
  });
  if (!ok) { try { localStorage.removeItem("coax.unsaved"); } catch {} return; }
  let n = 0;
  const left = [];
  for (const x of saved.entries) {
    try {
      if (x.add_streams || x.guide) await api.applyChannels([x]);
      else if (x.streams) await api.updateChannel(x.id, { streams: x.streams });
      n++;
    } catch (err) { if (err?.status !== 404) left.push(x); }
  }
  try { left.length ? localStorage.setItem("coax.unsaved", JSON.stringify({ at: saved.at, entries: left })) : localStorage.removeItem("coax.unsaved"); } catch {}
  toast(left.length ? `${n} saved, ${left.length} still not saved` : "Saved", left.length ? "They will be offered again next time." : `${n} channel${n === 1 ? "" : "s"} restored`, left.length ? "warn" : "ok");
  if (S) await reload();
}
function downloadUnsaved() {
  const rows = [];
  for (const [id, p] of S.pending) {
    const c = chById(id);
    if (!c) continue;
    const g = p.guide?.checked && p.guide.cands?.length ? guideFromCandidate(c, p.guide.cands[p.guide.idx]) : null;
    const b = (p.backups || []).filter((x) => x.checked).map((x) => ({ provider: x.provider, stream_id: String(x.stream_id), name: x.name }));
    if (g || b.length) rows.push({ id, name: c.name, guide: g, add_backups: b });
  }
  const blob = new Blob([JSON.stringify({ saved_at: new Date().toISOString(), channels: rows }, null, 2)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `coax-unsaved-${new Date().toISOString().slice(0, 16).replace(/[:T]/g, "")}.json`;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
  toast("Downloaded", `${rows.length} channels with checked suggestions`, "ok");
}

function feedNote(c) {
  const n = c.feed_notes || [];
  if (!n.length) return "";
  const t = (x) => new Date(x.t * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  const sub = n.map((x) => `${t(x)}: ${x.text}`).join("\n");
  const label = { removed: "Stream removed", promoted: "New main stream", undo: "Change undone", flagged: "Bad stream seen" }[n[0].kind] || "Streams reordered";
  return ` <span class="badge badge-warn feed-note" data-pop="${esc(label)}, ${esc(t(n[0]))}" data-pop-sub="${esc(sub)}" data-pop-always>${icon("layers", 'style="width:12px;height:12px"')}${esc(label)}</span>`;
}

// ---------- Guide editor ----------
function xmltvSources() {
  const fromProviders = S.providers.map((p) => ({ id: p.id, name: p.name, kind: "provider" }));
  const extra = (S.config?.xmltv_sources || []).map((x) => ({ id: x.id, name: x.name || x.id, kind: "source" }));
  return [...fromProviders, ...extra];
}
function guideModal(c) {
  const g = { mode: "none", station_id: "", xmltv_id: "", xmltv_provider: "", ...(c.guide || {}) };
  const p = S.pending.get(c.id);
  const sources = xmltvSources();
  if (!g.xmltv_provider && sources[0]) g.xmltv_provider = sources[0].id;
  const m = openModal({
    title: `Guide · ${c.name}`,
    wide: true,
    body: `
      <div class="row row-wrap" style="margin-bottom:16px">${logo(c.logo, c.name, "logo-lg")}<div><div style="font-weight:620;font-size:16px">${esc(c.name)}</div><div class="faint small">${esc(c.group || "")}${xmltvOnly(c) ? " · in XMLTV-only lineups" : ""}</div></div><span class="spacer"></span>
      </div>
      <div class="grid grid-2">
        <div class="stack" data-pane="gn">
          <div class="label">Gracenote station <button type="button" class="btn btn-ghost btn-sm" data-gclear style="padding:0 6px">Clear</button> <span class="faint" id="gnCur">${g.station_id ? `· current <span class="num">${esc(g.station_id)}</span>` : ""}</span></div>
          <label class="search"><span class="sr-only">Search stations</span>${icon("search")}<input class="input" id="gnQ" placeholder="Call sign, name or station id" value="${esc(c.name)}"></label>
          <div class="row" style="gap:8px"><input class="input input-sm mono" id="gnManual" inputmode="numeric" placeholder="Or type a station ID" value="${esc(g.station_id || "")}" style="flex:1"><button type="button" class="btn btn-sm" data-manual>Use this ID</button></div>
          <div class="result-list" id="gnList">${p?.guide?.cands?.length ? candList(p.guide.cands, g.station_id) : `<div class="faint small">Search to see stations.</div>`}</div>
        </div>
        <div class="stack" data-pane="xm">
          <div class="label">XMLTV channel <button type="button" class="btn btn-ghost btn-sm" data-xclear style="padding:0 6px">Clear</button> <span class="faint" id="xmCur">${g.mode === "xmltv" && g.xmltv_id ? `· current <span class="mono">${esc(g.xmltv_id)}</span>` : ""}</span></div>
          <div class="row"><select class="input" id="xmSrc" style="flex:0 0 42%">${sources.map((s) => `<option value="${esc(s.id)}" ${s.id === g.xmltv_provider ? "selected" : ""}>${esc(s.name)}${s.kind === "source" ? " (XMLTV)" : ""}</option>`).join("") || `<option value="">No sources</option>`}</select>
          <label class="search" style="flex:1"><span class="sr-only">Search XMLTV ids</span>${icon("search")}<input class="input" id="xmQ" placeholder="Search id or name"></label></div>
          <div class="result-list" id="xmList"><div class="faint small">Loading ids…</div></div>
        </div>
      </div>`,
    foot: `<span class="faint small" style="margin-right:auto" id="gSummary"></span><label class="row small" style="gap:6px;margin-right:12px" title="Reviewed: take this channel out of the Needs attention queue"><input type="checkbox" id="gLeft" ${g.reviewed ? "checked" : ""}>Left as is</label><button class="btn" data-close>Cancel</button><button class="btn" data-save-next title="Save and open the next channel in this list (Enter)">Save, next</button><button class="btn btn-primary" data-save>Save guide</button>`,
  });
  const el = m.el;
  const summary = () => {
    $("#gSummary", el).innerHTML = `Mode <b>${esc(g.mode)}</b>${g.station_id ? ` · station <span class="num">${esc(g.station_id)}</span>` : ""}${g.mode === "xmltv" && g.xmltv_id ? ` · xmltv <span class="mono">${esc(g.xmltv_id)}</span>` : ""}`;
    $$("[data-pane]", el).forEach((pane) => {
      pane.style.opacity = "1";
    });
  };
  // A guide is one source: picking a Gracenote station clears the XMLTV id, and the other way around.
  const useStation = () => {
    g.mode = "gracenote"; g.xmltv_id = ""; g.xmltv_provider = "";
    $("#xmCur", el).innerHTML = ""; $$("[data-xmid]", el).forEach((b) => b.classList.remove("picked"));
  };
  const useXmltv = () => {
    g.mode = "xmltv"; g.station_id = ""; g.call_sign = ""; g.station_name = ""; g.confidence = null;
    $("#gnCur", el).innerHTML = ""; $("#gnManual", el).value = ""; $$("[data-station]", el).forEach((b) => b.classList.remove("picked"));
  };
  summary();
  let manualOk = true;
  el.addEventListener("click", (e) => {
    if (e.target.closest("[data-manual]")) {
      const v = $("#gnManual", el).value.trim();
      if (!/^\d{1,12}$/.test(v)) { toast("Station IDs are numbers", "Example: 62420", "warn", 3000); manualOk = false; return; }
      manualOk = true;
      useStation();
      g.station_id = v; g.call_sign = ""; g.station_name = ""; g.confidence = null;
      $("#gnCur", el).innerHTML = `· current <span class="num">${esc(v)}</span>`;
      summary(); return;
    }
    if (e.target.closest("[data-gclear]")) { g.station_id = ""; g.call_sign = ""; g.station_name = ""; g.confidence = null; $("#gnCur", el).innerHTML = ""; $$("[data-station]", el).forEach((b) => b.classList.remove("picked")); summary(); return; }
    if (e.target.closest("[data-xclear]")) { g.xmltv_id = ""; $$("[data-xmid]", el).forEach((b) => b.classList.remove("picked")); summary(); return; }
    const mb = e.target.closest("[data-mode]");
    if (mb) { g.mode = mb.dataset.mode; $$("[data-mode]", el).forEach((b) => b.setAttribute("aria-pressed", b === mb)); summary(); return; }
    const st = e.target.closest("[data-station]");
    if (st) {
      useStation();
      g.station_id = st.dataset.station; g.call_sign = st.dataset.call || ""; g.station_name = st.dataset.name || ""; g.confidence = st.dataset.conf ? Number(st.dataset.conf) : null;
      $("#gnCur", el).innerHTML = `· current <span class="num">${esc(g.station_id)}</span>`;
      $$("[data-station]", el).forEach((b) => b.classList.toggle("picked", b === st));
      summary(); return;
    }
    const xm = e.target.closest("[data-xmid]");
    if (xm) {
      useXmltv();
      g.xmltv_id = xm.dataset.xmid; g.xmltv_provider = $("#xmSrc", el).value;
      $("#xmCur", el).innerHTML = `· current <span class="mono">${esc(g.xmltv_id)}</span>`;
      $$("[data-xmid]", el).forEach((b) => b.classList.toggle("picked", b === xm));
      summary();
    }
  });
  let gnSeq = 0;
  const searchGn = debounce(async () => {
    const q = $("#gnQ", el).value.trim();
    const list = $("#gnList", el);
    if (!q) return;
    const seq = ++gnSeq;
    list.innerHTML = `<span class="skel" style="height:52px;border-radius:12px"></span><span class="skel" style="height:52px;border-radius:12px"></span>`;
    try {
      const quick = await api.gracenoteSearch(q, true).catch(() => []);
      if (seq !== gnSeq) return;
      if (quick.length) list.innerHTML = candList(quick, g.station_id) + `<div class="faint small" style="padding:6px 2px">Searching all of Gracenote…</div>`;
      const r = await api.gracenoteSearch(q);
      if (seq !== gnSeq) return;
      list.innerHTML = r.length ? candList(r, g.station_id) : `<div class="faint small">No stations found for “${esc(q)}”.</div>`;
    } catch (e) { if (seq === gnSeq) list.innerHTML = `<div class="callout">${icon("alert")}<div>${esc(e.message)}</div></div>`; }
  }, 200);
  $("#gnQ", el).addEventListener("input", searchGn);
  if (!p?.guide?.cands?.length) searchGn();
  let xmSeq = 0;
  const searchXm = debounce(async () => {
    const src = $("#xmSrc", el).value;
    const list = $("#xmList", el);
    if (!src) { list.innerHTML = `<div class="faint small">Add a provider with an XMLTV feed or an XMLTV source in Settings.</div>`; return; }
    const seq = ++xmSeq;
    try {
      // The channel's name and group rank the results (an NFL PACKAGE channel leans to NFL entries).
      const r = await api.xmltvIds(src, $("#xmQ", el).value.trim(), `${c.name} ${c.group || ""}`);
      if (seq !== xmSeq) return;
      list.innerHTML = r.length ? r.slice(0, 100).map((x) => `<button type="button" class="result ${x.id === g.xmltv_id ? "picked" : ""}" data-xmid="${esc(x.id)}">${logo(x.icon, x.name || x.id, "logo-sm")}<span class="r-main" data-pop="${esc(x.name || x.id)}"><span class="r-title">${esc(x.name || x.id)}</span><br><span class="r-sub mono">${esc(x.id)}</span></span></button>`).join("") : `<div class="faint small">No XMLTV channels match. The feed may not be downloaded yet.</div>`;
    } catch (e) { if (seq === xmSeq) list.innerHTML = `<div class="callout">${icon("alert")}<div>${esc(e.message)}</div></div>`; }
  }, 250);
  $("#xmQ", el).addEventListener("input", searchXm);
  $("#xmSrc", el).addEventListener("change", searchXm);
  $("#xmQ", el).value = c.name.replace(/^[A-Z]{2,3}\s*[|:]\s*/, "").replace(/\b(HD|FHD|UHD|4K)\b/gi, "").trim();
  searchXm();
  // The next channel in the list as shown now (captured before saving, since saving can drop it from a filter).
  const order = visibleChannels().map((x) => x.id);
  const nextId = order[order.indexOf(c.id) + 1];
  let saving = false;
  const save = async (goNext) => {
    if (saving || m.closed) return;   // a second Enter or click while saving must not save twice
    saving = true;
    try {
      g.reviewed = $("#gLeft", el).checked;
      // A station wins over an XMLTV id left from an older build; the server keeps only one either way.
      // A Gracenote channel without a station stays Gracenote (unmatched; a provider tvg-id is only a hint).
      g.mode = g.station_id ? "gracenote" : g.mode === "xmltv" && g.xmltv_id ? "xmltv" : g.mode === "gracenote" ? "gracenote" : "none";
      if (g.station_id) { g.xmltv_id = ""; g.xmltv_provider = ""; }
      const r = await api.updateChannel(c.id, { guide: g });
      Object.assign(chById(c.id) || c, r && typeof r === "object" && r.id ? r : { guide: g });
      settleHeld();   // drops the held guide suggestion only when a guide was saved or the channel was left as is
      toast("Guide saved", c.name, "ok", 1500);
      m.close();
      renderFrame();
      const next = goNext && nextId != null ? chById(nextId) : null;
      if (next) guideModal(next);
      else if (goNext) toast("End of the list", "", "info", 2000);
    } catch (err) { toastError("Could not save guide", err); } finally { saving = false; }
  };
  $("[data-save]", el).onclick = (e) => busy(e.currentTarget, () => save(false));
  $("[data-save-next]", el).onclick = (e) => busy(e.currentTarget, () => save(true));
  // Enter saves and opens the next channel; Shift+Enter saves and closes.
  // In the station ID box, Enter applies the typed ID first.
  el.addEventListener("keydown", (e) => {
    if (e.key !== "Enter" || e.isComposing) return;
    if (e.target.matches("button, select, a")) return;
    e.preventDefault();
    if (e.target.id === "gnQ") { searchGn(); return; }
    if (e.target.id === "xmQ") return;
    if (e.target.id === "gnManual" && e.target.value.trim()) {
      $("[data-manual]", el).click();
      if (!manualOk) return;   // an invalid ID warns and stays on this channel
    }
    save(!e.shiftKey);
  });
}
function candList(list, current) {
  return list.slice(0, 30).map((s) => {
    const conf = s.score ?? s.confidence;
    const sub = [s.name && s.call_sign ? s.name : "", s.affiliate, s.type, (s.countries || []).join?.(", ")].filter(Boolean).join(" · ");
    return `<button type="button" class="result ${String(s.station_id) === String(current) ? "picked" : ""}" data-station="${esc(s.station_id)}" data-call="${esc(s.call_sign || "")}" data-name="${esc(s.name || "")}" ${conf != null ? `data-conf="${esc(conf)}"` : ""}>
      ${logo(s.logo, s.call_sign || s.name, "logo-sm")}
      <span class="r-main" data-pop="${esc(s.call_sign || s.name || s.station_id)}"><span class="r-title">${esc(s.call_sign || s.name || s.station_id)} <span class="faint num">${esc(s.station_id)}</span></span><br><span class="r-sub">${esc(sub)}${s.reason ? ` · ${esc(s.reason)}` : ""}</span></span>
      ${conf != null ? confidenceHtml(conf) : ""}${s.outside ? `<span class="badge badge-warn" title="Outside your lineup area">outside</span>` : ""}</button>`;
  }).join("");
}

// ---------- Streams / backups editor ----------
const refKey = (s) => `${s.provider}:${s.stream_id}`;
function streamsModal(c) {
  let streams = (c.streams || []).map((s) => ({ ...s }));
  const p = S.pending.get(c.id);
  const m = openModal({
    title: `Streams · ${c.name}`,
    wide: true,
    body: `
      <p class="muted small" style="margin-bottom:14px">The first stream is the primary. When a stream fails or its provider has no free line, coax tries the others, the most reliable first (by Smart Standby score), in this order when they tie. Drag to reorder.</p>
      <div class="grid grid-2">
        <div class="stack"><div class="label">Order</div><div class="backup-list" id="bList"></div>
          ${p?.backups?.length ? `<div class="label" style="margin-top:6px">Suggested</div><div class="backup-list" id="bSugg"></div>` : ""}</div>
        <div class="stack"><div class="label">Add from any provider</div>
          <div class="row"><select class="input input-sm" id="bProv" style="flex:0 0 40%"><option value="">All providers</option>${S.providers.map((x) => `<option value="${esc(x.id)}">${esc(x.name)}</option>`).join("")}</select>
          <label class="search" style="flex:1"><span class="sr-only">Search streams</span>${icon("search")}<input class="input input-sm" id="bQ" value="${esc(c.name.replace(/^[A-Z]{2,3}\s*[|:]\s*/, ""))}"></label></div>
          ${switchHtml("", false, "Include unselected groups", 'id="bAll"')}
          <div class="result-list" id="bRes" style="max-height:360px;overflow:auto"></div></div>
      </div>`,
    foot: `<button class="btn" data-close>Cancel</button><button class="btn btn-primary" data-save>Save streams</button>`,
  });
  const el = m.el;
  const draw = () => {
    $("#bList", el).innerHTML = streams.length ? streams.map((s, i) => `<div class="backup-item ${i === 0 ? "primary" : ""} ${s.missing ? "missing" : ""} ${s.disabled ? "off" : ""}" data-i="${i}">
      <button class="drag-handle" data-bdrag aria-label="Move ${esc(s.name || s.stream_id)}, Alt plus arrow keys">${icon("grip")}</button>
      <span class="order">${i + 1}</span>
      <span class="b-main" style="flex:1;min-width:0"><span class="ellip" style="display:block;font-weight:560" data-pop="${esc(s.name || s.stream_id)}">${esc(s.name || s.stream_id)}</span><span class="faint small">${esc(providerName(S.providers, s.provider))} · <span class="num">${esc(s.stream_id)}</span>${s.missing ? ' · <span style="color:var(--bad)">vanished</span>' : ""}</span></span>
      ${i === 0 ? `<span class="badge badge-accent">Primary</span>` : ""}
      ${s.disabled ? `<span class="badge badge-bad">Disabled</span>` : ""}
      <label class="f-off" title="A disabled stream is never played or used as a backup"><input type="checkbox" data-boff="${i}" ${s.disabled ? "checked" : ""}> Disable</label>
      <button class="btn btn-ghost btn-sm btn-icon btn-danger" data-rm="${i}" aria-label="Remove">${icon("x")}</button></div>`).join("") : `<div class="faint small">No streams. Add one from the right.</div>`;
    const sg = $("#bSugg", el);
    if (sg) {
      const keys = new Set(streams.map(refKey));
      const left = p.backups.filter((b) => !keys.has(refKey(b)));
      sg.innerHTML = left.length ? left.map((b) => resultHtml(b, `${pct(b.score)}%`)).join("") : `<div class="faint small">All suggestions added.</div>`;
    }
  };
  const resultHtml = (s, extra = "") => `<button type="button" class="result" data-add='${esc(JSON.stringify({ provider: s.provider, stream_id: String(s.stream_id), name: s.name, group: s.group }))}'>${logo(s.logo, s.name, "logo-sm")}<span class="r-main"><span class="r-title ellip">${esc(s.name)}</span><br><span class="r-sub">${esc(providerName(S.providers, s.provider))}${s.group ? ` · ${esc(s.group)}` : ""}${s.reason ? ` · ${esc(s.reason)}` : ""}</span></span>${extra ? `<span class="badge">${esc(extra)}</span>` : ""}${icon("plus", 'style="width:16px;height:16px;color:var(--accent)"')}</button>`;
  draw();
  // Reorder with pointer events instead of HTML drag-and-drop: the list reorders live as you move,
  // gaps between items and dropping outside an item still count, and it works the same in every browser.
  const list = $("#bList", el);
  let drag = null;
  list.addEventListener("pointerdown", (e) => {
    const it = e.target.closest(".backup-item");
    if (!it || e.button !== 0 || e.target.closest("[data-rm]")) return;
    drag = { i: Number(it.dataset.i), y: e.clientY, on: false, id: e.pointerId };
  });
  list.addEventListener("pointermove", (e) => {
    if (!drag || e.pointerId !== drag.id) return;
    if (!drag.on) {
      if (Math.abs(e.clientY - drag.y) < 4) return;
      drag.on = true;
      list.setPointerCapture(e.pointerId);
      list.classList.add("reordering");
      list.querySelector(`.backup-item[data-i="${drag.i}"]`)?.classList.add("dragging");
    }
    e.preventDefault();
    const items = [...list.querySelectorAll(".backup-item")];
    // New position = how many of the other rows have their middle above the pointer.
    let to = 0;
    items.forEach((it, k) => { if (k !== drag.i) { const r = it.getBoundingClientRect(); if (e.clientY > r.top + r.height / 2) to++; } });
    if (to === drag.i) return;
    const [x] = streams.splice(drag.i, 1);
    streams.splice(to, 0, x);
    drag.i = to;
    draw();
    list.querySelector(`.backup-item[data-i="${to}"]`)?.classList.add("dragging");
  });
  const endDrag = (e) => {
    if (!drag || (e && e.pointerId !== drag.id)) return;
    if (drag.on) { try { list.releasePointerCapture(drag.id); } catch {} }
    drag = null;
    list.classList.remove("reordering");
    $$(".dragging", list).forEach((x) => x.classList.remove("dragging"));
  };
  list.addEventListener("pointerup", endDrag);
  list.addEventListener("pointercancel", endDrag);
  el.addEventListener("keydown", (e) => {
    const h = e.target.closest("[data-bdrag]");
    if (!h || !e.altKey || !["ArrowUp", "ArrowDown"].includes(e.key)) return;
    e.preventDefault();
    const i = Number(h.closest(".backup-item").dataset.i), j = e.key === "ArrowUp" ? i - 1 : i + 1;
    if (j < 0 || j >= streams.length) return;
    [streams[i], streams[j]] = [streams[j], streams[i]];
    draw();
    $(`.backup-item[data-i="${j}"] [data-bdrag]`, el)?.focus();
  });
  el.addEventListener("click", (e) => {
    const rm = e.target.closest("[data-rm]");
    if (rm) { streams.splice(Number(rm.dataset.rm), 1); draw(); return; }
    const add = e.target.closest("[data-add]");
    if (add) {
      const s = JSON.parse(add.dataset.add);
      if (streams.some((x) => refKey(x) === refKey(s))) { toast("Already in the list", s.name, "info", 2000); return; }
      streams.push(s); draw();
    }
  });
  let seq = 0;
  const search = debounce(async () => {
    const q = $("#bQ", el).value.trim();
    const res = $("#bRes", el);
    const n = ++seq;
    res.innerHTML = `<span class="skel" style="height:48px;border-radius:12px"></span>`.repeat(3);
    try {
      const r = normalizePage(await api.streams({ provider: $("#bProv", el).value, q, offset: 0, limit: 50, all_groups: $("#bAll", el).checked ? 1 : "" }));
      if (n !== seq) return;
      res.innerHTML = r.items.length ? r.items.map((s) => resultHtml(s)).join("") : `<div class="faint small">No streams match.</div>`;
    } catch (err) { if (n === seq) res.innerHTML = `<div class="callout">${icon("alert")}<div>${esc(err.message)}</div></div>`; }
  }, 250);
  $("#bQ", el).addEventListener("input", search);
  $("#bProv", el).addEventListener("change", search);
  $("#bAll", el).addEventListener("change", search);
  search();
  el.addEventListener("change", (e) => { const t = e.target.closest("[data-boff]"); if (t) { streams[Number(t.dataset.boff)].disabled = t.checked; draw(); } });
  $("[data-save]", el).onclick = (e) => busy(e.currentTarget, async () => {
    try {
      if (streams.length && streams.every((s) => s.disabled)) { toast("Keep one stream enabled", "A channel needs at least one stream that is not disabled.", "warn"); return; }
      const r = await api.updateChannel(c.id, { streams: streams.map((s) => ({ provider: s.provider, stream_id: String(s.stream_id), ...(s.disabled ? { disabled: true } : {}) })) });
      Object.assign(chById(c.id) || c, r && r.id ? r : { streams });
      settleHeld();   // drops only the held backups the channel now has; the others stay to review
      toast("Streams saved", c.name, "ok", 2500);
      m.close();
      renderFrame();
    } catch (err) { toastError("Could not save streams", err); }
  });
}

// ---------- Lineup membership ----------
function lineupModal(ids) {
  if (!S.lineups.length) { toast("No lineups yet", "Create one on the Lineups page.", "warn"); return; }
  const chans = ids.map(chById).filter(Boolean);
  const state = new Map(S.lineups.map((l) => {
    const n = chans.filter((c) => lineupsOf(c).includes(l.id)).length;
    return [l.id, n === 0 ? false : n === chans.length ? true : null];
  }));
  const m = openModal({
    title: chans.length === 1 ? `Lineups · ${chans[0].name}` : `Lineups for ${chans.length} channels`,
    body: `<p class="muted small" style="margin-bottom:12px">A channel can be in any number of lineups. Each lineup is its own source in the DVR.</p>
      <div class="group-list">${S.lineups.map((l) => `<label class="group-item">${checkHtml(state.get(l.id) === true, `data-l="${esc(l.id)}"`)}<span class="gname">${esc(l.name)}</span><span class="badge ${guideBadge(l.guide)}">${esc(l.guide || "per-channel")}</span></label>`).join("")}</div>`,
    foot: `<button class="btn" data-close>Cancel</button><button class="btn btn-primary" data-save>Save</button>`,
  });
  $$("[data-l]", m.el).forEach((cb) => { if (state.get(cb.dataset.l) === null) cb.indeterminate = true; cb.onchange = () => state.set(cb.dataset.l, cb.checked); });
  $("[data-save]", m.el).onclick = (e) => busy(e.currentTarget, async () => {
    try {
      for (const c of chans) {
        const cur = new Set(lineupsOf(c));
        for (const [lid, v] of state) { if (v === true) cur.add(lid); else if (v === false) cur.delete(lid); }
        const r = await api.updateChannel(c.id, { lineups: [...cur] });
        c.lineups = r?.lineups || [...cur];
      }
      invalidate("lineups");
      S.lineups = await loadLineups(true).catch(() => S.lineups);
      toast("Lineups updated", "", "ok", 2000);
      m.close();
      renderFrame();
    } catch (err) { toastError("Could not update lineups", err); }
  });
}
export function guideBadge(g) {
  return { gracenote: "badge-accent", xmltv: "badge-info", both: "badge-copper" }[g] || "";
}

// ---------- Channel details ----------
function editModal(c) {
  const m = openModal({
    title: `Edit · ${c.name}`,
    body: `<div class="form-grid">
      <div class="field full"><label for="eName">Name</label><input class="input" id="eName" value="${esc(c.name)}" autofocus></div>
      <div class="field"><label for="eNum">Number</label><input class="input mono" id="eNum" value="${esc(c.number ?? "")}"></div>
      <div class="field"><label for="eGroup">Group</label><input class="input" id="eGroup" value="${esc(c.group || "")}"></div>
      <div class="field full"><label for="eLogo">Logo URL</label><div class="row">${logo(c.logo, c.name)}<input class="input mono" id="eLogo" value="${esc(c.logo || "")}" placeholder="https://"></div></div>
      <div class="field full"><span class="label">Stream URL for this channel</span><div class="copy-field"><code class="mono">/stream/${esc(c.uuid || "")}.ts</code></div><span class="hint">Channel id <span class="num">${c.id}</span>. The uuid never changes.</span></div>
    </div>`,
    foot: `<button class="btn btn-danger" data-del style="margin-right:auto">${icon("trash")}Delete</button><button class="btn" data-close>Cancel</button><button class="btn btn-primary" data-save>Save</button>`,
  });
  $("[data-save]", m.el).onclick = (e) => busy(e.currentTarget, async () => {
    const body = { name: $("#eName", m.el).value.trim(), number: $("#eNum", m.el).value.trim(), group: $("#eGroup", m.el).value.trim(), logo: $("#eLogo", m.el).value.trim() };
    if (!body.name) { toast("Name is required", "", "warn"); return; }
    try { const r = await api.updateChannel(c.id, body); Object.assign(c, r && r.id ? r : body); m.close(); renderFrame(); toast("Saved", c.name, "ok", 2000); }
    catch (err) { toastError("Could not save channel", err); }
  });
  $("[data-del]", m.el).onclick = async () => {
    m.close();
    if (!(await confirmDialog({ title: `Delete ${c.name}?`, message: "It disappears from every lineup.", confirm: "Delete", danger: true }))) return;
    try { await api.deleteChannel(c.id); S.channels = S.channels.filter((x) => x.id !== c.id); S.selected.delete(c.id); S.pending.delete(c.id); renderFrame(); toast("Deleted", c.name, "ok"); }
    catch (err) { toastError("Delete failed", err); }
  };
}

// ---------- Catalog ----------
function catalogHtml() {
  return `<div class="catalog-head">
      <div class="row"><h2 style="font-size:15px;font-weight:620">Catalog</h2><span class="spacer"></span><span class="faint small num" id="catTotal"></span></div>
      <div class="catalog-filters" id="catFilters">${catalogFiltersHtml()}</div>
      <label class="search"><span class="sr-only">Search catalog</span>${icon("search")}<input class="input input-sm" id="catQ" placeholder="Search streams" value="${esc(S.cat.q)}"></label>
    </div>
    <div class="catalog-list" id="catList"></div>
    <div class="catalog-foot" id="catFoot"></div>`;
}
function catalogFiltersHtml() {
  const groups = S.cat.groups.filter((g) => !S.cat.provider || g.provider === S.cat.provider);
  return `<select class="input input-sm" data-cat-provider aria-label="Provider"><option value="">All providers</option>${S.providers.map((p) => `<option value="${esc(p.id)}" ${S.cat.provider === p.id ? "selected" : ""}>${esc(p.name)}</option>`).join("")}</select>
    <select class="input input-sm" data-cat-group aria-label="Group"><option value="">Selected groups</option>${groups.map((g) => `<option value="${esc(g.name)}" ${S.cat.group === g.name ? "selected" : ""}>${esc(g.name)}${g.count != null ? ` (${g.count})` : ""}</option>`).join("")}</select>`;
}
function renderCatalogFilters() { const f = $("#catFilters", root); if (f) f.innerHTML = catalogFiltersHtml(); }
function wireCatalog() {
  const q = $("#catQ", root);
  if (!q) return;
  q.oninput = debounce(() => { S.cat.q = q.value; S.cat.offset = 0; loadCatalog(); }, 220);
  if (S.cat.items.length || S.cat.loading) renderCatalogList();
  const list = $("#catList", root);
  let lastIdx = null;
  list.addEventListener("click", (e) => {
    const row = e.target.closest("[data-k]");
    if (!row) return;
    const idx = Number(row.dataset.idx);
    const s = S.cat.items[idx];
    const on = !S.cat.selected.has(row.dataset.k);
    if (e.shiftKey && lastIdx != null) {
      const [a, b] = [Math.min(lastIdx, idx), Math.max(lastIdx, idx)];
      for (let i = a; i <= b; i++) on ? S.cat.selected.set(refKey(S.cat.items[i]), S.cat.items[i]) : S.cat.selected.delete(refKey(S.cat.items[i]));
    } else on ? S.cat.selected.set(row.dataset.k, s) : S.cat.selected.delete(row.dataset.k);
    lastIdx = idx;
    e.preventDefault();
    renderCatalogList();
  });
  list.addEventListener("keydown", (e) => { if ((e.key === " " || e.key === "Enter") && e.target.matches("[data-k]")) { e.preventDefault(); e.target.click(); } });
}
async function loadCatalogGroups() {
  const out = [];
  await Promise.all(S.providers.map(async (p) => {
    try {
      const { groups, selected } = normalizeGroups(await api.getGroups(p.id), p);
      groups.filter((g) => selected.has(g.name)).forEach((g) => out.push({ ...g, provider: p.id }));
    } catch {}
  }));
  if (!S) return;
  S.cat.groups = out;
  renderCatalogFilters();
}
let catSeq = 0;
async function loadCatalog() {
  if (!S || !S.cat.open) return;
  const seq = ++catSeq;
  S.cat.loading = true;
  renderCatalogList();
  try {
    const r = normalizePage(await api.streams({ provider: S.cat.provider, group: S.cat.group, q: S.cat.q.trim(), offset: S.cat.offset, limit: PER_PAGE }));
    if (!S || seq !== catSeq) return;
    S.cat.items = r.items;
    S.cat.total = r.total;
    S.cat.error = null;
  } catch (e) {
    if (!S || seq !== catSeq) return;
    S.cat.items = [];
    S.cat.total = 0;
    S.cat.error = e.message;
  }
  S.cat.loading = false;
  renderCatalogList();
}
function renderCatalogList() {
  const list = $("#catList", root), foot = $("#catFoot", root), tot = $("#catTotal", root);
  if (!list) return;
  const c = S.cat;
  if (tot) tot.textContent = c.total ? `${fmtInt(c.total)} streams` : "";
  if (c.loading && !c.items.length) {
    list.innerHTML = Array.from({ length: 9 }, (_, i) => `<div class="cat-row"><span class="skel" style="width:18px;height:18px"></span><span class="skel" style="width:28px;height:28px;border-radius:7px"></span><span style="flex:1"><span class="skel" style="width:${50 + ((i * 23) % 40)}%"></span></span></div>`).join("");
  } else if (c.error) {
    list.innerHTML = `<div class="empty" style="padding:32px 16px">${icon("alert", 'style="width:28px;height:28px;color:var(--bad)"')}<p>${esc(c.error)}</p><button class="btn btn-sm" onclick="this.closest('.catalog').querySelector('#catQ').dispatchEvent(new Event('input'))">Retry</button></div>`;
  } else if (!c.items.length) {
    list.innerHTML = S.providers.length
      ? `<div class="empty" style="padding:36px 16px"><h3>No streams</h3><p class="small">${c.q ? "Nothing matches this search." : "Pick groups for your providers on the Providers page, then refresh."}</p></div>`
      : `<div class="empty" style="padding:36px 16px"><h3>No providers</h3><p class="small">Add an Xtream or M3U provider to browse its streams.</p><a class="btn btn-sm btn-primary" href="#/providers">Add provider</a></div>`;
  } else {
    list.classList.toggle("busy", c.loading);
    list.innerHTML = `<label class="cat-row cat-all">${checkHtml(c.items.every((s) => c.selected.has(refKey(s))), 'data-cat-all aria-label="Select page"')}<span class="faint small">Select this page</span></label>` +
      c.items.map((s, i) => {
        const k = refKey(s);
        const used = (s.channels || []).length;
        return `<div class="cat-row ${c.selected.has(k) ? "on" : ""}" data-k="${esc(k)}" data-idx="${i}" role="checkbox" aria-checked="${c.selected.has(k)}" tabindex="0">
          ${checkHtml(c.selected.has(k), 'tabindex="-1" aria-hidden="true"')}${logo(s.logo, s.name, "logo-sm")}
          <span class="cat-main" data-pop="${esc(s.name)}" data-pop-sub="${esc(s.group || "")}"><span class="ellip cat-name">${esc(s.name)}</span><span class="ellip faint small">${esc(s.group || "")}${S.providers.length > 1 ? ` · ${esc(providerName(S.providers, s.provider))}` : ""}</span></span>
          ${used ? `<span class="badge badge-ok" title="Already used by ${used} channel(s)">${icon("check", 'style="width:11px;height:11px"')}in use</span>` : ""}</div>`;
      }).join("");
  }
  if (foot) {
    const n = c.selected.size;
    const from = c.total ? c.offset + 1 : 0, to = Math.min(c.offset + PER_PAGE, c.total);
    foot.innerHTML = `<div class="pager" style="border:0;padding:0 0 10px">
        <span class="num">${fmtInt(from)}–${fmtInt(to)}</span><span class="faint">of</span><span class="num">${fmtInt(c.total)}</span><span class="spacer"></span>
        <button class="btn btn-sm btn-icon" data-act="cat-prev" ${c.offset <= 0 ? "disabled" : ""} aria-label="Previous page">${icon("chevron", 'style="transform:rotate(90deg)"')}</button>
        <button class="btn btn-sm btn-icon" data-act="cat-next" ${to >= c.total ? "disabled" : ""} aria-label="Next page">${icon("chevron", 'style="transform:rotate(-90deg)"')}</button></div>
      <div class="row">${n ? `<button class="btn btn-sm btn-ghost" data-act="cat-clear">Clear</button>` : ""}<button class="btn btn-primary" style="flex:1" data-act="cat-add" ${n ? "" : "disabled"}>${icon("plus")}Add ${n ? fmtInt(n) + " " : ""}selected</button></div>`;
  }
}
async function addFromCatalog() {
  const streams = [...S.cat.selected.values()].map((s) => ({ provider: s.provider, stream_id: String(s.stream_id) }));
  if (!streams.length) return;
  try {
    const body = { streams };
    if (S.lineup) body.lineups = [S.lineup];
    const r = await api.bulkAdd(body);
    const created = r?.created?.length ?? streams.length;
    const skipped = r?.skipped?.length || 0;
    toast(`Added ${created} channel${created === 1 ? "" : "s"}`, skipped ? `${skipped} already in use and skipped.` : "Run Auto-match to fill guide and backups.", "ok");
    S.cat.selected.clear();
    await reload();
    loadCatalog();
  } catch (e) { toastError("Could not add channels", e); }
}
