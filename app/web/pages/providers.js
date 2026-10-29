// Providers: Xtream or M3U sources, each with one or more lines (connection pools), test, refresh, group picker.
import { api, ApiError } from "../api.js";
import { $, $$, esc, icon, toast, toastError, openModal, confirmDialog, emptyState, skeletonCards, busy, switchHtml, checkHtml, fmtAgo, fmtInt, debounce } from "../ui.js";
import { invalidate, normalizeGroups } from "../state.js";

let root, providers = [];

export async function mount(el) {
  root = el;
  root.innerHTML = `
    <div class="page-head">
      <div><div class="eyebrow">Sources</div><h1>Providers</h1>
      <p class="sub">Xtream Codes or M3U playlists. Add several lines per provider to pool their connections.</p></div>
      <div class="page-actions"><button class="btn btn-primary" data-act="add">${icon("plus")}Add provider</button></div>
    </div>
    <div class="grid" id="pList">${skeletonCards(2, 200)}</div>`;
  root.onclick = onClick;
  await load();
}
export function unmount() {}

async function load() {
  try {
    providers = await api.listProviders();
  } catch (e) {
    toastError("Could not load providers", e);
    providers = [];
  }
  render();
}

function render() {
  const list = $("#pList", root);
  if (!providers.length) {
    list.innerHTML = `<div class="card">${emptyState({
      title: "No providers yet",
      text: "Add your IPTV service with its Xtream Codes login, or paste an M3U playlist URL. You can add more lines later to pool connections.",
      actions: `<button class="btn btn-primary" data-act="add">${icon("plus")}Add provider</button>`,
    })}</div>`;
    return;
  }
  list.innerHTML = providers.map(cardHtml).join("");
}

function cardHtml(p) {
  const lines = p.lines || [];
  const slots = lines.reduce((a, l) => (a === Infinity || !Number(l.max_streams) ? Infinity : a + Number(l.max_streams)), 0);
  const cat = p.catalog || {};
  const r = p.refresh || {};
  const state = r.state === "running" ? `<span class="badge badge-info"><span class="dot"></span>Refreshing</span>`
    : r.state === "error" || r.ok === false ? `<span class="badge badge-bad" title="${esc(r.error || "")}"><span class="dot"></span>Refresh failed</span>`
    : cat.fetched_at ? `<span class="badge badge-ok"><span class="dot"></span>Catalog ready</span>`
    : `<span class="badge badge-warn"><span class="dot"></span>Not fetched</span>`;
  const host = p.type === "m3u" ? p.m3u_url : p.server;
  return `<article class="card provider-card" data-id="${esc(p.id)}">
    <div class="provider-top">
      <div class="provider-glyph ${p.type === "m3u" ? "m3u" : ""}">${icon(p.type === "m3u" ? "lineups" : "signal")}</div>
      <div style="flex:1;min-width:0">
        <div class="row row-wrap" style="gap:8px"><span class="provider-title">${esc(p.name)}</span>
          <span class="badge">${p.type === "m3u" ? "M3U" : "Xtream"}</span><span class="badge">${esc((p.format || "hls").toUpperCase())}</span>${state}</div>
        <div class="faint small mono ellip" style="margin-top:4px">${esc(redact(host || ""))}</div>
        ${r.state === "error" && r.error ? `<div class="small" style="color:var(--bad);margin-top:6px">${esc(r.error)}</div>` : ""}
        ${r.vanished_count ? `<div class="small" style="color:var(--warn);margin-top:6px">${fmtInt(r.vanished_count)} streams vanished in the last refresh. Affected channels are flagged on the Channels page.</div>` : ""}
      </div>
      <div class="row" style="flex:none">
        <button class="btn btn-sm" data-act="test">${icon("plug")}Test</button>
        <button class="btn btn-sm" data-act="refresh">${icon("refresh")}Refresh</button>
        <button class="btn btn-sm" data-act="groups">${icon("filter")}Groups</button>
        <button class="btn btn-sm btn-ghost btn-icon" data-act="edit" aria-label="Edit ${esc(p.name)}">${icon("edit")}</button>
        <button class="btn btn-sm btn-ghost btn-icon btn-danger" data-act="delete" aria-label="Delete ${esc(p.name)}">${icon("trash")}</button>
      </div>
    </div>
    <div class="provider-stats">
      <div><div class="stat-label">Lines</div><div class="stat-value">${lines.length}</div></div>
      <div><div class="stat-label">Max streams</div><div class="stat-value">${slots === Infinity ? "∞" : slots}</div></div>
      <div><div class="stat-label">Groups selected</div><div class="stat-value">${(p.groups || []).length}<span class="faint" style="font-size:12px"> / ${fmtInt(cat.groups ?? 0)}</span></div></div>
      <div><div class="stat-label">Catalog</div><div class="stat-value">${fmtInt(cat.streams ?? 0)}</div><div class="faint small">${cat.fetched_at ? `updated ${fmtAgo(cat.fetched_at)}` : "never refreshed"} · every ${esc(p.refresh_hours ?? 24)} h</div></div>
    </div>
  </article>`;
}
const redact = (u) => u.replace(/(username|password)=[^&]*/gi, "$1=…");

