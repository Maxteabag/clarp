// When a Host does not answer (docs/design/host-not-answering.md): the rules every web view
// shares, framework-free so the vitest suite can pin them.
//
// - the per-Host reachability record and its two transitions (an answer, a failure);
// - the reconnect backoff (2 s, 5 s, 10 s, 30 s, then every 60 s);
// - the classifier: which plain reason the last failed attempt most likely means;
// - the copy, word for word the same on every platform;
// - "Since 09:14", the quiet status line, and the private-data-free diagnostics text.
//
// The store (web/src/stores/reach.svelte.js) owns timers, fetches and Svelte state.

// ---- copy (exact strings, shared spec) -------------------------------------------------------

/**
 * The English of every string the panel shows, by its key in the web app's catalogs
 * (web/src/lib/i18n/en.json carries the same; a test keeps them equal). The views pass their
 * `t` as `tr`; without one these English strings are used (the diagnostics, the tests).
 */
export const EN = Object.freeze({
  'host_down.title': '{host} isn\'t answering',
  'host_down.since': 'Since {time}',
  'host_down.yesterday': 'yesterday {time}',
  'host_down.reconnecting': 'Reconnecting automatically…',
  'host_down.trying_in_s': 'Trying again in {n} s',
  'host_down.trying_in_min': 'Trying again in {n} min',
  'host_down.back_online': 'Back online',
  'host_down.read_only': 'Showing saved messages. You can send again when {host} is back.',
  'host_down.row': '{host} isn\'t answering · since {time}',
  'host_down.device_offline': 'This device is offline',
  'host_down.try_again': 'Try again',
  'host_down.check_tailscale': 'Check Tailscale',
  'host_down.pair_again': 'Pair again',
  'host_down.edit_address': 'Edit address',
  'host_down.switch_host': 'Switch Host',
  'host_down.copy_diagnostics': 'Copy diagnostics',
  'host_down.copied': 'Diagnostics copied',
  'host_down.close': 'Close',
  'host_down.token_or_link': 'Token or ?token= link',
  'host_down.token': 'Token',
  'host_down.token_needed': 'Paste the token or the link clarp-admin url prints.',
  'host_down.reason.device_offline': 'This device is offline.',
  'host_down.reason.auth_expired': 'This device is no longer signed in to {host}.',
  'host_down.reason.wrong_address': 'Nothing at this address is {host}. The address may have changed.',
  'host_down.reason.server_stopped': 'The computer is on, but Clarp isn\'t running on it.',
  'host_down.reason.tailscale_off': 'Tailscale doesn\'t seem to be connected on this device.',
  'host_down.reason.host_asleep': 'The computer seems to be off or asleep.',
  'host_down.reason.unknown': '{host} isn\'t responding right now.',
});

/** The English translator: `{name}` placeholders filled from params. */
export function english(key, params) {
  const msg = EN[key] ?? key;
  if (!params) return msg;
  return msg.replace(/\{(\w+)\}/g, (whole, name) => (params[name] == null ? whole : String(params[name])));
}

/** What each reason offers besides Try again. */
export const REASON_ACTIONS = Object.freeze({
  device_offline: '', auth_expired: 'pair', wrong_address: 'edit', server_stopped: '',
  tailscale_off: 'tailscale', host_asleep: '', unknown: '',
});

export function reasonAction(id) {
  return REASON_ACTIONS[id] ?? '';
}

export function reasonSentence(id, host, tr = english) {
  const key = Object.hasOwn(REASON_ACTIONS, id) ? id : 'unknown';
  return tr(`host_down.reason.${key}`, { host });
}

/** Which picture goes with a reason: calm, never an alarm. */
export function reasonIcon(id) {
  return {
    auth_expired: 'key', wrong_address: 'signpost', server_stopped: 'plug',
    tailscale_off: 'network', host_asleep: 'moon',
  }[id] || 'cloud-off';
}

// ---- backoff ----------------------------------------------------------------------------------

export const BACKOFF_MS = Object.freeze([2000, 5000, 10000, 30000, 60000]);
export const PROBE_TIMEOUT_MS = 8000;
export const BACK_ONLINE_MS = 2000;

/** The wait before the next attempt after `failures` failed ones (1 = the first failure). */
export function backoffDelay(failures) {
  const i = Math.max(0, Math.min(BACKOFF_MS.length - 1, (failures | 0) - 1));
  return BACKOFF_MS[i];
}

// ---- the record and its transitions ----------------------------------------------------------

/**
 * A Host's reachability. `lastOk` and `downSince` are what a restart keeps (persisted by the
 * store), so "since" stays true across a reload.
 */
