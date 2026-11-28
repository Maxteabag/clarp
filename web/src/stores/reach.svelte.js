// Whether each Host answers (docs/design/host-not-answering.md). One record per Host, so a Host
// that does not answer shows only where its own content is and never stalls another.
//
// Hosts are tracked by id: PRIMARY for the Host serving this page, the all-Hosts ids
// ('peer:<server_id>' read through the serving Host, 'h…' read directly) for the others. Each
// has a probe that reports back through noteOk / noteFail; this store keeps the backoff, the
// one-second clock the status line needs while something is down, and the triggers that try
// again at once (window focus, page shown, network back).
//
// A leaf module: it imports no other store, so any store can feed it.

import {
  PROBE_TIMEOUT_MS, addressKind, answerFailure, blankReach, classify, fetchFailure,
  reachFail, reachOk,
} from '@core/host-reach.js';
import { clog } from '../lib/net.js';

export const PRIMARY = 'primary';
const KEY = 'clarp.reach.v1';

function loadKept() {
  try {
    const v = JSON.parse(localStorage.getItem(KEY) || '{}');
    return v && typeof v === 'object' ? v : {};
  } catch (_) { return {}; }
}
const kept = loadKept();

export const reach = $state({
  /** id -> record (static/lib/host-reach.js blankReach) plus {label, url, key, via}. */
  hosts: {},
  /** What the OS says (navigator.onLine): false shows one app-wide banner instead of panels. */
  deviceOnline: typeof navigator === 'undefined' || navigator.onLine !== false,
  /** Ticks once a second while a status line counts down. */
  now: Date.now(),
  /** The Host whose full panel is open over the app (from a list row), or ''. */
  panel: '',
});

/** Probes and recovery hooks, outside $state: functions are not view state. */
const probes = new Map();
const recoverHooks = new Map();
const timers = new Map();

function persist() {
  const out = {};
  for (const r of Object.values(reach.hosts)) {
    if (r.key && (r.lastOk || r.downSince)) out[r.key] = { lastOk: r.lastOk, downSince: r.downSince };
  }
  try { localStorage.setItem(KEY, JSON.stringify({ ...kept, ...out })); } catch (_) {}
}

function record(id) {
  if (!reach.hosts[id]) reach.hosts[id] = { ...blankReach(), label: '', url: '', key: '', via: '' };
  return reach.hosts[id];
}

/**
 * Says who a Host is: its label, address, the key its saved "since" is kept under (server id,
 * else origin) and how this device reads it ('serving', 'host' for a peer, 'direct').
 * `probe` is an async function that reports through noteOk / noteFail.
 */
export function trackHost(id, { label = '', url = '', key = '', via = 'direct', probe = null } = {}) {
  const r = record(id);
  const nextKey = key || url;
  if (nextKey && nextKey !== r.key) {
    // What an earlier visit saw: the last answer, and a "since" that began before this page did.
    const k = kept[nextKey] || {};
    const base = blankReach({ lastOk: Number(k.lastOk) || 0, downSince: Number(k.downSince) || 0 });
    if (base.lastOk > r.lastOk) r.lastOk = base.lastOk;
    if (base.downSince && !r.down) r.keptDownSince = base.downSince;
    r.key = nextKey;
  }
  if (label) r.label = label;
  if (url) r.url = url;
  r.via = via;
  if (probe) probes.set(id, probe);
}

export function untrackHost(id) {
  clearTimeout(timers.get(id));
  timers.delete(id);
  probes.delete(id);
  delete reach.hosts[id];
  if (reach.panel === id) reach.panel = '';
}

/** Runs `fn` when the Host answers again after not answering. */
export function onRecover(id, fn) {
  if (!recoverHooks.has(id)) recoverHooks.set(id, new Set());
  recoverHooks.get(id).add(fn);
}

/** The Host's record, or null. */
export function reachOf(id) {
  return reach.hosts[id] || null;
}

/** Shown as not answering: down, and this device is online (offline is the banner's). */
export function isDown(id) {
  const r = reach.hosts[id];
  return !!(r && r.down && reach.deviceOnline);
}

/** The panel is shown: down, or "Back online" for its two seconds. Reads reach.now. */
export function panelShown(id) {
  const r = reach.hosts[id];
  if (!r || !reach.deviceOnline) return false;
  return r.down || r.backUntil > reach.now;
}