async function onClick(e) {
  const a = e.target.closest("[data-act]");
  if (!a) return;
  const card = a.closest("[data-id]");
  const p = card ? providers.find((x) => x.id === card.dataset.id) : null;
  switch (a.dataset.act) {
    case "add": editModal(null); break;
    case "edit": editModal(p); break;
    case "groups": groupsModal(p); break;
    case "test":
      busy(a, async () => {
        try { showTest(await api.testProvider(p.id)); } catch (err) { toastError(`Test failed: ${p.name}`, err); }
      });
      break;
    case "refresh":
      busy(a, async () => {
        try {
          const r = await api.refreshProvider(p.id);
          if (r && (r.ok === false || r.state === "error")) toast(`Refresh failed: ${p.name}`, r.error || "The old catalog is kept.", "bad");
          else toast(`Refreshed ${p.name}`, r ? `${fmtInt(r.streams)} streams, ${fmtInt(r.added || 0)} new, ${fmtInt(r.vanished_count || 0)} vanished` : "", "ok");
          invalidate("providers");
          await load();
        } catch (err) { toastError(`Refresh failed: ${p.name}`, err); }
      });
      break;
    case "delete": {
      const ok = await confirmDialog({ title: `Delete ${p.name}?`, message: "Its catalog cache is removed. Channels keep their other streams.", confirm: "Delete", danger: true });
      if (!ok) return;
      let r;
      try {
        r = await api.deleteProvider(p.id);
      } catch (err) {
        if (err instanceof ApiError && err.status === 409) {
          const force = await confirmDialog({ title: "Provider is in use", message: `${err.message}. Remove its streams from those channels and delete anyway? Channels themselves are kept.`, confirm: "Delete anyway", danger: true });
          if (!force) return;
          try { r = await api.deleteProvider(p.id, true); } catch (e2) { toastError("Delete failed", e2); return; }
        } else { toastError("Delete failed", err); return; }
      }
      const stuck = r?.only_disabled_streams || [];
      if (stuck.length) toast("Provider deleted", `These channels now have only disabled streams and will not play until you enable one: ${stuck.join(", ")}`, "warn", 12000);
      else toast("Provider deleted", p.name, "ok");
      invalidate("providers");
      await load();
      break;
    }
  }
}

function showTest(r) {
  if (!r || r.ok === false) { toast("Connection failed", r?.error || r?.message || "No details", "bad"); return; }
  const bits = [];
  if (r.status) bits.push(`status ${r.status}`);
  if (r.max_connections != null) bits.push(`${r.active_cons ?? 0}/${r.max_connections} connections`);
  if (r.exp_date) { const d = Number(r.exp_date) ? new Date(Number(r.exp_date) * 1000) : new Date(r.exp_date); if (!isNaN(d)) bits.push(`expires ${d.toLocaleDateString()}`); }
  if (r.streams != null) bits.push(`${fmtInt(r.streams)} streams`);
  toast("Connection OK", r.message || bits.join(" · "), "ok", 6000);
}

// ---------- Add / edit ----------
function blankLine(i, type) { return { name: `Line ${i}`, username: "", password: "", max_streams: type === "m3u" ? 0 : 1, search: "", replace: "" }; }

