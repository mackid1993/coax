// Small UI toolkit: escaping, icons, toasts, modals, logos, confirm dialogs. No framework.

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
export const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ESC[c]);

export function debounce(fn, ms = 200) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

// Stroke icons, 24px grid, drawn for this app.
const P = {
  providers: '<path d="M4 7.5h16M4 12h16M4 16.5h10"/><circle cx="18" cy="16.5" r="2"/>',
  channels: '<rect x="3" y="5" width="18" height="12" rx="2.5"/><path d="M8 21h8M12 17v4M9 2.5l3 2.5 3-2.5"/>',
  guide: '<rect x="3" y="4" width="18" height="16" rx="2.5"/><path d="M3 9h18M9 9v11M14 13h4M14 16.5h2.5"/>',
  lineups: '<path d="M4 6h10M4 12h10M4 18h10"/><path d="M18 5v14M15.5 16.5 18 19l2.5-2.5"/>',
  settings: '<path d="M4 7h9M17 7h3M4 17h3M11 17h9"/><circle cx="15" cy="7" r="2.2"/><circle cx="9" cy="17" r="2.2"/>',
  status: '<path d="M3 12h3.5l2.5-6 4 12 2.5-6H21"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  search: '<circle cx="11" cy="11" r="6.5"/><path d="m20 20-4.2-4.2"/>',
  refresh: '<path d="M20 11a8 8 0 0 0-14.3-4.9L4 8M4 4v4h4M4 13a8 8 0 0 0 14.3 4.9L20 16M20 20v-4h-4"/>',
  plug: '<path d="M9 3v5M15 3v5M6 8h12v3a6 6 0 0 1-12 0zM12 17v4"/>',
  trash: '<path d="M4 7h16M10 11v6M14 11v6M6 7l1 12a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-12M9 7V4h6v3"/>',
  edit: '<path d="M4 20h4L19 9l-4-4L4 16zM13.5 6.5l4 4"/>',
  x: '<path d="M6 6l12 12M18 6 6 18"/>',
  check: '<path d="m5 12.5 4.5 4.5L19 7.5"/>',
  copy: '<rect x="8" y="8" width="12" height="12" rx="2.5"/><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2"/>',
  grip: '<circle cx="9" cy="6" r="1.3"/><circle cx="15" cy="6" r="1.3"/><circle cx="9" cy="12" r="1.3"/><circle cx="15" cy="12" r="1.3"/><circle cx="9" cy="18" r="1.3"/><circle cx="15" cy="18" r="1.3"/>',
  chevron: '<path d="m6 9 6 6 6-6"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2.5v2M12 19.5v2M2.5 12h2M19.5 12h2M5.3 5.3l1.4 1.4M17.3 17.3l1.4 1.4M5.3 18.7l1.4-1.4M17.3 6.7l1.4-1.4"/>',
  moon: '<path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 7.5v.5"/>',
  alert: '<path d="M12 3.5 2.5 20h19z"/><path d="M12 10v4.5M12 17v.5"/>',
  ok: '<circle cx="12" cy="12" r="9"/><path d="m8 12.5 3 3 5-6"/>',
  users: '<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20a6.5 6.5 0 0 1 13 0M16 4.5a3.5 3.5 0 0 1 0 7M18 14a5.5 5.5 0 0 1 3.5 6"/>',
  layers: '<path d="m12 3 9 5-9 5-9-5z"/><path d="m3 13 9 5 9-5"/>',
  wand: '<path d="M4 20 15 9M14 4v3M19 9h3M17.5 5.5l2-2M12 2v1M20 12h1"/>',
  link: '<path d="M10 14a4.5 4.5 0 0 0 6.4 0l3-3a4.5 4.5 0 0 0-6.4-6.4l-1 1M14 10a4.5 4.5 0 0 0-6.4 0l-3 3a4.5 4.5 0 0 0 6.4 6.4l1-1"/>',
  hash: '<path d="M5 9h15M4 15h15M10 3 8 21M16 3l-2 18"/>',
  signal: '<path d="M5 18.5a10 10 0 0 1 0-13M19 5.5a10 10 0 0 1 0 13M8.5 15a5 5 0 0 1 0-6M15.5 9a5 5 0 0 1 0 6"/><circle cx="12" cy="12" r="1.5"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  gauge: '<path d="M4.5 18a9 9 0 1 1 15 0"/><path d="m12 13 4-5"/>',
  arrowUp: '<path d="M12 19V5M6 11l6-6 6 6"/>',
  arrowDown: '<path d="M12 5v14M6 13l6 6 6-6"/>',
  download: '<path d="M12 4v11M7 10l5 5 5-5M5 20h14"/>',
  filter: '<path d="M4 5h16l-6 8v6l-4-2v-4z"/>',
};
export function icon(name, extra = "") {
  return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" ${extra}>${P[name] || ""}</svg>`;
}

