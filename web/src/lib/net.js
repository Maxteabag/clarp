// Auth bootstrap + batched client event logging.
//
// The server reads `auth_token` from config.toml. When non-empty, every
// request must carry `Authorization: Bearer <token>`. Headerless transports
// (EventSource, <img>, <audio>) use the same-origin cookie set below.
//
// On first visit we accept the token via `?token=...`, stash it in
// localStorage, and strip it from the URL so it never reaches a bookmark.
// Later loads read it from storage. With no token stored, requests go out
// unauthenticated — correct when the server has auth disabled.

import {
  createHealth, noteFetch, noteSse, registerModule,
} from '@core/client-health.js';
import { clientHeader } from '@core/render-spec.js';
import { version as appVersion } from '../../../package.json';

// Who this client is, on every request it makes (X-Clarp-Client, docs/compatibility.md): the
// web app's own name and contract series. The Host decides per client what it may draw
// (render_spec: server_identity.RENDER_SPEC_MIN_CONTRACT).
export const CLIENT_HEADER_VALUE = clientHeader(appVersion);

// A fresh id per module evaluation: a second one for the same module means a
// hot reload left two live copies, which is what silently wedges the app.
let moduleSeq = 0;
export function instanceId(name) {
  moduleSeq += 1;
  return `${name}-${Date.now().toString(36)}-${moduleSeq}`;
}

const AUTH_KEY = 'claude-pwa.auth-token';

export function bootstrapAuth() {
  try {
    const url = new URL(window.location.href);
    const t = url.searchParams.get('token');
    if (!t) return;
    try { localStorage.setItem(AUTH_KEY, t); } catch (_) {}
    try {
      document.cookie = 'claude_pwa_token=' + encodeURIComponent(t) +
        '; Path=/; SameSite=Lax';
    } catch (_) {}
    url.searchParams.delete('token');
    history.replaceState(null, '', url.pathname + url.search + url.hash);
  } catch (_) {}
}

/** The web app's own version (package.json), for the diagnostics text. */
export const APP_VERSION = appVersion;

/**
 * Saves a new token for this Host (the Host-down panel's Pair again): what a fresh `?token=`
 * link does, without the reload. Accepts the token or the whole link.
 */
export function setAuthToken(input) {
  let t = String(input || '').trim();
  try {
    const fromLink = new URL(t, window.location.href).searchParams.get('token');
    if (fromLink) t = fromLink;
  } catch (_) {}
  if (!t || /\s/.test(t)) return false;
  try { localStorage.setItem(AUTH_KEY, t); } catch (_) {}
  try {
    document.cookie = 'claude_pwa_token=' + encodeURIComponent(t) + '; Path=/; SameSite=Lax';
  } catch (_) {}
  return true;
}

export function getAuthToken() {
  try { return localStorage.getItem(AUTH_KEY) || ''; } catch (_) { return ''; }
}

// A 401 means the saved token is stale or wrong, and the only way back is a
// fresh `?token=` link. The store is told once per transition so the shell can
// show how to get that link (issue #10); no store is imported here to keep
// this module free of UI dependencies.
let authListener = null;
let authKnownRejected = false;
export function onAuthChange(fn) { authListener = fn; }
function authRejected() {
  if (authKnownRejected) return;
  authKnownRejected = true;
  try { authListener && authListener(true); } catch (_) {}
}
function authAccepted() {
  if (!authKnownRejected) return;
  authKnownRejected = false;
  try { authListener && authListener(false); } catch (_) {}
}

// Wrap window.fetch so no call site has to remember the header. Kept as a
// global override rather than an exported helper because the vendored
// libraries and the audio element issue their own requests.
// Every request the app makes passes through here, which makes it the one
// place that knows whether this client is still reaching the server at all.
export const health = createHealth();

function pathOf(input) {
  try {
    const url = typeof input === 'string' ? input : (input && input.url) || '';
    return url.replace(/^https?:\/\/[^/]+/, '').split('?')[0];
  } catch (_) { return ''; }
}

function sameOrigin(input) {
  try {
    // A string, a URL object or a Request; anything else is not known to be this Host's.
    const url = typeof input === 'string' ? input
      : input instanceof URL ? input.href
      : input && typeof input.url === 'string' ? input.url : null;
    if (url === null) return false;
    return new URL(url, window.location.href).origin === window.location.origin;
  } catch (_) {
    return false;
  }
}

export function installAuthFetch() {
  const nativeFetch = window.fetch.bind(window);
  window.fetch = function (input, init) {
    const path = pathOf(input);
    const token = getAuthToken();
    const opts = { ...(init || {}) };
    const h = new Headers(opts.headers || (input instanceof Request ? input.headers : {}));
    // This Host's token goes to this Host only: another Host's request (the all-Hosts list)
    // carries that Host's own token, set by its caller, and never this one.
    if (token && !h.has('Authorization') && sameOrigin(input)) h.set('Authorization', 'Bearer ' + token);
    if (!h.has('X-Clarp-Client')) h.set('X-Clarp-Client', CLIENT_HEADER_VALUE);
    opts.headers = h;
    return nativeFetch(input, opts).then(
      (res) => {
        noteFetch(health, { path, ok: res.ok, status: res.status });
        if (res.status === 401) authRejected();
        else if (res.ok) authAccepted();
        return res;
      },
      (err) => { noteFetch(health, { path, ok: false }); throw err; },
    );
  };
}

export function noteSseEvent(type) {
  noteSse(health, { type });
}

// Headerless transports authenticate via the same-origin cookie set during
// bootstrap; keep URLs token-free so logs and iframe srcs stay clean.
export function withToken(url) {
  return url;
}

// ---- batched client events ---------------------------------------------
//
// Every call appends to a buffer flushed on a short timer, or immediately at
// 32 events, as a single POST. Turns the request count from O(events) into
// O(seconds_of_activity).

const buf = [];
let timer = null;
let flushMs = 1000;

export const trace = { id: '' };

registerModule(globalThis, 'net', instanceId('net'));

function flush() {
  timer = null;
  if (!buf.length) return;
  const events = buf.splice(0, buf.length);
  try {
    fetch('/clog', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ events }),
      keepalive: true,
    }).catch(() => {});
  } catch (_) {}
}

export function setClogFlushInterval(ms) {
  flushMs = ms;
}

export function clog(event, detail, extra) {
  // Objects go through intact: the server stores detail as JSON and the
  // audio_faults view reads fields out of it.
  const structured = detail !== null && typeof detail === 'object';
  const row = { event, detail: structured ? detail : String(detail ?? '') };
  if (trace.id) row.trace_id = trace.id;
  if (extra && typeof extra === 'object') Object.assign(row, extra);
  buf.push(row);
  if (buf.length >= 32) { flush(); return; }
  if (!timer) timer = setTimeout(flush, flushMs);
}

export function flushClog() { flush(); }
