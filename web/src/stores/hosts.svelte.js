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
//
// The serving Host's own peers come first (feature peer_snapshot): it reads them server to
// server with the device token it keeps for each, and this app reads only GET /peers/snapshot
// from its own Host (full scope only), so no other Host's token is in the browser at all. A pasted Host is read
// directly only when the serving Host does not know it (no peer with its server_id).

import { createAgentSnapshotStore } from '@core/agent-snapshot.js';
import { normalizeMoved } from '@core/agent-moved.js';
import { originOf, readIds, resolveAllHosts, PREFERENCE_KEY } from '@core/all-hosts.js';
import { visibleSessions } from '@core/session-select.js';
import { clog } from '../lib/net.js';
import { fetchFailure, peerAttempt } from '@core/host-reach.js';
import { noteFail, noteOk, reachOf, trackHost, untrackHost, untrackOthers } from './reach.svelte.js';

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
  /** The serving Host's peers (GET /peers/snapshot): [{id: 'peer:<server_id>', url, name, serverId, via}]. */
  peers: [],
  /** id -> {state: 'connecting'|'live'|'offline'|'refused', name, serverId, rows, at} */
  live: {},
});

// The user's own choice of the option, and whether the first peers have switched it on once.
const CHOSEN_KEY = 'chats.allHosts.chosen.v1';
const PEERS_KEY = 'chats.allHosts.peers.v1';
let peersSupported = true;

function persist() {
  try { localStorage.setItem(KEY, JSON.stringify(hostsState.saved)); } catch (_) {}
}

export function setAllHosts(on) {
  hostsState.allHosts = !!on;
  // Hosts no longer read are no longer asked, nor shown as down.
  if (!on) untrackOthers();
  try {
    localStorage.setItem(PREFERENCE_KEY, on ? '1' : '0');
    localStorage.setItem(CHOSEN_KEY, '1');
  } catch (_) {}
  schedule(0);
}

/** Every other Host this app knows: the serving Host's peers, then pasted Hosts it does not know. */
export function knownHosts() {
  const viaHost = new Set(hostsState.peers.map(p => p.serverId).filter(Boolean));
  return [...hostsState.peers, ...hostsState.saved.filter(h => !(h.serverId && viaHost.has(h.serverId)))];
}

/** The other Hosts read now (empty while the option is off), in known order. */
export function readHosts() {
  const known = knownHosts();
  const ids = readIds(known.map(h => h.id), hostsState.allHosts).slice(1);
  return ids.map(id => known.find(h => h.id === id)).filter(Boolean);
}

/**
 * The serving Host's peers, read through it (GET /peers/snapshot). A Host without the feature
 * (404) or a limited device (403) is not asked again. The first time it names any peer, the option is switched
 * on once, unless the user has chosen it themselves (the shared one-time default ran before any
 * peer could be counted).
 */
let peersInFlight = null;
function readPeers() {
  if (!peersInFlight) peersInFlight = readPeersOnce().finally(() => { peersInFlight = null; });
  return peersInFlight;
}

async function readPeersOnce() {
  if (!peersSupported) return;
  let body;
  try {
    const r = await fetch('/peers/snapshot', { cache: 'no-store' });
    // 404: a Host without the feature; 403: a limited device (peers are full scope). Neither is
    // asked again, and neither is said: the list is this Host's and any pasted ones.
    if (r.status === 404 || r.status === 403) {
      peersSupported = false;
      untrackOthers(id => !id.startsWith('peer:'));
      return;
    }
    if (!r.ok) return;
    body = await r.json();
  } catch (_) {
    return;
  }
  const entries = Object.entries((body && body.peers) || {}).filter(([, p]) => p && typeof p === 'object');
  const priorPeers = new Map(hostsState.peers.map(p => [p.id, p.name]));
  // A peer gone from the serving Host's registry is no longer asked about.
  const present = new Set(entries.map(([key]) => 'peer:' + key));
  for (const p of hostsState.peers) if (!present.has(p.id)) { untrackHost(p.id); delete hostsState.live[p.id]; }
  hostsState.peers = entries.map(([key, p]) => ({
    id: 'peer:' + key, url: String(p.url || ''),
    name: p.reachable ? String(p.name || key) : String(priorPeers.get('peer:' + key) || p.name || key),
    serverId: String(p.server_id || ''), via: 'host',
  }));
  for (const [key, p] of entries) {
    const id = 'peer:' + key;
    const prior = hostsState.live[id];
    // A peer that does not answer is named by its registry key; keep the name it last gave.
    const name = p.reachable ? String(p.name || key) : String(prior?.name || p.name || key);
    hostsState.live[id] = {
      state: p.reachable ? 'live' : p.error === 'refused' ? 'refused' : 'offline',
      name, serverId: String(p.server_id || ''),
      rows: p.reachable ? rowsFromSnapshot(p.snapshot) : (prior?.rows || []), at: Date.now(),
    };
    // Whether it answers, per Host (stores/reach.svelte.js). The serving Host read it, so a
    // failure here says nothing of this device's own network.
    // Only while its chats are listed: with the option off a peer is read for its name alone.
    if (!hostsState.allHosts) continue;
    trackHost(id, {
      label: name, url: String(p.url || ''), key: String(p.server_id || '') || id,
      via: 'host', probe: readPeers,
    });
    if (p.reachable) noteOk(id);
    // A peer already down counts a failure only when its own probe asked; the 15 s read would
    // otherwise run its backoff ahead.
    else if (!reachOf(id)?.down || reachOf(id)?.inFlight) noteFail(id, peerAttempt(p.error));
  }
  if (hostsState.peers.length) {
    try {
      if (localStorage.getItem(PEERS_KEY) !== '1') {
        localStorage.setItem(PEERS_KEY, '1');
        if (localStorage.getItem(CHOSEN_KEY) !== '1' && !hostsState.allHosts) {
          hostsState.allHosts = true;
          localStorage.setItem(PREFERENCE_KEY, '1');
        }
      }
    } catch (_) {}
  }
}

