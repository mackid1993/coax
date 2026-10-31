// On demand: the Channels page for movies and TV shows. Groups on the left (with Add all), the Catalog of every
// title in the groups made available on the Providers page, and the Content: everything that is or will be in
// Channels DVR. Hand-picks and removals are kept on the server as you work (like the Channels page's unsaved
// suggestions), so a reload, another device or a restart never loses them; nothing reaches Channels DVR until Save.
import { api } from "../api.js";
import { newDraft, keepOnServer } from "../autosave.js";
import { $, esc, icon, toast, toastError, openModal, confirmDialog, emptyState, debounce, busy, switchHtml, checkHtml, fmtInt, fmtAgo } from "../ui.js";

const PER_PAGE = 100;
const KINDS = { movies: "Movies", series: "TV Shows" };

let S;      // page state
let root;
let draft = newDraft();   // the server's copy of the unsaved changes (autosave.js)
let poll;

function freshState() {
  return {
    frame: null, loading: true, error: null,
    kind: "", provider: "", group: "", q: "",
    cat: pane("all"), con: pane("all"),
    pending: {},        // provider id -> {key: "add" | "remove"}
    notes: [],
  };
}
function pane(filter = "") {
  return { offset: 0, total: 0, items: [], loading: false, error: null, selected: new Map(), last: null, filter, seq: 0 };
}

export async function mount(el) {
  root = el;
  S = freshState();
  draft = newDraft();
  try { const k = localStorage.getItem("coax.ondemand.kind"); if (k === "movies" || k === "series") S.kind = k; } catch {}
  render();
  await loadFrame();
  await loadDraft();
  render();
  loadBoth();
  poll = setInterval(refreshFrame, 15000);
}
export function unmount() { clearInterval(poll); S = null; }

// ---------- Server data ----------
async function loadFrame() {
  try { S.frame = await api.ondemand(); S.error = null; }
  catch (e) { if (S) S.error = e.message; }
  if (S) S.loading = false;
}
// The library moves on its own (imports, matches): keep the status line and the tags current.
async function refreshFrame() {
  if (!S || document.hidden) return;
  const before = inChannelsTotal();
  const st = S;
  try { const f = await api.ondemand(); if (S !== st) return; S.frame = f; } catch { return; }
  drawStatus();
  drawGroups();
  if (inChannelsTotal() !== before) { loadPane("cat", true); loadPane("con", true); }
}
const inChannelsTotal = () => (S.frame?.providers || []).reduce((n, p) => n + Object.values(p.groups).flat().reduce((m, g) => m + (g.in_channels || 0), 0), 0);

async function loadDraft() {
  let d;
  try { d = await api.ondemandDraft(); } catch (e) { toastError("Could not load the unsaved changes", e); return; }
  if (!S) return;
  S.pending = d.pending || {};
  S.notes = d.notes || [];
  draft = newDraft({ loaded: true, rev: d.rev || 0, sent: JSON.stringify(S.pending), duplicates: d.duplicates || 0 });
  if (S.notes.length) toast("Unsaved changes updated", S.notes.join(" "), "info", 9000);
}
function saveDraft() {
  if (!S || !draft.loaded) return;
  const st = S, d = draft, pending = S.pending;
  keepOnServer(d, JSON.stringify(pending), {
    what: "unsaved changes",
    send: (rev) => api.saveOndemandDraft({ rev, pending }),
    alive: () => S === st,
    current: () => d === draft,
    retry: saveDraft,
    saved: (r) => { if (d.duplicates !== r.duplicates) { d.duplicates = r.duplicates; drawDraft(); } },
    conflict: async () => {   // newer work from another window: take the server's copy
      await loadDraft();
      if (S !== st) return;
      if (d === draft) { d.again = false; setTimeout(() => { if (S === st && d === draft) saveDraft(); }, 5000); return; }
      toast("Unsaved changes updated", "They were changed in another window; showing the latest.", "info");
      render(); loadBoth();
    },
  });
}

function params(which) {
  const p = S[which];
  return { view: which === "cat" ? "catalog" : "content", kind: S.kind, provider: S.provider, group: S.group,
           q: S.q.trim(), filter: p.filter === "all" ? "" : p.filter, offset: p.offset, limit: PER_PAGE };
}
async function loadPane(which, quiet = false) {
  if (!S) return;
  const p = S[which];
  const seq = ++p.seq;
  if (!quiet) { p.loading = true; drawPane(which); }
  try {
    const r = await api.ondemandTitles(params(which));
    if (!S || seq !== p.seq) return;
    if (r.offset && r.offset >= r.total) { p.offset = Math.max(0, Math.floor((r.total - 1) / PER_PAGE) * PER_PAGE); return loadPane(which, quiet); }
    p.items = r.items; p.total = r.total; p.error = null;
  } catch (e) {
    if (!S || seq !== p.seq) return;
    p.items = []; p.total = 0; p.error = e.message;
  }
  p.loading = false;
  drawPane(which);
}
function loadBoth() { loadPane("cat"); loadPane("con"); }

