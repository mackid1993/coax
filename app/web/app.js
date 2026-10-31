import { initPopovers } from "./popover.js";
// coax web UI entry: router, theme, server health. Pages live in pages/*.js.
import { api } from "./api.js";
import { $, icon, esc, flag, openModal, toast, toastError } from "./ui.js";

const PAGES = [
  { id: "channels", label: "Channels", icon: "channels", load: () => import("./pages/channels.js") },
  { id: "ondemand", label: "On demand", icon: "film", load: () => import("./pages/ondemand.js") },
  { id: "providers", label: "Providers", icon: "providers", load: () => import("./pages/providers.js") },
  { id: "lineups", label: "Lineups", icon: "lineups", load: () => import("./pages/lineups.js") },
  { id: "settings", label: "Settings", icon: "settings", load: () => import("./pages/settings.js") },
  { id: "status", label: "Status", icon: "status", load: () => import("./pages/status.js") },
  { id: "logs", label: "Logs", icon: "logs", load: () => import("./pages/logs.js") },
];

let current = null; // {id, mod}

function renderNav(active) {
  $("#nav").innerHTML = PAGES.map((p) => `<a href="#/${p.id}" ${p.id === active ? 'aria-current="page"' : ""}>${icon(p.icon)}<span>${esc(p.label)}</span><span class="nav-badge" data-badge="${p.id}"></span></a>`).join("");
}

export function setBadge(id, text) {
  const el = document.querySelector(`[data-badge="${id}"]`);
  if (el) el.textContent = text ?? "";
}