// ---------- Toasts ----------
export function toast(title, msg = "", kind = "info", ms = 4500) {
  const root = $("#toasts");
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.setAttribute("role", kind === "bad" ? "alert" : "status");
  const ic = { ok: "ok", bad: "alert", warn: "alert", info: "info" }[kind] || "info";
  el.innerHTML = `${icon(ic)}<div><div class="t-title">${esc(title)}</div>${msg ? `<div class="t-msg">${esc(msg)}</div>` : ""}</div><button class="t-close" aria-label="Dismiss">&times;</button>`;
  const close = () => { el.classList.add("leaving"); setTimeout(() => el.remove(), 220); };
  el.querySelector(".t-close").onclick = close;
  root.appendChild(el);
  while (root.children.length > 4) root.firstElementChild.remove();
  if (ms) setTimeout(close, kind === "bad" ? ms + 2500 : ms);
}
export const toastError = (what, err) => toast(what, err?.message || String(err), "bad");

// ---------- Modals ----------
let modalStack = [];
export function openModal({ title, body = "", foot = "", wide = false, onClose } = {}) {
  const prevFocus = document.activeElement;
  const back = document.createElement("div");
  back.className = "modal-backdrop";
  back.innerHTML = `<div class="modal ${wide ? "wide" : ""}" role="dialog" aria-modal="true" aria-label="${esc(title)}">
    <div class="modal-head"><h2>${esc(title)}</h2><button class="btn btn-ghost btn-icon btn-sm" data-close aria-label="Close">${icon("x")}</button></div>
    <div class="modal-body">${body}</div>${foot ? `<div class="modal-foot">${foot}</div>` : ""}</div>`;
  const m = {
    el: back.querySelector(".modal"),
    body: back.querySelector(".modal-body"),
    close() {
      if (m.closed) return;
      m.closed = true;
      modalStack = modalStack.filter((x) => x !== m);
      back.classList.add("closing");
      setTimeout(() => back.remove(), 160);
      document.removeEventListener("keydown", onKey, true);
      onClose && onClose();
      prevFocus && prevFocus.focus && prevFocus.focus();
    },
  };
  const onKey = (e) => {
    if (modalStack[modalStack.length - 1] !== m) return;
    if (e.key === "Escape") { e.stopPropagation(); m.close(); }
    if (e.key === "Tab") {
      const f = $$('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])', m.el).filter((x) => !x.disabled && x.offsetParent);
      if (!f.length) return;
      const first = f[0], last = f[f.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    }
  };
  document.addEventListener("keydown", onKey, true);
  back.addEventListener("mousedown", (e) => { if (e.target === back) m.close(); });
  back.addEventListener("click", (e) => { if (e.target.closest("[data-close]")) m.close(); });
  $("#modalRoot").appendChild(back);
  modalStack.push(m);
  requestAnimationFrame(() => {
    const auto = m.el.querySelector("[autofocus]") || m.el.querySelector(".modal-body input, .modal-body select, .modal-body button");
    (auto || m.el.querySelector("[data-close]")).focus();
  });
  return m;
}

export function confirmDialog({ title, message, confirm = "Confirm", danger = false }) {
  return new Promise((resolve) => {
    let ok = false;
    const m = openModal({
      title,
      body: `<p class="muted">${esc(message)}</p>`,
      foot: `<button class="btn" data-close>Cancel</button><button class="btn ${danger ? "btn-danger" : "btn-primary"}" data-ok>${esc(confirm)}</button>`,
      onClose: () => resolve(ok),
    });
    const b = m.el.querySelector("[data-ok]");
    b.onclick = () => { ok = true; m.close(); };
    setTimeout(() => b.focus(), 30);
  });
}

// ---------- Logos with graceful fallback ----------
const HUES = [18, 32, 200, 160, 260, 340, 120, 48, 220, 290];
export function initials(name) {
  const clean = String(name || "?").replace(/^[A-Z]{2,3}\s*[|:]\s*/, "").replace(/[^\p{L}\p{N}\s]/gu, " ").trim();
  const w = clean.split(/\s+/).filter(Boolean);
  if (!w.length) return "?";
  if (w.length === 1) return w[0].slice(0, 3).toUpperCase();
  return (w[0][0] + w[1][0] + (w[2] ? w[2][0] : "")).toUpperCase();
}
function hue(name) {
  let h = 0;
  for (const c of String(name || "")) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  return HUES[h % HUES.length];
}
export function logo(src, name, size = "") {
  const h = hue(name);
  const style = `style="background: linear-gradient(135deg, hsl(${h} 45% 30% / .55), hsl(${h} 45% 20% / .35)); color: hsl(${h} 70% 82%)"`;
  const fb = esc(initials(name));
  if (src && /^(https?:|data:image)/.test(src)) {
    return `<span class="logo ${size}" ${style}><img src="${esc(src)}" alt="" loading="lazy" decoding="async" referrerpolicy="no-referrer" data-fb="${fb}"></span>`;
  }
  return `<span class="logo ${size}" ${style} aria-hidden="true">${fb}</span>`;
}
// One global listener swaps broken logo images for initials.
document.addEventListener("error", (e) => {
  const img = e.target;
  if (img.tagName === "IMG" && img.dataset.fb !== undefined) {
    const span = img.parentElement;
    img.remove();
    span.textContent = img.dataset.fb;
  }
}, true);

// ---------- Empty states and skeletons ----------
export function emptyState({ title, text, actions = "", art = "cable" }) {
  const arts = {
    cable: `<svg class="empty-art" viewBox="0 0 120 80" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true">
      <path d="M6 58c18 0 22-30 44-30s26 26 44 26" stroke-dasharray="3 4" opacity=".55"/>
      <circle cx="100" cy="54" r="13" fill="var(--surface-2)"/><circle cx="100" cy="54" r="8.5" stroke-dasharray="1.5 1.5"/><circle cx="100" cy="54" r="3.2" fill="var(--accent)" stroke="none"/>
      <rect x="12" y="20" width="26" height="18" rx="4" fill="var(--surface-2)"/><path d="M18 26h14M18 32h9"/></svg>`,
    tv: `<svg class="empty-art" viewBox="0 0 120 80" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true">
      <rect x="28" y="14" width="64" height="44" rx="6" fill="var(--surface-2)"/>
      <path d="M36 24h20M36 31h30M36 38h14" opacity=".6"/><circle cx="80" cy="44" r="5" fill="var(--accent)" stroke="none" opacity=".85"/>
      <path d="M50 66h20M60 58v8M50 6l10 8 10-8"/></svg>`,
    signal: `<svg class="empty-art" viewBox="0 0 120 80" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" aria-hidden="true">
      <path d="M10 44h22l8-18 12 36 10-26 8 8h40" opacity=".6"/><circle cx="60" cy="40" r="4" fill="var(--accent)" stroke="none"/></svg>`,
  };
  return `<div class="empty">${arts[art] || arts.cable}<h3>${esc(title)}</h3><p>${text}</p>${actions ? `<div class="row">${actions}</div>` : ""}</div>`;
}

export function skeletonRows(n = 8, cols = 4) {
  let r = "";
  for (let i = 0; i < n; i++) {
    r += "<tr>";
    for (let c = 0; c < cols; c++) r += `<td><span class="skel" style="width:${c === 1 ? 50 + ((i * 37) % 40) : 30 + ((i * 13 + c * 7) % 50)}%"></span></td>`;
    r += "</tr>";
  }
  return r;
}
export function skeletonCards(n = 3, h = 160) {
  return Array.from({ length: n }, () => `<div class="card card-pad"><span class="skel" style="width:40%;height:16px"></span><span class="skel" style="width:70%;margin-top:14px"></span><span class="skel" style="width:55%;margin-top:10px"></span><span class="skel" style="height:${h - 90}px;margin-top:18px;border-radius:10px"></span></div>`).join("");
}

// ---------- Formatting ----------
export function fmtBytes(n) {
  n = Number(n) || 0;
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1000 && i < u.length - 1) { n /= 1000; i++; }
  return `${n.toFixed(n < 10 && i ? 1 : 0)} ${u[i]}`;
}
export function fmtRate(bitsPerSec) {
  const b = Number(bitsPerSec) || 0;
  if (b >= 1e6) return `${(b / 1e6).toFixed(1)} Mbps`;
  if (b >= 1e3) return `${(b / 1e3).toFixed(0)} kbps`;
  return `${b.toFixed(0)} bps`;
}
export function fmtDuration(sec) {
  sec = Math.max(0, Math.floor(Number(sec) || 0));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  if (h) return `${h}h ${String(m).padStart(2, "0")}m`;
  if (m) return `${m}m ${String(s).padStart(2, "0")}s`;
  return `${s}s`;
}
export function fmtAgo(iso) {
  if (!iso) return "never";
  const t = typeof iso === "number" ? (iso > 1e12 ? iso : iso * 1000) : Date.parse(iso);
  if (!t) return String(iso);
  const d = (Date.now() - t) / 1000;
  if (d < 0) return "just now";
  if (d < 60) return "just now";
  if (d < 3600) return `${Math.floor(d / 60)} min ago`;
  if (d < 86400) return `${Math.floor(d / 3600)} h ago`;
  return `${Math.floor(d / 86400)} d ago`;
}
export const fmtInt = (n) => (Number(n) || 0).toLocaleString("en-US");