export function blankReach({ lastOk = 0, downSince = 0 } = {}) {
  return {
    down: false,
    /** First failure after the last success (ms), or 0 while it answers. */
    downSince: downSince > lastOk ? downSince : 0,
    lastOk,
    /** Failed attempts since it went down. */
    failures: 0,
    /** The last failed attempt: {kind, status?, ms?}. */
    attempt: null,
    reason: '',
    /** When the next attempt is due (ms), or 0. */
    nextAt: 0,
    inFlight: false,
    /** Until when "Back online" shows after a recovery (ms), or 0. */
    backUntil: 0,
    hostVersion: '',
    contract: 0,
  };
}

/** The Host answered. */
export function reachOk(rec, now, info = {}) {
  const wasDown = rec.down;
  return {
    ...rec,
    down: false, downSince: 0, lastOk: now, failures: 0, attempt: null, reason: '',
    nextAt: 0, inFlight: false,
    backUntil: wasDown ? now + BACK_ONLINE_MS : rec.backUntil,
    hostVersion: info.hostVersion || rec.hostVersion,
    contract: info.contract || rec.contract,
  };
}

/** An attempt failed; `reason` comes from classify(). */
export function reachFail(rec, attempt, reason, now) {
  const failures = rec.failures + 1;
  return {
    ...rec,
    down: true,
    downSince: rec.downSince || now,
    failures, attempt, reason, inFlight: false, backUntil: 0,
    nextAt: now + backoffDelay(failures),
  };
}

// ---- addresses --------------------------------------------------------------------------------

function ipv4(host) {
  const m = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/.exec(host);
  if (!m) return null;
  const parts = m.slice(1).map(Number);
  return parts.every(p => p <= 255) ? parts : null;
}

/** What kind of address a Host has: tailnet, lan, loopback, public or unknown. */
export function addressKind(url) {
  let host = '';
  try { host = new URL(String(url || '')).hostname.toLowerCase(); } catch (_) { return 'unknown'; }
  if (!host) return 'unknown';
  host = host.replace(/^\[|\]$/g, '');
  if (host === 'localhost' || host.endsWith('.localhost') || host === '::1') return 'loopback';
  if (host.endsWith('.ts.net') || host.startsWith('fd7a:115c:a1e0:')) return 'tailnet';
  const v4 = ipv4(host);
  if (v4) {
    const [a, b] = v4;
    if (a === 127) return 'loopback';
    if (a === 100 && b >= 64 && b <= 127) return 'tailnet';
    if (a === 10 || (a === 172 && b >= 16 && b <= 31) || (a === 192 && b === 168)
        || (a === 169 && b === 254)) return 'lan';
    return 'public';
  }
  if (host.includes(':')) {
    if (/^fe[89ab]/.test(host) || /^f[cd]/.test(host)) return 'lan';
    return 'public';
  }
  if (host.endsWith('.local') || host.endsWith('.lan') || host.endsWith('.home.arpa')
      || !host.includes('.')) return 'lan';
  return 'public';
}

export function schemeOf(url) {
  try { return new URL(String(url || '')).protocol.replace(':', '') || 'unknown'; } catch (_) { return 'unknown'; }
}

// ---- attempts ---------------------------------------------------------------------------------

/**
 * A failed browser fetch as an attempt. A page cannot see why a request failed, only how: our own
 * timeout fired, or the request failed by itself. For the Host serving this page that failure is
 * a refused connection (its name resolved when the page loaded, and the same origin has no CORS);
 * for another Host it could be a refused connection, an unknown name or a missing CORS listing.
 */
export function fetchFailure({ timedOut = false, sameOrigin = false, ms = 0 } = {}) {
  if (timedOut) return { kind: 'timeout', ms };
  return { kind: sameOrigin ? 'refused' : 'network', ms };
}

/** An HTTP answer that is not a usable /server-info, as an attempt; null when it is fine. */
export function answerFailure(status, { json = true, serverId = '', expectedId = '' } = {}) {
  if (status === 401 || status === 403) return { kind: 'http', status };
  if (status === 404) return { kind: 'not_this_host', status };
  if (status < 200 || status >= 300) return { kind: 'http', status };
  if (!json) return { kind: 'not_this_host', status };
  if (expectedId && serverId && serverId !== expectedId) return { kind: 'not_this_host', status };
  return null;
}

/**
 * A peer's error as the serving Host reports it (GET /peers/snapshot, peers/transport.rs) as an
 * attempt. The Host reads its peers itself, so this device's Tailscale plays no part.
 */
