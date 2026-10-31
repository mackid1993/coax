// Every server endpoint the UI uses lives here, so a route change is a one-line fix.
// Responses are normalized loosely (the UI accepts a bare array or an object wrapper).

const enc = encodeURIComponent;

function qs(params) {
  const parts = [];
  for (const [k, v] of Object.entries(params || {})) {
    if (v === undefined || v === null || v === "") continue;
    if (Array.isArray(v)) v.forEach((x) => parts.push(`${enc(k)}=${enc(x)}`));
    else parts.push(`${enc(k)}=${enc(v)}`);
  }
  return parts.length ? `?${parts.join("&")}` : "";
}

export const routes = {
  config: () => "/api/config",
  providers: () => "/api/providers",
  provider: (id, force) => `/api/providers/${enc(id)}${qs({ force: force ? 1 : "" })}`,
  providerTestDraft: () => "/api/providers/test",
  providerTest: (id) => `/api/providers/${enc(id)}/test`,
  providerRefresh: (id) => `/api/providers/${enc(id)}/refresh?wait=1`,
  providerGroups: (id) => `/api/providers/${enc(id)}/groups`,
  providerVod: (id, refresh) => `/api/providers/${enc(id)}/vod${refresh ? "?refresh=1" : ""}`,
  providerVodSync: (id) => `/api/providers/${enc(id)}/vod/sync`,
  // {provider, group, q, offset, limit, all_groups}
  streams: (p) => `/api/catalog/streams${qs(p)}`,
  channels: () => "/api/channels",
  channel: (id) => `/api/channels/${enc(id)}`,
  channelStreams: (id) => `/api/channels/${enc(id)}/streams`,
  channelsBulkAdd: () => "/api/channels/bulk-add",
  channelsBulkUpdate: () => "/api/channels/bulk-update",
  channelsBulkDelete: () => "/api/channels/bulk-delete",
  channelsReorder: () => "/api/channels/reorder",
  channelsAutonumber: () => "/api/channels/auto-number",
  channelsApply: () => "/api/channels/apply",
  automatch: () => "/api/automatch",
  automatchStart: () => "/api/automatch/start",
  automatchJob: (result) => `/api/automatch/job${result ? "?result=1" : ""}`,
  suggestions: () => "/api/suggestions",
  ondemand: () => "/api/ondemand",
  ondemandTitles: (p) => `/api/ondemand/titles${qs(p)}`,
  ondemandDraft: () => "/api/ondemand/draft",
  ondemandSave: () => "/api/ondemand/save",
  ondemandAddAll: () => "/api/ondemand/add-all",
  gracenoteSearch: (q, local, deep) => `/api/gracenote/search${qs(local ? { q, local: 1 } : deep ? { q, deep: 1 } : { q })}`,
  gracenoteSuggest: (p) => `/api/gracenote/suggest${qs(p)}`,
  gracenoteBulkSuggest: () => "/api/gracenote/bulk-suggest",
  xmltvIds: (provider, q, limit = 200, context = "") => `/api/xmltv/ids${qs({ provider, q, limit, context })}`,
  lineups: () => "/api/lineups",
  lineup: (id) => `/api/lineups/${enc(id)}`,
  lineupDvr: (id) => `/api/lineups/${enc(id)}/dvr`,
  lineupDvrSync: (id) => `/api/lineups/${enc(id)}/dvr/sync`,
  status: () => "/api/status",
  network: () => "/api/network",
  backups: () => "/api/backups",
  backup: (name) => `/api/backups/${encodeURIComponent(name)}`,
  log: (since, limit = 500) => `/api/log${qs({ since, limit })}`,
  // Output URLs (not JSON) shown on the Lineups page.
  outputM3u: (lineup) => `/output/m3u/${enc(lineup)}.m3u`,
  outputXmltv: (lineup) => `/output/xmltv/${enc(lineup)}.xml`,
};

export class ApiError extends Error {
  constructor(message, status, body) {
    super(message);
    this.status = status;
    this.body = body;
  }
}

async function request(method, url, body, { timeout = 30000 } = {}) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeout);
  let res;
  try {
    res = await fetch(url, {
      method,
      headers: body !== undefined ? { "Content-Type": "application/json" } : {},
      body: body !== undefined ? JSON.stringify(body) : undefined,
      signal: ctrl.signal,
      cache: "no-store",
    });
  } catch (e) {
    clearTimeout(timer);
    const msg = e.name === "AbortError" ? "The server took too long to answer." : "Could not reach the coax server.";
    throw new ApiError(msg, 0, null);
  }
  clearTimeout(timer);
  const text = await res.text();
  let data = null;
  if (text) {
    try { data = JSON.parse(text); } catch { data = text; }
  }
  if (!res.ok) {
    const msg = (data && typeof data === "object" && (data.error || data.message)) || (typeof data === "string" && data.slice(0, 200)) || `${res.status} ${res.statusText}`;
    throw new ApiError(msg, res.status, data);
  }
  return data;
}

const get = (u, o) => request("GET", u, undefined, o);
const post = (u, b, o) => request("POST", u, b === undefined ? {} : b, o);
const put = (u, b, o) => request("PUT", u, b, o);
const del = (u, o) => request("DELETE", u, undefined, o);

// Accept [..] or {key: [..]} or {items: [..]}.
export function list(data, key) {
  if (Array.isArray(data)) return data;
  if (data && typeof data === "object") {
    if (key && Array.isArray(data[key])) return data[key];
    for (const k of ["items", "results", "data"]) if (Array.isArray(data[k])) return data[k];
  }
  return [];
}