async function route() {
  let id = (location.hash.replace(/^#\/?/, "").split(/[/?]/)[0] || "channels").toLowerCase();
  if (id === "guide") id = "channels"; // the guide lives on the channel screen now
  const page = PAGES.find((p) => p.id === id) || PAGES[0];
  renderNav(page.id);
  if (current && current.mod.unmount) {
    try { current.mod.unmount(); } catch (e) { console.error(e); }
  }
  const main = $("#main");
  main.innerHTML = "";
  const mod = await page.load();
  current = { id: page.id, mod };
  document.title = `${page.label} · coax`;
  const root = document.createElement("div");
  root.className = "page";
  main.appendChild(root);
  try {
    await mod.mount(root);
  } catch (e) {
    console.error(e);
  }
  main.focus({ preventScroll: true });
}

// ---------- Theme ----------
function applyThemeIcon() {
  const t = document.documentElement.dataset.theme;
  $("#themeToggle").innerHTML = icon(t === "dark" ? "sun" : "moon");
  document.querySelector('meta[name="theme-color"]')?.setAttribute("content", t === "dark" ? "#070A16" : "#F3F6FB");
}
$("#themeToggle").addEventListener("click", () => {
  const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem("coax.theme", next); } catch {}
  applyThemeIcon();
});
applyThemeIcon();

// ---------- Health pulse ----------
async function health() {
  const el = $("#health");
  try {
    const s = await api.status();
    el.className = "pulse-row ok";
    const n = (s.sessions || []).length;
    el.querySelector(".pulse-label").textContent = n ? `${n} live` : "Online";
    setBadge("status", n ? String(n) : "");
    if (s.channels) setBadge("channels", String(s.channels.total ?? ""));
  } catch {
    el.className = "pulse-row bad";
    el.querySelector(".pulse-label").textContent = "Offline";
  }
}
health();
setInterval(health, 10000);

// ---------- Notifications bell (feed health: removed and promoted streams) ----------
let notes = { items: [], unread: 0 };
async function pollNotes() {
  try { notes = await api.notifications(); } catch { return; }
  const n = $("#bell .bell-n");
  n.hidden = !notes.unread;
  n.textContent = notes.unread > 99 ? "99+" : String(notes.unread);
  $("#bell").classList.toggle("has", !!notes.unread);
}
const NOTE_KIND = { blocked: "Stream blocked", promoted: "New main stream", reordered: "Streams reordered",
  removed: "Stream removed", flagged: "Bad stream seen", desync: "Out of sync" };
function notesHtml() {
  if (!notes.items?.length) return `<p class="faint">Nothing yet. When coax removes a bad stream from a channel or makes a more reliable backup the main stream, it shows up here.</p>`;
  return `<ol class="notes">${notes.items.map((x) => `<li class="note ${x.read ? "" : "unread"} ${x.undone ? "undone" : ""}">
    <div class="note-head"><span class="badge ${x.kind === "promoted" ? "badge-ok" : "badge-warn"}">${esc(NOTE_KIND[x.kind] || x.kind)}</span><b>${esc(x.channel || "")}</b><span class="faint small">${esc(new Date(x.t * 1000).toLocaleString())}</span><button class="btn btn-ghost btn-sm btn-icon note-x" data-dismiss="${esc(x.id)}" aria-label="Dismiss" title="Dismiss">${icon("x")}</button></div>
    <div class="note-text">${esc(x.text)}</div>
    ${x.undone ? `<div class="faint small">Undone</div>` : x.undo ? `<button class="btn btn-sm" data-undo="${esc(x.id)}">Undo</button>` : ""}</li>`).join("")}</ol>`;
}
async function openNotes() {
  await pollNotes();
  const m = openModal({ title: "Notifications", wide: true, body: notesHtml(),
    foot: `<button class="btn" data-clear>Clear all</button><button class="btn btn-primary" data-close>Close</button>` });
  m.el.addEventListener("click", async (e) => {
    const x = e.target.closest("[data-dismiss]");
    const c = e.target.closest("[data-clear]");
    if (x || c) {
      try {
        notes = x ? await api.notificationDismiss(x.dataset.dismiss) : await api.notificationsClear();
        m.body.innerHTML = notesHtml();
        pollNotes();
      } catch (err) { toastError("Could not clear", err); }
      return;
    }
    const b = e.target.closest("[data-undo]");
    if (!b) return;
    b.disabled = true;
    try {
      await api.notificationUndo(b.dataset.undo);
      toast("Undone", "The channel's streams are back the way they were.", "ok");
      await pollNotes();
      m.body.innerHTML = notesHtml();
    } catch (err) { b.disabled = false; toastError("Could not undo", err); await pollNotes(); m.body.innerHTML = notesHtml(); }
  });
  if (notes.unread) {
    try { notes = await api.notificationsRead(); } catch {}
    pollNotes();
  }
}
$("#bell").addEventListener("click", openNotes);
pollNotes();
setInterval(pollNotes, 15000);

// ---------- Public IP pill (lets users confirm they are behind their VPN) ----------
const REGIONS = (() => { try { return new Intl.DisplayNames(["en"], { type: "region" }); } catch { return null; } })();
function countryName(cc) {
  try { return REGIONS?.of(cc.toUpperCase()) || cc; } catch { return cc; }
}
// "Amsterdam, North Holland": the VPN exit's city and its state or province (left out when it repeats the city).
export function placeName(n) {
  return [n?.city, n?.region && n.region !== n.city ? n.region : null].filter(Boolean).join(", ");
}

export async function refreshNetwork(fresh = false) {
  const pill = $("#netPill");
  pill.classList.add("checking");
  try {
    const n = await api.network(fresh);
    pill.classList.toggle("bad", !!n?.error || !n?.ip);
    pill.querySelector(".np-ip").textContent = n?.ip || "unknown";
    pill.querySelector(".np-loc").textContent = placeName(n);
    pill.querySelector(".np-cc").innerHTML = n?.country
      ? `${flag(n.country)} <span class="cc-code">${esc(n.country)}</span><span class="cc-name">${esc(countryName(n.country))}</span>` : "";
    pill.title = `Public IP of this container${n?.checked_at ? `, checked ${new Date(n.checked_at).toLocaleTimeString()}` : ""}${n?.error ? `. ${n.error}` : ""}. Click to check again.`;
    window.dispatchEvent(new CustomEvent("coax:network", { detail: n }));
    return n;
  } catch (e) {
    pill.classList.add("bad");
    pill.querySelector(".np-ip").textContent = "unavailable";
    pill.querySelector(".np-cc").textContent = "";
    pill.querySelector(".np-loc").textContent = "";
    throw e;
  } finally {
    pill.classList.remove("checking");
  }
}
$("#netPill").addEventListener("click", () => refreshNetwork(true).catch(() => {}));

// ---------- Version (stamped into each build by scripts/stamp-version.sh) ----------
api.version().then((v) => {
  const el = $("#buildVer");
  if (v?.version) { el.textContent = `v${v.version}`; el.title = `coax ${v.version}`; }
}).catch(() => {});

// Narrow screens: the sidebar shows icons only, so the IP pill and the version move to a bar above the page.
const narrow = matchMedia("(max-width: 900px)");
function placeMeta() {
  const pill = $("#netPill"), ver = $("#buildVer"), foot = $(".sidebar-foot");
  if (narrow.matches) $("#mobileBar").append(pill, ver);
  else { foot.before(pill); foot.after(ver); }
}
narrow.addEventListener("change", placeMeta);
placeMeta();

refreshNetwork().catch(() => {});
setInterval(() => refreshNetwork().catch(() => {}), 300000);

// Keyboard: "/" focuses the page search.
document.addEventListener("keydown", (e) => {
  if (e.key === "/" && !/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName) && !document.querySelector(".modal")) {
    const s = document.querySelector(".page [data-search]");
    if (s) { e.preventDefault(); s.focus(); }
  }
});

initPopovers();
window.addEventListener("hashchange", route);
route();

// First run: adopt the browser's time zone if coax has none of its own (user can change it in Settings).
(async () => {
  try {
    const cfg = await api.getConfig();
    const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
    if (!cfg.timezone && cfg.timezone_effective?.source === "default" && zone && zone !== "UTC") {
      await api.putConfig({ timezone: zone });
    }
  } catch {}
})();
