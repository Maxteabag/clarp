// The user's other Hosts (issue 651): saved on this device, read cross-origin for the all-Hosts
// chat list. The rules are @core/all-hosts.js; this store saves, reads and keeps state.
//
// A saved Host is {id, url (its origin), token, name, serverId}. The token is a paired device's
// token for that Host, kept in this browser's localStorage the way the app keeps its own. It is
// sent only to that Host's origin, only in the Authorization header, with credentials 'omit'
// (no cookies), and only on GET /server-info and GET /agents/snapshot, the two routes a Host
// answers cross-origin for an origin on its [server] web_read_origins list.
//
// Other Hosts are polled (every 15 s, and when the page comes back), not streamed: an
// EventSource cannot carry an Authorization header, and the token never goes in a URL or a
// cookie.

import { createAgentSnapshotStore } from '@core/agent-snapshot.js';
import { normalizeMoved } from '@core/agent-moved.js';
import { originOf, readIds, resolveAllHosts, PREFERENCE_KEY } from '@core/all-hosts.js';
import { visibleSessions } from '@core/session-select.js';
import { clog } from '../lib/net.js';

const KEY = 'clarp.hosts.v1';
const POLL_MS = 15000;
const TIMEOUT_MS = 8000;

function loadSaved() {
  try {
    const list = JSON.parse(localStorage.getItem(KEY) || '[]');
    return Array.isArray(list)
      ? list.filter(h => h && typeof h.id === 'string' && originOf(h.url) && typeof h.token === 'string')
      : [];
  } catch (_) {
    return [];
  }
}

const saved = loadSaved();

export const hostsState = $state({
  /** Saved other Hosts, in saved order. */
  saved,
  /** 'Show chats from all Hosts' (resolved once, with its one-time default). */
  allHosts: resolveAllHosts(localStorage, 1 + saved.length),
  /** id -> {state: 'connecting'|'live'|'offline'|'refused', name, serverId, rows, at} */
  live: {},
});

function persist() {
  try { localStorage.setItem(KEY, JSON.stringify(hostsState.saved)); } catch (_) {}
}

export function setAllHosts(on) {
  hostsState.allHosts = !!on;
  try { localStorage.setItem(PREFERENCE_KEY, on ? '1' : '0'); } catch (_) {}
  schedule(0);
}

/** The other Hosts read now (empty while the option is off), in saved order. */
export function readHosts() {
  const ids = readIds(hostsState.saved.map(h => h.id), hostsState.allHosts).slice(1);
  return ids.map(id => hostsState.saved.find(h => h.id === id)).filter(Boolean);
}

async function getJson(origin, path, token) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), TIMEOUT_MS);
  try {
    const r = await fetch(origin + path, {
      method: 'GET', mode: 'cors', credentials: 'omit', cache: 'no-store', referrerPolicy: 'no-referrer',
      headers: { Authorization: 'Bearer ' + token }, signal: ctrl.signal,
    });
    if (r.status === 401 || r.status === 403) return { refused: true, status: r.status };
    if (!r.ok) return { offline: true, status: r.status };
    return { body: await r.json() };
  } catch (_) {
    // A network error, a timeout, or a Host that does not list this app's origin (CORS):
    // a page cannot tell these apart.
    return { offline: true };
  } finally {
    clearTimeout(timer);
  }
}

/** A Host's roster as list rows: what the chat list draws, from GET /agents/snapshot. */
export function rowsFromSnapshot(snapshot) {
  const store = createAgentSnapshotStore();
  const status = store.replaceFromSnapshot(snapshot || {});
  const sessions = (snapshot && Array.isArray(snapshot.agents) ? snapshot.agents : [])
    .map(a => a && a.session).filter(Boolean);
  return visibleSessions(sessions, status).map(sid => {
    const s = status[sid] || {};
    return {
      sid, name: s.name || sid, lastActivity: Number(s.last_activity) || 0,
      moved: normalizeMoved(s.moved), status: s,
    };
  }).filter(row => row.name);
}