// ---------- Derived ----------
const providers = () => S.frame?.providers || [];
const pname = (id) => providers().find((p) => p.id === id)?.name || id;
const multi = () => providers().length > 1;
const changeOf = (pid, key) => (S.pending[pid] || {})[key] || "";
function setChange(pid, key, change) {
  const mine = { ...(S.pending[pid] || {}) };
  if (change) mine[key] = change; else delete mine[key];
  const next = { ...S.pending };
  if (Object.keys(mine).length) next[pid] = mine; else delete next[pid];
  S.pending = next;
}
function counts() {
  let add = 0, remove = 0;
  for (const changes of Object.values(S.pending)) for (const c of Object.values(changes)) c === "add" ? add++ : remove++;
  return { add, remove };
}
const rowKey = (r) => `${r.provider}|${r.key}`;
const selectable = (r) => r.via !== "all";

// ---------- Frame ----------
function render() {
  if (!S) return;
  root.innerHTML = `
  <div class="page-head">
    <div>
      <div class="eyebrow">Video on demand</div>
      <h1>On demand</h1>
      <p class="sub">Pick single movies and shows for Channels DVR, or add whole groups with Add all.</p>
    </div>
  </div>
  <div id="odStatus"></div>
  <div id="odDraft"></div>
  ${body()}`;
  wire();
  drawStatus();
  drawDraft();
  drawGroups();
  drawPane("cat");
  drawPane("con");
}
function body() {
  if (S.loading) return `<div class="card" style="padding:40px"><span class="skel" style="width:40%"></span></div>`;
  if (S.error) return `<div class="card">${emptyState({ art: "signal", title: "Could not load the On demand page", text: esc(S.error), actions: `<button class="btn" data-act="retry">Try again</button>` })}</div>`;
  if (!S.frame?.running) return `<div class="card">${emptyState({ art: "tv", title: "Video on demand is not running", text: "coax could not start its video on demand library. The log on the Logs page says why." })}</div>`;
  if (!providers().length) {
    return `<div class="card">${emptyState({ art: "tv", title: "No groups are available yet", text: "Choose which of a provider's movie and series groups to make available on the Providers page (the On demand button on its card). Their titles show here, to pick one by one or add whole.", actions: `<a class="btn btn-primary" href="#/providers">${icon("providers")}Open Providers</a>` })}</div>`;
  }
  const kinds = [["", "All"], ["movies", "Movies"], ["series", "TV Shows"]];
  return `
  <div class="card od-bar">
    <div class="toolbar">
      <div class="seg" role="group" aria-label="Movies or TV shows">${kinds.map(([k, l]) => `<button type="button" data-kind="${k}" aria-pressed="${S.kind === k}">${l}</button>`).join("")}</div>
      <label class="search" style="flex:1 1 220px"><span class="sr-only">Search titles</span>${icon("search")}<input class="input input-sm" id="odQ" data-search placeholder="Search titles, like heat 1995" value="${esc(S.q)}"><kbd>/</kbd></label>
      ${multi() ? `<label class="sr-only" for="odProv">Provider</label><select class="input input-sm" id="odProv" style="width:auto;min-width:140px"><option value="">All providers</option>${providers().map((p) => `<option value="${esc(p.id)}" ${S.provider === p.id ? "selected" : ""}>${esc(p.name)}</option>`).join("")}</select>` : ""}
      <button class="btn btn-sm od-groups-btn" data-act="groups-sheet">${icon("filter")}${S.group ? esc(S.group) : "Groups"}</button>
      ${S.group || S.q || S.provider ? `<button class="btn btn-sm btn-ghost" data-act="clear-filters">Clear filters</button>` : ""}
    </div>
  </div>
  <div class="od-setup">
    <aside class="card od-groups" aria-label="Groups" id="odGroups"></aside>
    <section class="card od-pane" aria-label="Catalog">
      <div class="od-head"><h2>Catalog</h2><span class="spacer"></span><span class="faint small num" id="catTotal"></span></div>
      <div class="od-filters"><div class="seg" role="group" aria-label="Show">${[["all", "All"], ["not_added", "Not added"], ["chosen", "Added"]].map(([k, l]) => `<button type="button" data-catfilter="${k}" aria-pressed="${(S.cat.filter || "all") === k}">${l}</button>`).join("")}</div></div>
      <div class="od-list" id="catList"></div>
      <div class="od-foot" id="catFoot"></div>
    </section>
    <section class="card od-pane" aria-label="Content">
      <div class="od-head"><h2>Content</h2><span class="faint small od-sub" style="margin-left:6px">in Channels DVR</span><span class="spacer"></span><span class="faint small num" id="conTotal"></span></div>
      <div class="od-filters"><div class="seg" role="group" aria-label="Show">${[["all", "All"], ["picked", "Hand-picked"], ["add_all", "Add all"], ["pending", "Pending"], ["duplicates", "Duplicates"], ["draft", "Unsaved"]].map(([k, l]) => `<button type="button" data-cfilter="${k}" aria-pressed="${S.con.filter === k}">${l}</button>`).join("")}</div></div>
      <div class="od-list" id="conList"></div>
      <div class="od-foot" id="conFoot"></div>
    </section>
  </div>`;
}