export function peerAttempt(code) {
  const c = String(code || '');
  if (c === 'refused') return { kind: 'http', status: 401 };
  if (c === 'timeout') return { kind: 'timeout', ms: 0 };
  if (c === 'connect_refused' || c === 'connection_refused') return { kind: 'refused' };
  if (c === 'dns' || c === 'name_not_found') return { kind: 'dns' };
  if (c === 'tls') return { kind: 'tls' };
  if (c === 'not_json' || c === 'redirect_refused') return { kind: 'not_this_host' };
  const http = /^http_(\d{3})$/.exec(c);
  if (http) {
    const status = Number(http[1]);
    return status === 404 ? { kind: 'not_this_host', status } : { kind: 'http', status };
  }
  return { kind: 'network', via: 'host' };
}

/** The attempt in the diagnostics' words. */
export function attemptLabel(attempt) {
  if (!attempt) return 'none';
  switch (attempt.kind) {
    case 'timeout': return attempt.ms ? `timeout after ${Math.round(attempt.ms / 1000)} s` : 'timeout';
    case 'refused': return 'connection refused';
    case 'dns': return 'name not found';
    case 'tls': return 'TLS error';
    case 'http': return `HTTP ${attempt.status}`;
    case 'not_this_host': return attempt.status && attempt.status !== 200 ? `not this Host (HTTP ${attempt.status})` : 'not this Host';
    case 'network': return attempt.via === 'host' ? 'unreachable from the serving Host' : 'network error (refused, name or CORS)';
    default: return String(attempt.kind || 'unknown');
  }
}

// ---- classifier -------------------------------------------------------------------------------

/**
 * The most likely reason, first match wins (the spec's table).
 *
 * - `deviceOnline`: false when the OS says this device has no network.
 * - `address`: addressKind() of the Host.
 * - `viaHost`: the attempt was made by the serving Host (a peer), not by this device.
 * - `deviceTunnel`: true/false when this device's VPN/Tailscale is observable, else null.
 * - `others`: the other Hosts this device reads directly, [{address, ok}].
 */
export function classify(attempt, { deviceOnline = true, address = 'unknown', viaHost = false,
  deviceTunnel = null, others = [] } = {}) {
  if (deviceOnline === false) return 'device_offline';
  if (!attempt) return 'unknown';
  const { kind, status } = attempt;
  if (kind === 'http' && (status === 401 || status === 403)) return 'auth_expired';
  if (kind === 'dns' || kind === 'tls' || kind === 'not_this_host') return 'wrong_address';
  if (kind === 'refused') return 'server_stopped';
  // A proxy in front of the Host (tailscale serve, a reverse proxy) answered, Clarp behind it did not.
  if (kind === 'http' && status === 502) return 'server_stopped';
  const silent = kind === 'timeout' || kind === 'network';
  if (silent && address === 'tailnet' && !viaHost) {
    const tailnetOthers = others.filter(o => o.address === 'tailnet');
    if (deviceTunnel === false || (tailnetOthers.length > 0 && !tailnetOthers.some(o => o.ok))) {
      return 'tailscale_off';
    }
  }
  if (kind === 'timeout' && (viaHost || others.some(o => o.ok))) return 'host_asleep';
  return 'unknown';
}

// ---- time -------------------------------------------------------------------------------------

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const pad = n => String(n).padStart(2, '0');

/**
 * `HH:mm` today, `yesterday HH:mm`, else `d MMM HH:mm`; 24-hour clock, local time. In another
 * language "yesterday" comes from `tr` and the month's short name from Intl.
 */
export function sinceTime(ts, now = Date.now(), tr = english, locale = 'en') {
  const d = new Date(ts);
  const n = new Date(now);
  const hm = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  const day = x => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const days = Math.round((day(n) - day(d)) / 86400000);
  if (days <= 0) return hm;
  if (days === 1) return tr('host_down.yesterday', { time: hm });
  let month = MONTHS[d.getMonth()];
  if (locale && locale !== 'en') {
    try { month = new Intl.DateTimeFormat(locale, { month: 'short' }).format(d); } catch (_) {}
  }
  return `${d.getDate()} ${month} ${hm}`;
}

/** The quiet status line under the panel. */
export function statusLine(rec, now = Date.now(), tr = english) {
  if (!rec) return '';
  if (!rec.down && rec.backUntil > now) return tr('host_down.back_online');
  if (!rec.down) return '';
  if (rec.inFlight || !rec.nextAt) return tr('host_down.reconnecting');
  const s = Math.max(1, Math.ceil((rec.nextAt - now) / 1000));
  if (s < 60) return tr('host_down.trying_in_s', { n: s });
  return tr('host_down.trying_in_min', { n: Math.round(s / 60) });
}

