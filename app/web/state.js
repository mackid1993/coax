// Shared cached data with cheap invalidation. Pages call load*() and get fresh-enough copies.
import { api } from "./api.js";

const cache = new Map();

async function cached(key, fn, maxAgeMs) {
  const hit = cache.get(key);
  if (hit && Date.now() - hit.t < maxAgeMs) return hit.p;
  const p = fn();
  cache.set(key, { t: Date.now(), p });
  try {
    return await p;
  } catch (e) {
    cache.delete(key);
    throw e;
  }
}

export const invalidate = (...keys) => (keys.length ? keys.forEach((k) => cache.delete(k)) : cache.clear());

export const loadProviders = (fresh = false) => { if (fresh) invalidate("providers"); return cached("providers", api.listProviders, 15000); };
export const loadChannels = (fresh = false) => { if (fresh) invalidate("channels"); return cached("channels", api.listChannels, 15000); };
export const loadLineups = (fresh = false) => { if (fresh) invalidate("lineups"); return cached("lineups", api.listLineups, 15000); };
export const loadConfig = (fresh = false) => { if (fresh) invalidate("config"); return cached("config", api.getConfig, 15000); };

// Normalize a provider's group list: accepts ["A", ...] or [{name, count}] plus a selected list.
export function normalizeGroups(data, provider) {
  let groups = [];
  let selected = provider?.groups || [];
  const raw = Array.isArray(data) ? data : data?.groups || data?.items || [];
  if (data && !Array.isArray(data) && Array.isArray(data.selected)) selected = data.selected;
  for (const g of raw) {
    if (typeof g === "string") groups.push({ name: g, count: null });
    else if (g && typeof g === "object") {
      groups.push({ name: g.name ?? g.group ?? g.title ?? "", count: g.count ?? g.streams ?? null });
      if (g.selected === true && !selected.includes(g.name)) selected = [...selected, g.name];
    }
  }
  return { groups, selected: new Set(selected) };
}

export function providerName(providers, id) {
  return providers.find((p) => p.id === id)?.name || id || "Unknown";
}

// Catalog page result: {total, offset, limit, items}.
export function normalizePage(data) {
  const items = Array.isArray(data) ? data : data?.items || data?.streams || [];
  const total = Array.isArray(data) ? items.length : data?.total ?? items.length;
  return { items, total, offset: data?.offset ?? 0 };
}

// Scores arrive as 0..1 or 0..100; return 0..100.
export function pct(score) {
  let v = Number(score) || 0;
  if (v <= 1) v *= 100;
  return Math.round(v);
}
export const HIGH = 85;
export const LOW = 60;