// ---------- Library status ----------
function drawStatus() {
  const el = $("#odStatus", root);
  if (!el || !S.frame) return;
  el.innerHTML = providers().map((p) => statusHtml(p)).join("");
}
function statusHtml(p) {
  const lib = p.library;
  const who = multi() ? `<b>${esc(p.name)}:</b> ` : "";
  if (!p.enabled) return `<div class="callout small od-callout">${icon("info")}<div>${who}This provider is switched off on the Providers page. Its titles stay in Channels DVR; nothing new is added until it is switched on.</div></div>`;
  if (!lib) return "";
  if (!lib.imports_ok) return `<div class="callout small od-callout">${icon("alert")}<div>${who}${esc(lib.imports_message)}</div></div>`;
  const bits = [];
  if (lib.movies != null) bits.push(`In Channels DVR: ${fmtInt(lib.movies)} movies, ${fmtInt(lib.shows || 0)} shows (${fmtInt(lib.episodes || 0)} episodes)`);
  if (lib.matching) bits.push(`${fmtInt(lib.matching)} still getting TMDB details`);
  if (lib.last_sync) bits.push(`synced ${fmtAgo(new Date(lib.last_sync * 1000).toISOString())}`);
  if (lib.series_left) bits.push(`${fmtInt(lib.series_left)} series still to read, next batch soon`);
  if (!p.has_catalog && !lib.running) bits.push("the provider's list has not been read yet");
  const running = lib.running ? `<span class="badge badge-info od-running"><span class="dot pulse"></span>${esc(lib.running)}</span>` : "";
  const err = lib.error ? `<div class="small" style="color:var(--bad);margin-top:6px">${who}${esc(lib.error)}</div>` : "";
  const note = lib.dvr_note ? `<div class="small" style="color:var(--warn);margin-top:6px">${esc(lib.dvr_note)}</div>` : "";
  return `<div class="card od-status"><div class="row row-wrap small" style="gap:8px">${running}<span class="faint" style="flex:1;min-width:0">${who}${bits.join(" · ") || "Nothing in Channels DVR yet."}</span><button class="btn btn-sm" data-act="sync" data-provider="${esc(p.id)}">${icon("refresh")}Sync now</button></div>${err}${note}</div>`;
}

// ---------- Unsaved changes ----------
function drawDraft() {
  const el = $("#odDraft", root);
  if (!el) return;
  const { add, remove } = counts();
  if (!add && !remove) { el.innerHTML = ""; return; }
  const parts = [];
  if (add) parts.push(`<b>${fmtInt(add)}</b> to add`);
  if (remove) parts.push(`<b>${fmtInt(remove)}</b> to remove`);
  el.innerHTML = `<div class="selection-bar pending-bar od-draft">${icon("film")}<span>${parts.join(", ")}.<span class="od-draft-hint"> Kept until you Save or Dismiss; nothing reaches Channels DVR until you press Save.</span><span id="odDupNote"></span></span>
    <span class="spacer"></span>
    <button class="btn btn-sm btn-ghost" data-act="show-draft">Show</button>
    <button class="btn btn-sm btn-ghost" data-act="dismiss">Dismiss</button>
    <button class="btn btn-sm btn-primary" data-act="save">${icon("check")}Save</button></div>`;
  const dups = draft.duplicates || 0;
  $("#odDupNote", root).innerHTML = dups && add ? ` <span class="od-dupnote">${fmtInt(dups)} of the titles to add ${dups === 1 ? "is another copy" : "are other copies"} of a title already chosen: Channels DVR lists them as one title, and each copy stays in its own collection.</span>` : "";
}