export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    // Clipboard API needs a secure context; LAN http falls back to a hidden textarea.
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    document.execCommand("copy");
    ta.remove();
  }
  toast("Copied", text, "ok", 2200);
}

// Button busy state helper: await busy(btn, promise).
export async function busy(btn, fn) {
  if (btn) { btn.classList.add("loading"); btn.disabled = true; }
  try { return await fn(); } finally { if (btn) { btn.classList.remove("loading"); btn.disabled = false; } }
}

export function switchHtml(name, checked, label = "", attrs = "") {
  return `<label class="switch"><input type="checkbox" ${name ? `name="${esc(name)}"` : ""} ${checked ? "checked" : ""} ${attrs}><span class="track"></span>${label ? `<span>${esc(label)}</span>` : ""}</label>`;
}
export function checkHtml(checked, attrs = "") {
  return `<span class="check"><input type="checkbox" ${checked ? "checked" : ""} ${attrs}></span>`;
}

export function confidenceHtml(c) {
  let v = Number(c) || 0;
  if (v <= 1) v *= 100;
  v = Math.round(v);
  const color = v >= 85 ? "var(--ok)" : v >= 60 ? "var(--warn)" : "var(--bad)";
  const cls = v >= 85 ? "badge-ok" : v >= 60 ? "badge-warn" : "badge-bad";
  return `<span class="badge ${cls} confidence" title="Match confidence"><span class="conf-bar"><span style="width:${v}%;background:${color}"></span></span>${v}%</span>`;
}