function duration(ms) {
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 60) return `${s} s`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m} min`;
  const h = Math.floor(m / 60);
  if (h < 48) return `${h} h ${m % 60} min`;
  return `${Math.floor(h / 24)} d`;
}

// ---- diagnostics ------------------------------------------------------------------------------

/**
 * sha256 of a string as hex. Written out because crypto.subtle exists only in secure contexts,
 * and a Host on http://100.x.y.z is not one.
 */
export function sha256Hex(text) {
  const bytes = new TextEncoder().encode(String(text));
  const K = new Uint32Array([
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
  ]);
  const H = new Uint32Array([
    0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19,
  ]);
  const len = bytes.length;
  const total = Math.ceil((len + 9) / 64) * 64;
  const msg = new Uint8Array(total);
  msg.set(bytes);
  msg[len] = 0x80;
  const view = new DataView(msg.buffer);
  view.setUint32(total - 8, Math.floor(len / 0x20000000));
  view.setUint32(total - 4, (len << 3) >>> 0);
  const W = new Uint32Array(64);
  const rotr = (x, n) => (x >>> n) | (x << (32 - n));
  for (let off = 0; off < total; off += 64) {
    for (let i = 0; i < 16; i++) W[i] = view.getUint32(off + i * 4);
    for (let i = 16; i < 64; i++) {
      const s0 = rotr(W[i - 15], 7) ^ rotr(W[i - 15], 18) ^ (W[i - 15] >>> 3);
      const s1 = rotr(W[i - 2], 17) ^ rotr(W[i - 2], 19) ^ (W[i - 2] >>> 10);
      W[i] = (W[i - 16] + s0 + W[i - 7] + s1) >>> 0;
    }
    let [a, b, c, d, e, f, g, h] = H;
    for (let i = 0; i < 64; i++) {
      const S1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25);
      const ch = (e & f) ^ (~e & g);
      const t1 = (h + S1 + ch + K[i] + W[i]) >>> 0;
      const S0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22);
      const maj = (a & b) ^ (a & c) ^ (b & c);
      const t2 = (S0 + maj) >>> 0;
      h = g; g = f; f = e; e = (d + t1) >>> 0; d = c; c = b; b = a; a = (t1 + t2) >>> 0;
    }
    H[0] += a; H[1] += b; H[2] += c; H[3] += d; H[4] += e; H[5] += f; H[6] += g; H[7] += h;
  }
  return Array.from(H, x => x.toString(16).padStart(8, '0')).join('');
}

/**
 * The text "Copy diagnostics" puts on the clipboard: at most 15 lines, and nothing private. No
 * token, address, host name, user, path, chat or message; the Host is a 6-hex hash of its id.
 */
export function diagnosticsText({
  app = 'web', version = '', build = '', hostKey = '', url = '', rec = blankReach(),
  othersOk = 0, othersTotal = 0, deviceOnline = null, network = '', tunnel = null,
  now = Date.now(),
} = {}) {
  const iso = t => new Date(t).toISOString().replace(/\.\d{3}Z$/, 'Z');
  const reason = rec.reason || 'unknown';
  const sentence = reasonSentence(reason, 'the Host');
  const net = deviceOnline === false ? 'offline'
    : deviceOnline === true ? `online${network ? ` (${network})` : ''}` : 'unknown';
  const yn = v => (v === true ? 'yes' : v === false ? 'no' : 'unknown');
  const next = rec.nextAt ? Math.max(0, Math.round((rec.nextAt - now) / 1000)) : 0;
  const hostVersion = rec.hostVersion
    ? `${rec.hostVersion}${rec.contract ? ` · contract ${rec.contract}` : ''}`
    : (rec.contract ? `unknown · contract ${rec.contract}` : 'unknown');
  const state = rec.down && rec.downSince
    ? `not answering since ${iso(rec.downSince)} (${duration(now - rec.downSince)})`
    : 'answering';
  return [
    'Clarp diagnostics',
    `App: ${app} ${version || 'unknown'} (${build || 'unknown'})`,
    `Host: #${sha256Hex(hostKey || url).slice(0, 6)} · address kind: ${addressKind(url)} · scheme: ${schemeOf(url)}`,
    `Host version: ${hostVersion}`,
    `State: ${state}`,
    `Likely reason: ${reason} — ${sentence[0].toUpperCase()}${sentence.slice(1)}`,
    `Last attempt: ${attemptLabel(rec.attempt)}`,
    `Attempts since down: ${rec.failures} · next in ${next} s`,
    `Other Hosts answering: ${othersOk} of ${othersTotal}`,
    `Device network: ${net} · Tailscale/VPN on device: ${yn(tunnel)}`,
    `Time: ${iso(now)}`,
  ].join('\n');
}