// ---------- Groups ----------
function groupsHtml() {
  const kinds = S.kind ? [S.kind] : ["movies", "series"];
  const ps = providers().filter((p) => !S.provider || p.id === S.provider);
  let out = `<button class="od-group ${S.group ? "" : "on"}" data-group=""><span class="ellip">All groups</span></button>`;
  for (const kind of kinds) {
    const rows = ps.flatMap((p) => (p.groups[kind] || []).map((g) => ({ ...g, provider: p.id })));
    if (!rows.length) continue;
    out += `<div class="od-group-kind">${KINDS[kind]}</div>`;
    out += rows.map((g) => `<div class="od-group-row ${S.group === g.name ? "on" : ""}">
        <button class="od-group" data-group="${esc(g.name)}" data-pop="${esc(g.name)}" data-pop-sub="${esc(`${fmtInt(g.count)} titles${g.add_all ? " · Add all is on" : g.picked ? ` · ${fmtInt(g.picked)} hand-picked` : ""}${multi() ? " · " + pname(g.provider) : ""}`)}">
          <span class="ellip">${esc(g.name)}</span><span class="faint small num">${fmtInt(g.count)}</span></button>
        <span class="od-addall" title="Add all: every title in this group, now and later">${switchHtml("", g.add_all, "", `data-addall data-provider="${esc(g.provider)}" data-kind="${kind}" data-name="${esc(g.name)}" data-count="${g.count}" data-picked="${g.picked || 0}" aria-label="Add all of ${esc(g.name)}"`)}</span>
      </div>`).join("");
  }
  return `<div class="od-head"><h2>Groups</h2><span class="spacer"></span><span class="faint small">Add all</span></div><div class="od-group-list">${out}</div>`;
}
function drawGroups() {
  const el = $("#odGroups", root);
  if (el) el.innerHTML = groupsHtml();
}
async function toggleAddAll(input) {
  const { provider, kind, name } = input.dataset;
  const on = input.checked;
  const count = Number(input.dataset.count) || 0;
  const per = S.frame?.per_minute || 150;
  const what = kind === "movies" ? "movies" : "shows";
  let ok;
  if (on) {
    const picked = Number(input.dataset.picked) || 0;
    ok = await confirmDialog({
      title: `Add all of ${name}?`,
      message: `All ${fmtInt(count)} ${what} in ${name} go to Channels DVR, and so does everything the provider adds to this group later. At about ${per} titles a minute that takes ${estimate(count, per)}, and Channels DVR is slower while it runs. ${picked ? `The ${fmtInt(picked)} hand-picked ${picked === 1 ? "title in it becomes" : "titles in it become"} part of the group. ` : ""}Unsaved changes in this group are dropped.`,
      confirm: "Add all",
    });
  } else {
    ok = await confirmDialog({
      title: `Turn off Add all for ${name}?`,
      message: `Every title from ${name} leaves Channels DVR, including any you picked by hand before Add all was on, along with its watch history. Adding the group back later takes ${estimate(count, per)}.`,
      confirm: "Turn off and remove", danger: true,
    });
  }
  if (!ok) { input.checked = !on; return; }
  input.disabled = true;
  try {
    const r = await api.ondemandAddAll({ provider, kind, group: name, on });
    if (r.dropped) await loadDraft();   // the server dropped the unsaved changes inside this group
    toast(on ? `Add all is on for ${name}` : `Add all is off for ${name}`, on ? "Channels DVR fills in as coax writes the titles. Hand-picked titles still go first." : "Its titles are being removed from Channels DVR.", "ok");
    await loadFrame();
    drawStatus(); drawGroups(); drawDraft();
    loadBoth();
  } catch (e) { input.checked = !on; toastError("Could not change Add all", e); }
  finally { input.disabled = false; }
}
function estimate(count, per) {
  const minutes = count / per;
  if (minutes < 1.5) return "about a minute";
  if (minutes < 90) return `about ${Math.round(minutes)} minutes`;
  const hours = minutes / 60;
  if (hours < 36) return `roughly ${Math.round(hours)} hours`;
  return `roughly ${Math.round(hours / 24)} days`;
}
function groupsSheet() {
  const m = openModal({ title: "Groups", body: `<div class="od-sheet">${groupsHtml()}</div>`, foot: `<button class="btn" data-close>Done</button>` });
  m.el.addEventListener("click", (e) => {
    const g = e.target.closest("[data-group]");
    if (g) { setGroup(g.dataset.group); m.close(); }
  });
  m.el.addEventListener("change", (e) => { if (e.target.matches("[data-addall]")) { m.close(); toggleAddAll(e.target); } });
}
function setGroup(name) {
  S.group = name;
  S.cat.offset = S.con.offset = 0;
  render();
  loadBoth();
}