/** The combined list's line for each of `ids` that does not answer: [{id, label, since}]. */
export function downHosts(ids) {
  return ids.filter(isDown).map(id => {
    const r = reach.hosts[id];
    return { id, label: r.label || 'This Host', since: r.downSince || reach.now };
  });
}

export function openPanel(id) { reach.panel = id; }
export function closePanel() { reach.panel = ''; }

/** The other Hosts this device reads itself, for the classifier and the diagnostics. */
function othersOf(id) {
  return Object.entries(reach.hosts)
    .filter(([other, r]) => other !== id && r.via !== 'host' && (r.down || r.lastOk))
    .map(([, r]) => ({ address: addressKind(r.url), ok: !r.down }));
}

export function othersAnswering(id) {
  const known = Object.entries(reach.hosts).filter(([other, r]) => other !== id && (r.down || r.lastOk));
  return { ok: known.filter(([, r]) => !r.down).length, total: known.length };
}

function reasonFor(id, attempt) {
  const r = reach.hosts[id];
  return classify(attempt, {
    deviceOnline: reach.deviceOnline,
    address: addressKind(r?.url),
    viaHost: r?.via === 'host',
    // A page cannot see this device's network interfaces.
    deviceTunnel: null,
    others: othersOf(id),
  });
}

/** A failure elsewhere can change what a down Host's silence most likely means. */
function reclassify() {
  for (const [id, r] of Object.entries(reach.hosts)) {
    if (r.down && r.attempt) {
      const next = reasonFor(id, r.attempt);
      if (next !== r.reason) r.reason = next;
    }
  }
}

function schedule(id) {
  clearTimeout(timers.get(id));
  const r = reach.hosts[id];
  if (!r || !r.down || !probes.has(id)) return;
  const wait = Math.max(0, r.nextAt - Date.now());
  timers.set(id, setTimeout(() => probeNow(id), wait));
}

/** The Host answered. `info`: {hostVersion, contract} when known. */
export function noteOk(id, info = {}) {
  const r = record(id);
  const wasDown = r.down;
  Object.assign(r, reachOk(r, Date.now(), info));
  delete r.keptDownSince;
  clearTimeout(timers.get(id));
  persist();
  if (wasDown) {
    clog('hostReach', `id=${id.startsWith('peer:') ? 'peer' : id === PRIMARY ? 'serving' : 'saved'} state=back`);
    for (const fn of recoverHooks.get(id) || []) { try { fn(); } catch (_) {} }
    if (reach.panel === id) setTimeout(() => { if (reach.panel === id && !reach.hosts[id]?.down) reach.panel = ''; }, 2000);
  }
  reclassify();
  tickWhileNeeded();
}

/** An attempt failed. `attempt`: static/lib/host-reach.js's {kind, status?, ms?}. */
export function noteFail(id, attempt) {
  const r = record(id);
  const wasDown = r.down;
  if (!wasDown && r.keptDownSince) r.downSince = r.keptDownSince;
  delete r.keptDownSince;
  Object.assign(r, reachFail(r, attempt, reasonFor(id, attempt), Date.now()));
  persist();
  if (!wasDown) clog('hostReach', `id=${id.startsWith('peer:') ? 'peer' : id === PRIMARY ? 'serving' : 'saved'} state=down reason=${r.reason}`);
  reclassify();
  schedule(id);
  tickWhileNeeded();
}

/** Tries the Host now; a probe already on its way is not doubled. */
export async function probeNow(id) {
  const probe = probes.get(id);
  const r = reach.hosts[id];
  if (!probe || !r || r.inFlight) return;
  // No network at all: nothing to learn. The 'online' event tries again.
  if (!reach.deviceOnline) return;
  clearTimeout(timers.get(id));
  r.inFlight = true;
  try { await probe(); } catch (_) {}
  const after = reach.hosts[id];
  // A probe that reported nothing leaves the Host as it was and keeps the schedule.
  if (after && after.inFlight) {
    after.inFlight = false;
    schedule(id);
  }
}

/** Something hinted the Host may be gone (the event stream dropped): ask it, unless already down. */
export function suspect(id) {
  const r = reach.hosts[id];
  if (!r || r.down || r.inFlight) return;
  probeNow(id);
}

