import { initPopovers } from "./popover.js";
// coax web UI entry: router, theme, server health. Pages live in pages/*.js.
import { api } from "./api.js";
import { $, icon, esc, flag } from "./ui.js";

const PAGES = [
  { id: "channels", label: "Channels", icon: "channels", load: () => import("./pages/channels.js") },
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

// ---------- Public IP pill (lets users confirm they are behind their VPN) ----------
export async function refreshNetwork(fresh = false) {
  const pill = $("#netPill");
  pill.classList.add("checking");
  try {
    const n = await api.network(fresh);
    pill.classList.toggle("bad", !!n?.error || !n?.ip);
    pill.querySelector(".np-ip").textContent = n?.ip || "unknown";
    pill.querySelector(".np-cc").textContent = n?.country ? `${flag(n.country)} ${n.country}` : "";
    pill.title = `Public IP of this container${n?.checked_at ? `, checked ${new Date(n.checked_at).toLocaleTimeString()}` : ""}${n?.error ? `. ${n.error}` : ""}. Click to check again.`;
    window.dispatchEvent(new CustomEvent("coax:network", { detail: n }));
    return n;
  } catch (e) {
    pill.classList.add("bad");
    pill.querySelector(".np-ip").textContent = "unavailable";
    pill.querySelector(".np-cc").textContent = "";
    throw e;
  } finally {
    pill.classList.remove("checking");
  }
}
$("#netPill").addEventListener("click", () => refreshNetwork(true).catch(() => {}));
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