// ---------- Rows ----------
function tags(r) {
  const out = [];
  if (r.via === "all") out.push(`<span class="badge" data-pop="Add all" data-pop-sub="This group's Add all is on: every title in it is in Channels DVR. Turn it off in Groups to remove them." data-pop-always>Add all</span>`);
  else if (r.via === "pick") out.push(`<span class="badge badge-copper">Hand-picked</span>`);
  if (r.in_channels && r.draft !== "remove") out.push(`<span class="badge badge-ok">${icon("check", 'style="width:11px;height:11px"')}In Channels</span>`);
  if (r.pending && r.draft !== "remove") out.push(`<span class="badge badge-info" data-pop="Pending" data-pop-sub="Saved. coax is adding it to Channels DVR (hand-picked titles go first)." data-pop-always>Pending</span>`);
  if (r.draft === "add") out.push(`<span class="badge badge-accent">To add</span>`);
  if (r.draft === "remove") out.push(`<span class="badge badge-bad">To remove</span>`);
  if (r.duplicate) out.push(`<span class="badge badge-warn" data-pop="Duplicate" data-pop-sub="Another copy of this title is also chosen. Channels DVR lists them as one title; each copy stays in its own collection." data-pop-always>Duplicate</span>`);
  return out.join("");
}
function rowHtml(which, r, i) {
  const p = S[which];
  const k = rowKey(r);
  const can = selectable(r);
  const on = can && p.selected.has(k);
  const sub = [r.kind === "movie" ? "Movie" : "Show", r.year, r.groups.join(", "), multi() ? pname(r.provider) : ""].filter(Boolean).join(" · ");
  return `<div class="od-row ${on ? "on" : ""} ${can ? "" : "locked"}" data-k="${esc(k)}" data-idx="${i}" ${can ? `role="checkbox" aria-checked="${on}" tabindex="0"` : `aria-disabled="true"`}>
    ${can ? checkHtml(on, 'tabindex="-1" aria-hidden="true"') : `<span class="od-lock" aria-hidden="true"></span>`}
    <span class="od-main" data-pop="${esc(r.name)}${r.year ? ` (${esc(r.year)})` : ""}" data-pop-sub="${esc(sub)}"><span class="ellip od-name">${esc(r.name)}${r.year ? ` <span class="faint">(${esc(r.year)})</span>` : ""}</span><span class="ellip faint small">${esc(sub)}</span></span>
    <span class="od-tags"><span class="badge od-q od-q-${esc(r.quality.toLowerCase())}">${esc(r.quality)}</span>${tags(r)}</span></div>`;
}
function drawPane(which) {
  const list = $(`#${which}List`, root), foot = $(`#${which}Foot`, root), tot = $(`#${which}Total`, root);
  if (!list) return;
  const p = S[which];
  if (tot) tot.textContent = p.total ? `${fmtInt(p.total)} titles` : "";
  if (p.loading && !p.items.length) {
    list.innerHTML = Array.from({ length: 8 }, (_, i) => `<div class="od-row"><span class="skel" style="width:18px;height:18px"></span><span style="flex:1"><span class="skel" style="width:${45 + ((i * 23) % 40)}%"></span></span></div>`).join("");
  } else if (p.error) {
    list.innerHTML = `<div class="empty" style="padding:32px 16px">${icon("alert", 'style="width:28px;height:28px;color:var(--bad)"')}<p>${esc(p.error)}</p><button class="btn btn-sm" data-act="retry-pane" data-pane="${which}">Try again</button></div>`;
  } else if (!p.items.length) {
    list.innerHTML = emptyPane(which);
  } else {
    list.classList.toggle("busy", p.loading);
    const can = p.items.filter(selectable);
    const all = can.length > 0 && can.every((r) => p.selected.has(rowKey(r)));
    list.innerHTML = `<div class="od-row od-all">${can.length ? `<label class="row" style="gap:8px">${checkHtml(all, `data-page-all="${which}" aria-label="Select this page"`)}<span class="faint small">Select this page</span></label>` : `<span class="faint small">Add all titles cannot be picked one by one.</span>`}
      <span class="spacer"></span>${p.total > can.length || p.total > PER_PAGE ? `<button class="btn btn-sm btn-ghost" data-act="select-matching" data-pane="${which}">Select all that match</button>` : ""}</div>` +
      p.items.map((r, i) => rowHtml(which, r, i)).join("");
  }
  if (foot) foot.innerHTML = footHtml(which);
}
function emptyPane(which) {
  const filtered = S.q || S.group || S.provider || S.kind || (S[which].filter && S[which].filter !== "all");
  if (filtered) return `<div class="empty" style="padding:36px 16px"><h3>Nothing matches</h3><p class="small">No title fits these filters.</p><button class="btn btn-sm" data-act="clear-filters">Clear filters</button></div>`;
  if (which === "cat") {
    const reading = providers().some((p) => !p.has_catalog);
    return `<div class="empty" style="padding:36px 16px"><h3>${reading ? "Reading the provider's list" : "No titles"}</h3><p class="small">${reading ? "coax is reading the provider's movies and series. This takes a minute or two the first time." : "The available groups have no titles. Make other groups available on the Providers page."}</p>${reading ? "" : `<a class="btn btn-sm" href="#/providers">Open Providers</a>`}</div>`;
  }
  return `<div class="empty" style="padding:36px 16px"><h3>Nothing in Channels DVR yet</h3><p class="small">Tick titles in the Catalog and press Add, or switch on Add all for a group.</p></div>`;
}
function footHtml(which) {
  const p = S[which];
  const n = p.selected.size;
  const from = p.total ? p.offset + 1 : 0, to = Math.min(p.offset + PER_PAGE, p.total);
  const last = Math.max(0, Math.floor((p.total - 1) / PER_PAGE) * PER_PAGE);
  const pager = `<div class="pager" style="border:0;padding:0 0 10px">
      <span class="num">${fmtInt(from)}–${fmtInt(to)}</span><span class="faint">of</span><span class="num">${fmtInt(p.total)}</span><span class="spacer"></span>
      <button class="btn btn-sm btn-icon" data-page="first" data-pane="${which}" ${p.offset <= 0 ? "disabled" : ""} aria-label="First page">${icon("chevron", 'style="transform:rotate(90deg)"')}${icon("chevron", 'style="transform:rotate(90deg);margin-left:-10px"')}</button>
      <button class="btn btn-sm btn-icon" data-page="prev" data-pane="${which}" ${p.offset <= 0 ? "disabled" : ""} aria-label="Previous page">${icon("chevron", 'style="transform:rotate(90deg)"')}</button>
      <button class="btn btn-sm btn-icon" data-page="next" data-pane="${which}" ${to >= p.total ? "disabled" : ""} aria-label="Next page">${icon("chevron", 'style="transform:rotate(-90deg)"')}</button>
      <button class="btn btn-sm btn-icon" data-page="last" data-pane="${which}" data-last="${last}" ${p.offset >= last ? "disabled" : ""} aria-label="Last page">${icon("chevron", 'style="transform:rotate(-90deg)"')}${icon("chevron", 'style="transform:rotate(-90deg);margin-left:-10px"')}</button></div>`;
  const vals = [...p.selected.values()];
  if (which === "cat") {
    const toAdd = vals.filter((r) => !r.via && r.draft !== "add").length;
    const toRemove = vals.filter((r) => r.via === "pick" && r.draft !== "remove").length;
    const undo = vals.filter((r) => r.draft).length;
    return `${pager}<div class="row row-wrap" style="gap:8px">${n ? `<span class="faint small">${fmtInt(n)} selected</span><button class="btn btn-sm btn-ghost" data-act="clear-sel" data-pane="cat">Clear</button>` : ""}<span class="spacer"></span>
      ${undo ? `<button class="btn btn-sm" data-act="undo" data-pane="cat">Undo ${fmtInt(undo)}</button>` : ""}
      ${toRemove ? `<button class="btn btn-sm btn-danger" data-act="remove" data-pane="cat">${icon("trash")}Remove ${fmtInt(toRemove)}</button>` : ""}
      <button class="btn btn-primary" data-act="add" ${toAdd ? "" : "disabled"}>${icon("plus")}Add ${toAdd ? fmtInt(toAdd) + " " : ""}selected</button></div>`;
  }
  const toRemove = vals.filter((r) => r.via === "pick" && r.draft !== "remove").length;
  const undo = vals.filter((r) => r.draft).length;
  return `${pager}<div class="row row-wrap" style="gap:8px">${n ? `<span class="faint small">${fmtInt(n)} selected</span><button class="btn btn-sm btn-ghost" data-act="clear-sel" data-pane="con">Clear</button>` : ""}<span class="spacer"></span>
    ${undo ? `<button class="btn btn-sm" data-act="undo" data-pane="con">Undo ${fmtInt(undo)}</button>` : ""}
    <button class="btn btn-danger" data-act="remove" data-pane="con" ${toRemove ? "" : "disabled"}>${icon("trash")}Remove ${toRemove ? fmtInt(toRemove) + " " : ""}selected</button></div>`;
}