async function readOne(host) {
  const prior = hostsState.live[host.id];
  if (!prior) hostsState.live[host.id] = { state: 'connecting', name: host.name, serverId: host.serverId, rows: [] };
  const info = await getJson(host.url, '/server-info', host.token);
  if (!info.body) {
    hostsState.live[host.id] = { ...(hostsState.live[host.id] || {}), state: info.refused ? 'refused' : 'offline' };
    return;
  }
  const snapshot = await getJson(host.url, '/agents/snapshot', host.token);
  if (!snapshot.body) {
    hostsState.live[host.id] = { ...(hostsState.live[host.id] || {}), state: snapshot.refused ? 'refused' : 'offline' };
    return;
  }
  const name = String(info.body.name || host.name || host.url);
  const serverId = String(info.body.server_id || host.serverId || '');
  if (name !== host.name || serverId !== host.serverId) {
    const at = hostsState.saved.findIndex(h => h.id === host.id);
    if (at >= 0) { hostsState.saved[at] = { ...hostsState.saved[at], name, serverId }; persist(); }
  }
  hostsState.live[host.id] = { state: 'live', name, serverId, rows: rowsFromSnapshot(snapshot.body), at: Date.now() };
}

let timer = 0;
let running = false;
function schedule(ms = POLL_MS) {
  clearTimeout(timer);
  timer = setTimeout(pollNow, ms);
}

export async function pollNow() {
  clearTimeout(timer);
  if (running) { schedule(1000); return; }
  running = true;
  try {
    const hosts = readHosts();
    await Promise.all(hosts.map(h => readOne(h).catch(() => {})));
  } finally {
    running = false;
    if (readHosts().length) schedule();
  }
}

/** Starts reading the saved Hosts; again whenever the page comes back. */
export function startHosts() {
  if (readHosts().length) schedule(0);
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible' && readHosts().length) schedule(0);
  });
}

/**
 * Saves another Host after reading it once with the token. `primaryServerId` is the serving
 * Host's own server id: the same Host cannot be added again. Adding a Host is the user's own
 * act in the place the option lives, so it switches 'Show chats from all Hosts' on.
 * Returns '' or the reason it was not added.
 */
export async function addHost(url, token, primaryServerId = '') {
  const origin = originOf(url);
  const secret = String(token || '').trim();
  if (!origin) return 'Give the Host\'s address, like https://studio.example.ts.net.';
  if (origin === location.origin) return 'That is the Host this app is served by.';
  if (!secret) return 'Give a token paired with that Host.';
  if (hostsState.saved.some(h => h.url === origin)) return 'That Host is already added.';
  const info = await getJson(origin, '/server-info', secret);
  if (info.refused) return 'That Host refused the token.';
  if (!info.body) {
    return `Cannot read ${origin} from here. On that Host, add ${location.origin} to `
      + '[server] web_read_origins, and check the address.';
  }
  const serverId = String(info.body.server_id || '');
  if (serverId && (serverId === primaryServerId || hostsState.saved.some(h => h.serverId === serverId))) {
    return 'That Host is already in the list.';
  }
  const id = 'h' + Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
  hostsState.saved = [...hostsState.saved, { id, url: origin, token: secret, name: String(info.body.name || origin), serverId }];
  persist();
  clog('hostsAdd', `count=${hostsState.saved.length}`);
  setAllHosts(true);
  return '';
}

export function removeHost(id) {
  hostsState.saved = hostsState.saved.filter(h => h.id !== id);
  delete hostsState.live[id];
  persist();
}

/** Opens a chat of another Host in that Host's own web app (its origin, its own sign-in). */
export function openOnHost(host, sid) {
  const url = `${host.url}/?session=${encodeURIComponent(sid)}`;
  window.open(url, '_blank', 'noopener,noreferrer');
}
