// Lineups: named profiles, each its own DVR custom source with its own M3U and XMLTV URLs and guide source.
import { api, routes } from "../api.js";
import { $, $$, esc, icon, toast, toastError, openModal, confirmDialog, emptyState, skeletonCards, busy, copyText, fmtInt } from "../ui.js";
import { invalidate, loadConfig } from "../state.js";

let root, lineups = [], channels = [], base = "";

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
      <div class="card-head"><div class="row" style="min-width:0"><h3 class="ellip">${esc(l.name)}</h3><span class="badge ${guideBadge(g)}">${esc(g)}</span></div>
        <div class="row"><button class="btn btn-ghost btn-sm btn-icon" data-act="edit" aria-label="Edit ${esc(l.name)}">${icon("edit")}</button><button class="btn btn-ghost btn-sm btn-icon btn-danger" data-act="delete" aria-label="Delete ${esc(l.name)}">${icon("trash")}</button></div></div>
      <div class="card-body stack" style="gap:12px">
        <div class="row" style="gap:24px"><div><div class="stat-label">Channels</div><div class="stat-value">${fmtInt(n)}</div></div><div><div class="stat-label">Enabled</div><div class="stat-value">${fmtInt(enabled)}</div></div><div><div class="stat-label">Id</div><div class="stat-value" style="font-size:14px;margin-top:5px">${esc(l.id)}</div></div></div>
        <div class="copy-field"><span class="tag">M3U</span><code title="${esc(m3u)}">${esc(m3u)}</code><button class="btn btn-ghost btn-sm btn-icon" data-copy="${esc(m3u)}" aria-label="Copy M3U URL">${icon("copy")}</button></div>
        <div class="copy-field"><span class="tag">XMLTV</span><code title="${esc(xml)}">${esc(xml)}</code><button class="btn btn-ghost btn-sm btn-icon" data-copy="${esc(xml)}" aria-label="Copy XMLTV URL">${icon("copy")}</button></div>
        <p class="faint small">${icon("info", 'style="width:13px;height:13px;vertical-align:-2px"')} ${hint}</p>
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
      ${l ? "" : `<label class="switch"><input type="checkbox" id="lAll"><span class="track"></span><span>Start with every channel</span></label>`}
      <p class="faint small">Add channels to lineups from the Channels page (the Lineups column or a bulk selection).</p></div>`,
    foot: `<button class="btn" data-close>Cancel</button><button class="btn btn-primary" data-save>${l ? "Save" : "Create lineup"}</button>`,
  });
  $$('input[name="lg"]', m.el).forEach((r) => (r.onchange = () => (guide = r.value)));
  $("[data-save]", m.el).onclick = (e) => busy(e.currentTarget, async () => {
    const name = $("#lName", m.el).value.trim();
    if (!name) { toast("Name is required", "", "warn"); return; }
    try {
      if (l) await api.updateLineup(l.id, { name, guide });
      else await api.createLineup({ name, guide, all_channels: $("#lAll", m.el).checked });
      invalidate("lineups");
      m.close();
      toast(l ? "Lineup saved" : "Lineup created", name, "ok");
      await load();
    } catch (err) { toastError("Could not save lineup", err); }
  });
}