// ---------- Changes ----------
function applyToSelection(which, how) {
  const p = S[which];
  let n = 0;
  for (const r of p.selected.values()) {
    const cur = changeOf(r.provider, r.key);
    if (how === "add" && !r.via && cur !== "add") { setChange(r.provider, r.key, "add"); n++; }
    else if (how === "remove" && r.via === "pick" && cur !== "remove") { setChange(r.provider, r.key, "remove"); n++; }
    else if (how === "undo" && cur) { setChange(r.provider, r.key, ""); n++; }
  }
  p.selected.clear();
  saveDraft();
  drawDraft();
  loadBoth();
  return n;
}
async function selectMatching(which) {
  const p = S[which];
  const st = S;
  try {
    const r = await api.ondemandTitles({ ...params(which), keys: 1, offset: 0 });
    if (S !== st) return;
    if (!r.keys.length) { toast("Nothing to select", "These titles come from groups with Add all on; turn Add all off to pick them one by one.", "info"); return; }
    // Rows of other pages are known by key only; enough to add or remove them.
    const known = new Map(p.items.map((x) => [rowKey(x), x]));
    for (const k of r.keys) {
      const key = `${k.provider}|${k.key}`;
      p.selected.set(key, known.get(key) || { provider: k.provider, key: k.key, via: k.via, draft: k.draft });
    }
    drawPane(which);
  } catch (e) { toastError("Could not select every match", e); }
}