/** The Try again button: the backoff starts over and the Host is asked now. */
export function retry(id) {
  const r = reach.hosts[id];
  if (!r) return;
  r.failures = 0;
  probeNow(id);
}

/** Focus, page shown, network back: every Host that is down is asked again at once. */
export function retryAll() {
  for (const [id, r] of Object.entries(reach.hosts)) {
    if (r.down) { r.failures = 0; probeNow(id); }
  }
}

// ---- the clock --------------------------------------------------------------------------------

let clock = 0;
function tickWhileNeeded() {
  const now = Date.now();
  reach.now = now;
  const needed = Object.values(reach.hosts).some(r => r.down || r.backUntil > now);
  if (needed && !clock) {
    clock = setInterval(() => {
      reach.now = Date.now();
      if (!Object.values(reach.hosts).some(r => r.down || r.backUntil > reach.now)) {
        clearInterval(clock);
        clock = 0;
      }
    }, 1000);
  }
}

// ---- the serving Host ------------------------------------------------------------------------

let servingIds = [];

/** The serving Host's server id (and previous ones), from /server-info: a different id is not it. */
export function setServingIdentity({ serverId = '', previous = [], label = '' } = {}) {
  servingIds = [serverId, ...previous].filter(Boolean);
  trackHost(PRIMARY, {
    label: label || reach.hosts[PRIMARY]?.label || location.host,
    url: location.origin, key: serverId || location.origin, via: 'serving',
  });
}

/** Asks the serving Host's /server-info, with a short timeout. */
async function probeServing() {
  const ctrl = new AbortController();
  let timedOut = false;
  const t0 = Date.now();
  const timer = setTimeout(() => { timedOut = true; ctrl.abort(); }, PROBE_TIMEOUT_MS);
  let res;
  try {
    res = await fetch('/server-info', { cache: 'no-store', signal: ctrl.signal });
  } catch (_) {
    clearTimeout(timer);
    noteFail(PRIMARY, fetchFailure({ timedOut, sameOrigin: true, ms: Date.now() - t0 }));
    return;
  }
  clearTimeout(timer);
  let body = null;
  try { body = await res.json(); } catch (_) {}
  const json = !!body && typeof body === 'object' && !Array.isArray(body);
  const id = json ? String(body.server_id || '') : '';
  const previous = json && Array.isArray(body.previous_server_ids) ? body.previous_server_ids.map(String) : [];
  const same = !servingIds.length || !id || servingIds.includes(id) || previous.some(p => servingIds.includes(p));
  const fail = answerFailure(res.status, { json: json && 'server_id' in body, serverId: same ? '' : id, expectedId: servingIds[0] || '' });
  if (fail) { noteFail(PRIMARY, fail); return; }
  noteOk(PRIMARY, {
    hostVersion: String(body.version || body.clarp_version || ''),
    contract: Number(body.contract && body.contract.host) || 0,
  });
}

trackHost(PRIMARY, {
  label: typeof location !== 'undefined' ? location.host : '',
  url: typeof location !== 'undefined' ? location.origin : '',
  key: '', via: 'serving', probe: probeServing,
});

// ---- device network and the triggers ---------------------------------------------------------

/** Wifi, cellular, ethernet when the browser says (Chromium's NetworkInformation), else ''. */
export function deviceNetworkKind() {
  const c = typeof navigator !== 'undefined' && navigator.connection;
  const t = c && typeof c.type === 'string' ? c.type : '';
  return ['wifi', 'cellular', 'ethernet'].includes(t) ? t : '';
}

let started = false;
export function startReach() {
  if (started || typeof window === 'undefined') return;
  started = true;
  window.addEventListener('online', () => {
    reach.deviceOnline = true;
    clog('hostReach', 'device=online');
    // Every Host is asked: the ones that were down, and the serving one in case it went down
    // while there was no network to notice.
    retryAll();
    suspect(PRIMARY);
  });
  window.addEventListener('offline', () => {
    reach.deviceOnline = false;
    clog('hostReach', 'device=offline');
  });
  window.addEventListener('focus', retryAll);
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') retryAll();
  });
  try { navigator.connection?.addEventListener?.('change', retryAll); } catch (_) {}
}