function editModal(p) {
  const d = p ? JSON.parse(JSON.stringify(p)) : { name: "", type: "xtream", server: "", m3u_url: "", xmltv_url: "", user_agent: "", format: "hls", refresh_hours: 24, max_streams: 0, auto_add: false, lines: [blankLine(1, "xtream")] };
  if (!d.lines?.length) d.lines = [blankLine(1, d.type)];
  const m = openModal({
    title: p ? `Edit ${p.name}` : "Add provider",
    wide: true,
    body: `<form id="pForm" class="stack" novalidate>
      <div class="row row-wrap"><div class="seg" role="group" aria-label="Type">
        <button type="button" class="seg-accent" data-type="xtream" aria-pressed="${d.type === "xtream"}">Xtream Codes</button>
        <button type="button" class="seg-accent" data-type="m3u" aria-pressed="${d.type === "m3u"}">M3U playlist</button></div></div>
      <div class="form-grid">
        <div class="field"><label for="fName">Name</label><input class="input" id="fName" value="${esc(d.name)}" placeholder="My provider" required autofocus></div>
        <div class="field" data-x><label for="fServer">Server URL</label><input class="input mono" autocomplete="off" autocorrect="off" autocapitalize="off" spellcheck="false" id="fServer" value="${esc(d.server)}" placeholder="http://host:80"></div>
        <div class="field" data-m><label for="fM3u">M3U URL</label><input class="input mono" autocomplete="off" autocorrect="off" autocapitalize="off" spellcheck="false" id="fM3u" value="${esc(d.m3u_url)}" placeholder="http://host/playlist.m3u"></div>
        <div class="field"><label for="fXmltv">XMLTV URL <span class="faint">(optional)</span></label><input class="input mono" autocomplete="off" autocorrect="off" autocapitalize="off" spellcheck="false" id="fXmltv" value="${esc(d.xmltv_url)}" placeholder="Xtream: built from the first line"></div>
        <div class="field"><label for="fFormat">Stream format</label><select class="input" id="fFormat"><option value="hls" ${d.format !== "ts" ? "selected" : ""}>HLS (recommended)</option><option value="ts" ${d.format === "ts" ? "selected" : ""}>MPEG-TS</option></select><span class="hint">Use HLS if your provider supports it: segments can be retried and caught up, so stalls and drops recover far better than with a single MPEG-TS connection.</span></div>
        <div class="field"><label for="fMax">Total stream limit</label><input class="input mono" type="number" min="0" id="fMax" value="${esc(d.max_streams ?? 0)}"><span class="hint">Most streams this provider allows at once across all its lines. 0 means only the per-line limits apply.</span></div>
        <div class="field"><label for="fRefresh">Refresh every (hours)</label><input class="input mono" autocomplete="off" autocorrect="off" autocapitalize="off" spellcheck="false" id="fRefresh" type="number" min="0" max="720" value="${esc(d.refresh_hours ?? 24)}"><span class="hint">0 means refresh only when you press Refresh.</span></div>
        <div class="field full">${switchHtml("", d.auto_add, "Automatically add new streams in selected groups as disabled channels", 'id="fAuto"')}</div>
      </div>
      <hr class="divider" style="margin:4px 0">
      <div class="row"><div><div style="font-weight:620">Lines</div><div class="faint small" data-x>Separate logins on the same service. Each adds its own connection slots.</div><div class="faint small" data-m>Optional URL rewrite per line (regex search and replace). 0 max streams means unlimited.</div></div><span class="spacer"></span><button type="button" class="btn btn-sm" data-addline>${icon("plus")}Add line</button></div>
      <div class="lines-list" id="fLines"></div>
    </form>`,
    foot: `<button class="btn" data-test style="margin-right:auto">${icon("plug")}Test connection</button><button class="btn" data-close>Cancel</button><button class="btn btn-primary" data-save>${p ? "Save" : "Add provider"}</button>`,
  });
  const el = m.el;
  const syncType = () => {
    $$("[data-x]", el).forEach((x) => (x.style.display = d.type === "xtream" ? "" : "none"));
    $$("[data-m]", el).forEach((x) => (x.style.display = d.type === "m3u" ? "" : "none"));
    $$("[data-type]", el).forEach((b) => b.setAttribute("aria-pressed", b.dataset.type === d.type));
    drawLines();
  };
  const readLines = () => $$(".line-row", el).forEach((row, i) => {
    const l = d.lines[i];
    $$("[data-f]", row).forEach((inp) => (l[inp.dataset.f] = inp.type === "number" ? Number(inp.value) || 0 : inp.value));
  });
  const drawLines = () => {
    $("#fLines", el).innerHTML = d.lines.map((l, i) => d.type === "xtream" ? `<div class="line-row">
        <div class="field"><label>Name</label><input class="input input-sm" data-f="name" value="${esc(l.name)}"></div>
        <div class="field"><label>Server <span class="faint">(optional)</span></label><input class="input input-sm mono" data-f="server" value="${esc(l.server || "")}" placeholder="Same as provider" autocomplete="off" autocorrect="off" autocapitalize="off" spellcheck="false" title="Only if this line uses a different address than the provider"></div>
        <div class="field"><label>Username</label><input class="input input-sm mono" data-f="username" value="${esc(l.username)}" autocomplete="off"></div>
        <div class="field"><label>Password</label><input class="input input-sm mono" type="password" data-f="password" value="${esc(l.password)}" autocomplete="new-password"></div>
        <div class="field"><label>Max streams</label><input class="input input-sm mono" type="number" min="0" data-f="max_streams" value="${esc(l.max_streams)}"></div>
        <button type="button" class="btn btn-ghost btn-sm btn-icon btn-danger" data-rmline="${i}" aria-label="Remove line" ${d.lines.length < 2 ? "disabled" : ""}>${icon("trash")}</button></div>`
      : `<div class="line-row m3u">
        <div class="field"><label>Name</label><input class="input input-sm" data-f="name" value="${esc(l.name)}"></div>
        <div class="field"><label>URL search (regex)</label><input class="input input-sm mono" data-f="search" value="${esc(l.search || "")}" placeholder="optional"></div>
        <div class="field"><label>Replace with</label><input class="input input-sm mono" data-f="replace" value="${esc(l.replace || "")}" placeholder="optional"></div>
        <div class="field"><label>Max streams</label><input class="input input-sm mono" type="number" min="0" data-f="max_streams" value="${esc(l.max_streams)}"></div>
        <button type="button" class="btn btn-ghost btn-sm btn-icon btn-danger" data-rmline="${i}" aria-label="Remove line" ${d.lines.length < 2 ? "disabled" : ""}>${icon("trash")}</button></div>`).join("");
  };
  syncType();
  el.addEventListener("click", (e) => {
    const t = e.target.closest("[data-type]");
    if (t) { readLines(); d.type = t.dataset.type; syncType(); return; }
    if (e.target.closest("[data-addline]")) { readLines(); d.lines.push(blankLine(d.lines.length + 1, d.type)); drawLines(); $$(".line-row", el).pop().querySelector("input").focus(); return; }
    const rm = e.target.closest("[data-rmline]");
    if (rm) { readLines(); d.lines.splice(Number(rm.dataset.rmline), 1); drawLines(); }
  });
  const collect = () => {
    readLines();
    return {
      ...(p ? { id: p.id } : {}),
      name: $("#fName", el).value.trim(), type: d.type,
      server: $("#fServer", el).value.trim(), m3u_url: $("#fM3u", el).value.trim(), xmltv_url: $("#fXmltv", el).value.trim(),
      format: $("#fFormat", el).value, max_streams: Number($("#fMax", el).value) || 0, refresh_hours: $("#fRefresh", el).value === "" ? 24 : Number($("#fRefresh", el).value),
      auto_add: $("#fAuto", el).checked,
      lines: d.lines.map((l) => ({ ...(l.id ? { id: l.id } : {}), name: l.name, username: l.username || "", password: l.password || "", server: (l.server || "").trim(), max_streams: Number(l.max_streams) || 0, search: l.search || "", replace: l.replace || "" })),
    };
  };
  const validate = (b) => {
    if (!b.name) return "Give the provider a name.";
    if (b.type === "xtream" && !/^https?:\/\//.test(b.server)) return "Server URL must start with http:// or https://";
    if (b.type === "m3u" && !/^https?:\/\//.test(b.m3u_url)) return "M3U URL must start with http:// or https://";
    if (b.type === "xtream" && b.lines.some((l) => !l.username || !l.password)) return "Every line needs a username and password.";
    return null;
  };
  $("[data-test]", el).onclick = (e) => busy(e.currentTarget, async () => {
    const b = collect(); const bad = validate(b);
    if (bad) { toast("Check the form", bad, "warn"); return; }
    try { showTest(await api.testDraft(b)); } catch (err) { toastError("Test failed", err); }
  });
  $("[data-save]", el).onclick = (e) => busy(e.currentTarget, async () => {
    const b = collect(); const bad = validate(b);
    if (bad) { toast("Check the form", bad, "warn"); return; }
    try {
      const saved = p ? await api.updateProvider(p.id, b) : await api.createProvider(b);
      m.close();
      invalidate("providers");
      toast(p ? "Provider saved" : "Provider added", b.name, "ok");
      await load();
      if (!p && saved?.id) {
        toast("Fetching catalog", "Then pick the groups to bring in.", "info");
        try { await api.refreshProvider(saved.id); } catch (err) { toastError("First refresh failed", err); }
        await load();
        groupsModal(providers.find((x) => x.id === saved.id) || saved);
      }
    } catch (err) { toastError("Could not save provider", err); }
  });
}

// ---------- Group picker ----------
async function groupsModal(p) {
  const m = openModal({
    title: `Groups · ${p.name}`,
    wide: true,
    body: `<p class="muted small" style="margin-bottom:12px">Only streams in the groups you select show up in the channel catalog.</p>
      <div class="row row-wrap" style="margin-bottom:12px">
        <label class="search" style="flex:1;min-width:220px"><span class="sr-only">Search groups</span>${icon("search")}<input class="input" id="gQ" placeholder="Search groups" autofocus></label>
        <button class="btn btn-sm" data-all>Select all</button><button class="btn btn-sm" data-none>Select none</button>
        <span class="badge badge-accent num" id="gCount"></span></div>
      <div class="group-list" id="gList">${Array.from({ length: 8 }, () => `<div class="group-item"><span class="skel" style="width:18px;height:18px"></span><span class="skel" style="flex:1"></span></div>`).join("")}</div>`,
    foot: `<button class="btn" data-close>Cancel</button><button class="btn btn-primary" data-save>Save groups</button>`,
  });
  const el = m.el;
  let groups = [], sel = new Set();
  try {
    ({ groups, selected: sel } = normalizeGroups(await api.getGroups(p.id), p));
  } catch (err) {
    $("#gList", el).innerHTML = `<div class="empty" style="padding:28px">${icon("alert", 'style="width:28px;height:28px;color:var(--bad)"')}<p>${esc(err.message)}</p><p class="small faint">Refresh the provider to download its categories.</p></div>`;
    return;
  }
  const filtered = () => { const q = $("#gQ", el).value.trim().toLowerCase(); return q ? groups.filter((g) => g.name.toLowerCase().includes(q)) : groups; };
  const draw = () => {
    const list = filtered();
    $("#gCount", el).textContent = `${sel.size} of ${groups.length} selected`;
    $("#gList", el).innerHTML = list.length ? list.map((g) => `<label class="group-item">${checkHtml(sel.has(g.name), `data-g="${esc(g.name)}"`)}<span class="gname">${esc(g.name)}</span>${g.count != null ? `<span class="gcount">${fmtInt(g.count)}</span>` : ""}</label>`).join("")
      : `<div class="empty" style="padding:28px"><p>${groups.length ? "No groups match." : "No groups yet. Refresh the provider first."}</p></div>`;
  };
  draw();
  $("#gQ", el).addEventListener("input", debounce(draw, 80));
  $("#gList", el).addEventListener("change", (e) => { const g = e.target.dataset.g; if (g == null) return; e.target.checked ? sel.add(g) : sel.delete(g); $("#gCount", el).textContent = `${sel.size} of ${groups.length} selected`; });
  $("[data-all]", el).onclick = () => { filtered().forEach((g) => sel.add(g.name)); draw(); };
  $("[data-none]", el).onclick = () => { filtered().forEach((g) => sel.delete(g.name)); draw(); };
  $("[data-save]", el).onclick = (e) => busy(e.currentTarget, async () => {
    try {
      const ordered = groups.map((g) => g.name).filter((n) => sel.has(n));
      await api.setGroups(p.id, ordered);
      invalidate("providers");
      toast("Groups saved", `${ordered.length} group${ordered.length === 1 ? "" : "s"} selected`, "ok");
      m.close();
      await load();
    } catch (err) { toastError("Could not save groups", err); }
  });
}