// ---------- Wiring ----------
function wire() {
  root.onclick = onClick;
  root.onchange = onChange;
  root.onkeydown = (e) => { if ((e.key === " " || e.key === "Enter") && e.target.matches(".od-row[data-k]")) { e.preventDefault(); toggleRow(e.target, e.shiftKey); } };
  const q = $("#odQ", root);
  if (q) q.oninput = debounce(() => { S.q = q.value; S.cat.offset = S.con.offset = 0; S.cat.selected.clear(); S.con.selected.clear(); loadBoth(); }, 250);
  const pv = $("#odProv", root);
  if (pv) pv.onchange = () => { S.provider = pv.value; S.group = ""; S.cat.offset = S.con.offset = 0; render(); loadBoth(); };
}
function toggleRow(row, shift) {
  const which = row.closest("#catList") ? "cat" : "con";
  const p = S[which];
  const idx = Number(row.dataset.idx);
  const r = p.items[idx];
  if (!r || !selectable(r)) return;
  const on = !p.selected.has(rowKey(r));
  if (shift && p.last != null && p.last < p.items.length) {
    const [a, b] = [Math.min(p.last, idx), Math.max(p.last, idx)];
    for (let i = a; i <= b; i++) {
      const x = p.items[i];
      if (!selectable(x)) continue;
      on ? p.selected.set(rowKey(x), x) : p.selected.delete(rowKey(x));
    }
  } else on ? p.selected.set(rowKey(r), r) : p.selected.delete(rowKey(r));
  p.last = idx;
  drawPane(which);
  const again = $(`#${which}List [data-idx="${idx}"]`, root);
  if (again) again.focus({ preventScroll: true });
}
async function onClick(e) {
  const t = e.target;
  const kind = t.closest("[data-kind]");
  if (kind && kind.closest(".seg")) {
    S.kind = kind.dataset.kind; S.group = ""; S.cat.offset = S.con.offset = 0;
    try { localStorage.setItem("coax.ondemand.kind", S.kind); } catch {}
    render(); loadBoth(); return;
  }
  const kf = t.closest("[data-catfilter]");
  if (kf) { S.cat.filter = kf.dataset.catfilter; S.cat.offset = 0; S.cat.selected.clear(); render(); loadPane("cat"); return; }
  const cf = t.closest("[data-cfilter]");
  if (cf) { S.con.filter = cf.dataset.cfilter; S.con.offset = 0; S.con.selected.clear(); render(); loadPane("con"); return; }
  const g = t.closest("[data-group]");
  if (g && g.closest("#odGroups")) { setGroup(g.dataset.group); return; }
  const pg = t.closest("[data-page]");
  if (pg && !pg.disabled) {
    const p = S[pg.dataset.pane];
    const dir = pg.dataset.page;
    p.offset = dir === "first" ? 0 : dir === "prev" ? Math.max(0, p.offset - PER_PAGE) : dir === "next" ? p.offset + PER_PAGE : Number(pg.dataset.last) || 0;
    p.last = null;
    loadPane(pg.dataset.pane);
    return;
  }
  const row = t.closest(".od-row[data-k]");
  if (row && !t.closest("a,button")) { e.preventDefault(); toggleRow(row, e.shiftKey); return; }
  const a = t.closest("[data-act]");
  if (!a) return;
  const which = a.dataset.pane;
  switch (a.dataset.act) {
    case "retry": S.loading = true; render(); await loadFrame(); await loadDraft(); render(); loadBoth(); break;
    case "retry-pane": loadPane(which); break;
    case "clear-filters": S.q = ""; S.group = ""; S.provider = ""; S.kind = ""; S.con.filter = "all"; S.cat.filter = "all"; S.cat.offset = S.con.offset = 0; render(); loadBoth(); break;
    case "groups-sheet": groupsSheet(); break;
    case "sync": busy(a, async () => { try { await api.syncVod(a.dataset.provider); toast("Sync started", "Channels DVR picks the titles up as coax adds them.", "ok"); } catch (err) { toastError("Could not start the sync", err); } }); break;
    case "add": { const n = applyToSelection("cat", "add"); if (n) toast(`${fmtInt(n)} to add`, "Press Save to send them to Channels DVR.", "info"); break; }
    case "remove": { const n = applyToSelection(which, "remove"); if (n) toast(`${fmtInt(n)} to remove`, "Press Save to take them out of Channels DVR.", "info"); break; }
    case "undo": applyToSelection(which, "undo"); break;
    case "clear-sel": S[which].selected.clear(); drawPane(which); break;
    case "select-matching": busy(a, () => selectMatching(which)); break;
    case "show-draft": S.con.filter = "draft"; S.con.offset = 0; S.q = ""; S.group = ""; S.provider = ""; S.kind = ""; render(); loadBoth(); break;
    case "dismiss": dismiss(); break;
    case "save": busy(a, save); break;
  }
}
function onChange(e) {
  const t = e.target;
  if (t.matches("[data-addall]")) { toggleAddAll(t); return; }
  if (t.matches("[data-page-all]")) {
    const which = t.dataset.pageAll;
    const p = S[which];
    for (const r of p.items.filter(selectable)) t.checked ? p.selected.set(rowKey(r), r) : p.selected.delete(rowKey(r));
    drawPane(which);
  }
}
async function dismiss() {
  const { add, remove } = counts();
  const ok = await confirmDialog({ title: "Dismiss the unsaved changes?", message: `${add ? `${fmtInt(add)} to add` : ""}${add && remove ? " and " : ""}${remove ? `${fmtInt(remove)} to remove` : ""} will be forgotten. Nothing in Channels DVR changes.`, confirm: "Dismiss", danger: true });
  if (!ok || !S) return;
  S.pending = {};
  saveDraft();
  drawDraft();
  loadBoth();
}
async function save() {
  // The last change may still be on its way to the server; Save works on the server's copy.
  for (let i = 0; i < 50 && (draft.busy || JSON.stringify(S.pending) !== draft.sent); i++) {
    if (!draft.busy) saveDraft();
    await new Promise((r) => setTimeout(r, 100));
  }
  if (JSON.stringify(S.pending) !== draft.sent) { toast("Not saved yet", "The changes are still being kept on the server. Try again in a moment.", "warn"); return; }
  try {
    const r = await api.ondemandSave({ rev: draft.rev });
    if (!S) return;
    draft.rev = r.rev; draft.sent = "{}"; S.pending = {};
    const bits = [];
    if (r.added) bits.push(`${fmtInt(r.added)} added`);
    if (r.removed) bits.push(`${fmtInt(r.removed)} removed`);
    toast(bits.length ? `Saved: ${bits.join(", ")}` : "Saved", [r.added ? "Hand-picked titles go to Channels DVR ahead of any Add all import, usually within a minute." : "", ...(r.notes || [])].filter(Boolean).join(" "), "ok", 7000);
    S.cat.selected.clear(); S.con.selected.clear();
    await loadFrame();
    drawStatus(); drawGroups(); drawDraft();
    loadBoth();
  } catch (e) {
    if (e?.status === 409) { await loadDraft(); render(); loadBoth(); toast("Not saved", "The changes were edited in another window; showing the latest. Check them and press Save again.", "warn"); }
    else toastError("Could not save", e);
  }
}