export const api = {
  getConfig: () => get(routes.config()),
  putConfig: (cfg) => put(routes.config(), cfg),

  listProviders: async () => list(await get(routes.providers()), "providers"),
  createProvider: (p) => post(routes.providers(), p),
  updateProvider: (id, p) => put(routes.provider(id), p),
  deleteProvider: (id, force = false) => del(routes.provider(id, force)),
  testDraft: (p) => post(routes.providerTestDraft(), p, { timeout: 60000 }),
  testProvider: (id) => post(routes.providerTest(id), {}, { timeout: 60000 }),
  refreshProvider: (id) => post(routes.providerRefresh(id), { wait: true }, { timeout: 300000 }),
  getGroups: (id) => get(routes.providerGroups(id), { timeout: 120000 }),
  setGroups: (id, groups) => put(routes.providerGroups(id), { groups }),
  getVod: (id, refresh = false) => get(routes.providerVod(id, refresh), { timeout: 600000 }),
  setVod: (id, picks) => put(routes.providerVod(id), picks, { timeout: 600000 }),
  syncVod: (id) => post(routes.providerVodSync(id), {}),

  streams: (params) => get(routes.streams(params)),

  listChannels: async () => list(await get(routes.channels()), "channels"),
  getChannel: (id) => get(routes.channel(id)),
  createChannel: (c) => post(routes.channels(), c),
  updateChannel: (id, c) => put(routes.channel(id), c),
  setFeedDisabled: (id, b) => post(`/api/channels/${id}/feed`, b),
  switchFeed: (id, b) => post(`/api/channels/${id}/switch`, b),
  unblockFeed: (id, b) => post(`/api/channels/${id}/unblock`, b),
  deleteChannel: (id) => del(routes.channel(id)),
  setChannelStreams: (id, streams, mode = "replace") => post(routes.channelStreams(id), { streams, mode }),
  bulkAdd: (body) => post(routes.channelsBulkAdd(), body),
  bulkUpdate: (ids, set) => post(routes.channelsBulkUpdate(), { ids, set }),
  bulkDelete: (ids) => post(routes.channelsBulkDelete(), { ids }),
  reorder: (ids) => post(routes.channelsReorder(), { ids }),
  autonumber: (body) => post(routes.channelsAutonumber(), body),
  // {channel_id: {guide: [candidates], backups: [{provider, stream_id, name, score, reason}]}}
  automatch: (ids) => post(routes.automatch(), { ids }, { timeout: 300000 }),
  automatchStart: (ids) => post(routes.automatchStart(), { ids }),
  automatchJob: (result = false) => get(routes.automatchJob(result), { timeout: 10000 }),
  suggestions: () => get(routes.suggestions()),
  saveSuggestions: (draft) => put(routes.suggestions(), draft),

  ondemand: () => get(routes.ondemand(), { timeout: 60000 }),
  ondemandTitles: (p) => get(routes.ondemandTitles(p), { timeout: 60000 }),
  ondemandDraft: () => get(routes.ondemandDraft()),
  saveOndemandDraft: (d) => put(routes.ondemandDraft(), d),
  ondemandSave: (b) => post(routes.ondemandSave(), b, { timeout: 60000 }),
  ondemandAddAll: (b) => post(routes.ondemandAddAll(), b, { timeout: 60000 }),
  // {channels: [{id, guide: {...}, streams: [...]}]}
  applyChannels: (channels) => post(routes.channelsApply(), { channels }, { timeout: 60000 }),

  gracenoteSearch: async (q, local = false, deep = false) => list(await get(routes.gracenoteSearch(q, local, deep), { timeout: local ? 15000 : deep ? 90000 : 30000 }), "results"),
  gracenoteSuggest: (p) => get(routes.gracenoteSuggest(p), { timeout: 60000 }),
  xmltvIds: async (provider, q, context = "") => list(await get(routes.xmltvIds(provider, q, 200, context)), "items"),

  listLineups: async () => list(await get(routes.lineups()), "lineups"),
  createLineup: (l) => post(routes.lineups(), l),
  updateLineup: (id, l) => put(routes.lineup(id), l),
  deleteLineup: (id) => del(routes.lineup(id)),
  // {key, dvr, last_sync, defaults, dvr_url_set, base_url_set, in_dvr, source, error}
  lineupDvr: (id) => get(routes.lineupDvr(id), { timeout: 15000 }),
  // body {guide, limit, numbering, start_number, logos, name}
  addLineupToDvr: (id, opts) => post(routes.lineupDvr(id), opts, { timeout: 90000 }),
  syncLineupDvr: (id) => post(routes.lineupDvrSync(id), {}, { timeout: 90000 }),

  status: () => get(routes.status(), { timeout: 8000 }),
  // {ip, country, region, city, checked_at, error}
  backups: () => get(routes.backups()),
  backupNow: () => post(routes.backups(), {}),
  backupRestore: (name) => post(`${routes.backup(name)}/restore`, {}),
  backupDelete: (name) => del(routes.backup(name)),
  notifications: () => get("/api/notifications"),
  notificationsRead: () => post("/api/notifications/read", {}),
  notificationUndo: (id) => post(`/api/notifications/${encodeURIComponent(id)}/undo`, {}),
  notificationDismiss: (id) => post(`/api/notifications/${encodeURIComponent(id)}/dismiss`, {}),
  notificationsClear: () => post("/api/notifications/clear", {}),
  network: (fresh = false) => get(routes.network() + (fresh ? "?refresh=1" : ""), { timeout: 15000 }),
  version: () => get("/api/version"),
  languages: () => get("/api/languages"),
  log: (since, limit) => get(routes.log(since, limit), { timeout: 8000 }),
};
