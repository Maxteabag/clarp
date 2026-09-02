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

export function getAuthToken() {
  try { return localStorage.getItem(AUTH_KEY) || ''; } catch (_) { return ''; }
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

export function installAuthFetch() {
  const nativeFetch = window.fetch.bind(window);
  window.fetch = function (input, init) {
    const path = pathOf(input);
    const token = getAuthToken();
    const opts = { ...(init || {}) };
    if (token) {
      const h = new Headers(opts.headers || {});
      if (!h.has('Authorization')) h.set('Authorization', 'Bearer ' + token);
      opts.headers = h;
    }
    return nativeFetch(input, token ? opts : init).then(
      (res) => { noteFetch(health, { path, ok: res.ok }); return res; },
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
  const row = { event, detail: String(detail ?? '') };
  if (trace.id) row.trace_id = trace.id;
  if (extra && typeof extra === 'object') Object.assign(row, extra);
  buf.push(row);
  if (buf.length >= 32) { flush(); return; }
  if (!timer) timer = setTimeout(flush, flushMs);
}

export function flushClog() { flush(); }
