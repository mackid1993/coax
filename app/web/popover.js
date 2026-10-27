// Hover popovers: elements with data-pop="Title" (and optional data-pop-sub="Second line")
// show a styled card near the pointer. One shared element, event delegation, no per-row cost.
let pop, timer, current;

function ensure() {
  if (pop) return pop;
  pop = document.createElement("div");
  pop.className = "popover";
  pop.setAttribute("role", "tooltip");
  pop.innerHTML = '<div class="pop-title"></div><div class="pop-sub"></div>';
  document.body.appendChild(pop);
  return pop;
}

function place(target) {
  const r = target.getBoundingClientRect();
  const p = pop.getBoundingClientRect();
  const gap = 10;
  let left = r.left + Math.min(r.width / 2, 120) - p.width / 2;
  left = Math.max(8, Math.min(left, window.innerWidth - p.width - 8));
  let top = r.top - p.height - gap;
  let below = false;
  if (top < 8) { top = r.bottom + gap; below = true; }
  pop.style.left = `${Math.round(left)}px`;
  pop.style.top = `${Math.round(top)}px`;
  pop.classList.toggle("below", below);
  const arrow = Math.max(14, Math.min(r.left + Math.min(r.width / 2, 120) - left, p.width - 14));
  pop.style.setProperty("--arrow-x", `${Math.round(arrow)}px`);
}

function show(target) {
  const title = target.dataset.pop;
  if (!title) return;
  const sub = target.dataset.popSub || "";
  // Only pop when the text is actually cut off, unless data-pop-always is set.
  const clipped = [...target.querySelectorAll(".ellip, .cat-name, .r-title"), target]
    .some((el) => el.scrollWidth > el.clientWidth + 1);
  if (!clipped && !("popAlways" in target.dataset)) return;
  ensure();
  pop.querySelector(".pop-title").textContent = title;
  const s = pop.querySelector(".pop-sub");
  s.textContent = sub;
  s.hidden = !sub;
  pop.classList.add("measuring");
  place(target);
  pop.classList.remove("measuring");
  current = target;
  requestAnimationFrame(() => pop.classList.add("open"));
}

function hide() {
  clearTimeout(timer);
  current = null;
  if (pop) pop.classList.remove("open");
}

function onOver(e) {
  const t = e.target.closest("[data-pop]");
  if (t === current) return;
  clearTimeout(timer);
  if (!t) { hide(); return; }
  timer = setTimeout(() => show(t), 280);
}

export function initPopovers() {
  document.addEventListener("mouseover", onOver);
  document.addEventListener("focusin", (e) => { const t = e.target.closest("[data-pop]"); if (t) show(t); });
  document.addEventListener("focusout", hide);
  document.addEventListener("scroll", hide, true);
  document.addEventListener("mousedown", hide);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") hide(); });
}