async function getJson(origin, path, token) {
  const ctrl = new AbortController();
  let timedOut = false;
  const t0 = Date.now();
  const timer = setTimeout(() => { timedOut = true; ctrl.abort(); }, TIMEOUT_MS);
  let r;
  try {
    r = await fetch(origin + path, {
      method: 'GET', mode: 'cors', credentials: 'omit', cache: 'no-store', referrerPolicy: 'no-referrer',
      headers: { Authorization: 'Bearer ' + token }, signal: ctrl.signal,
    });
  } catch (_) {
    // A network error, a timeout, or a Host that does not list this app's origin (CORS):
    // a page cannot tell the last ones apart, only whether our own timeout fired.
    clearTimeout(timer);
    return { offline: true, attempt: fetchFailure({ timedOut, sameOrigin: false, ms: Date.now() - t0 }) };
  }
  try {
    if (r.status === 401 || r.status === 403) return { refused: true, status: r.status, attempt: { kind: 'http', status: r.status } };
    if (r.status === 404) return { offline: true, status: r.status, attempt: { kind: 'not_this_host', status: 404 } };
    if (!r.ok) return { offline: true, status: r.status, attempt: { kind: 'http', status: r.status } };
    try {
      return { body: await r.json() };
    } catch (_) {
      // An answer that is not JSON is not a Clarp Host.
      return { offline: true, status: r.status, attempt: { kind: 'not_this_host', status: r.status } };
    }
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
  trackHost(host.id, {
    label: host.name || host.url, url: host.url, key: host.serverId || host.url, via: 'direct',
    probe: () => readSaved(host.id),
  });
  const info = await getJson(host.url, '/server-info', host.token);
  const infoId = info.body ? String(info.body.server_id || '') : '';
  if (info.body && host.serverId && infoId && infoId !== host.serverId
      && !(Array.isArray(info.body.previous_server_ids) && info.body.previous_server_ids.includes(host.serverId))) {
    // Another Clarp Host answers at this address now.
    hostsState.live[host.id] = { ...(hostsState.live[host.id] || {}), state: 'offline' };
    noteFail(host.id, { kind: 'not_this_host', status: 200 });
    return;
  }
  if (!info.body) {
    hostsState.live[host.id] = { ...(hostsState.live[host.id] || {}), state: info.refused ? 'refused' : 'offline' };
    noteFail(host.id, info.attempt || { kind: 'network' });
    return;
  }
  const snapshot = await getJson(host.url, '/agents/snapshot', host.token);
  if (!snapshot.body) {
    hostsState.live[host.id] = { ...(hostsState.live[host.id] || {}), state: snapshot.refused ? 'refused' : 'offline' };
    noteFail(host.id, snapshot.attempt || { kind: 'network' });
    return;
  }
  noteOk(host.id, {
    hostVersion: String(info.body.version || info.body.clarp_version || ''),
    contract: Number(info.body.contract && info.body.contract.host) || 0,
  });
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
    if (hostsState.allHosts) await readPeers();
    // A Host that is down is asked on its own backoff (stores/reach.svelte.js), not here.
    const pasted = readHosts().filter(h => h.via !== 'host' && !reachOf(h.id)?.down);
    await Promise.all(pasted.map(h => readOne(h).catch(() => {})));
  } finally {
    running = false;
    if (reading()) schedule();
  }
}

function reading() {
  return hostsState.allHosts && (peersSupported || readHosts().length > 0);
}

/** Starts reading: the serving Host's peers once (their names count for the badge even while
 *  the option is off), then every Host read; again whenever the page comes back. */
export function startHosts() {
  readPeers().finally(() => { if (reading()) schedule(0); });
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible' && reading()) schedule(0);
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
  if (serverId && (serverId === primaryServerId || knownHosts().some(h => h.serverId === serverId)
      || hostsState.saved.some(h => h.serverId === serverId))) {
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
  untrackHost(id);
  persist();
}

/** Reads one saved Host now, by id (its probe while it does not answer). */
function readSaved(id) {
  const host = hostsState.saved.find(h => h.id === id);
  return host ? readOne(host) : Promise.resolve();
}

/**
 * Changes a saved Host's address or token (the panel's Edit address and Pair again), then reads
 * it at once. Returns '' or why it was not changed.
 */
export async function updateHost(id, { url, token } = {}) {
  const at = hostsState.saved.findIndex(h => h.id === id);
  if (at < 0) return 'That Host is not saved on this device.';
  const next = { ...hostsState.saved[at] };
  if (url !== undefined) {
    const origin = originOf(url);
    if (!origin) return 'Give the Host\'s address, like https://studio.example.ts.net.';
    if (origin === location.origin) return 'That is the Host this app is served by.';
    next.url = origin;
  }
  if (token !== undefined) {
    const secret = String(token || '').trim();
    if (!secret) return 'Give a token paired with that Host.';
    next.token = secret;
  }
  hostsState.saved[at] = next;
  persist();
  trackHost(id, { url: next.url });
  await readOne(next).catch(() => {});
  return '';
}

/** Opens a chat of another Host in that Host's own web app (its origin, its own sign-in). */
export function openOnHost(host, sid) {
  const url = `${host.url}/?session=${encodeURIComponent(sid)}`;
  window.open(url, '_blank', 'noopener,noreferrer');
}
