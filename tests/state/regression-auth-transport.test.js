// Hardening sweep — the PWA's two auth transports.
//
// The client authenticates in two incompatible ways at once. `fetch` gets an
// `Authorization: Bearer` header injected by installAuthFetch()
// (web/src/lib/net.js:77-98). EventSource, <img> and <audio> cannot send
// headers, so they rely on the `claude_pwa_token` cookie written by
// bootstrapAuth() (web/src/lib/net.js:26-39); the server accepts either
// (server/server.py:664-676).
//
// The two do not have the same lifetime, and nothing reconciles them. These
// pin what happens when only one of them survives.

import { describe, it, expect, beforeEach, vi } from 'vitest';
import {
  Health, assess, createHealth, noteFetch, noteSse,
} from '@core/client-health.js';

const AUTH_KEY = 'claude-pwa.auth-token';
const TOKEN = 'a-real-looking-token-0123456789';

function makeEnv({ href = 'https://clarp.test/', stored = '', cookie = '' } = {}) {
  const store = new Map();
  if (stored) store.set(AUTH_KEY, stored);
  const jar = cookie ? [cookie] : [];
  const doc = {
    get cookie() { return jar.join('; '); },
    set cookie(v) { jar.push(String(v)); },
  };
  const localStorage = {
    getItem: k => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => { store.set(k, String(v)); },
    removeItem: k => { store.delete(k); },
  };
  const replaced = [];
  const win = { location: { href, origin: 'https://clarp.test' } };
  vi.stubGlobal('window', win);
  vi.stubGlobal('location', win.location);
  vi.stubGlobal('document', doc);
  vi.stubGlobal('localStorage', localStorage);
  vi.stubGlobal('history', { replaceState: (...a) => replaced.push(a) });
  return { store, jar, doc, replaced, win };
}

async function loadNet() {
  vi.resetModules();
  return import('../../web/src/lib/net.js');
}

beforeEach(() => { vi.unstubAllGlobals(); });

describe('bootstrapAuth', () => {
  it('stashes the token and writes the headerless cookie on the first visit', async () => {
    const env = makeEnv({ href: `https://clarp.test/?token=${TOKEN}` });
    const net = await loadNet();
    net.bootstrapAuth();
    expect(env.store.get(AUTH_KEY)).toBe(TOKEN);
    expect(env.doc.cookie).toContain(`claude_pwa_token=${TOKEN}`);
    // And it is stripped from the URL so it never reaches a bookmark.
    expect(env.replaced).toHaveLength(1);
  });

  // BUG. bootstrapAuth() returns at web/src/lib/net.js:30 (`if (!t) return;`)
  // whenever the URL carries no `?token=`, which is every load after the
  // first. The cookie is therefore only ever written on the one visit that
  // carried the link, while localStorage keeps the token forever.
  //
  // Once the cookie is gone and the token is not — browser restart (see the
  // next test), a cookie purge, Safari ITP's 7-day cap on script-written
  // cookies, or opening the same host on a different port — every `fetch`
  // still succeeds via the header and only `GET /events` gets a 401. The app
  // then looks alive (roster, transcript, sends all work) with a permanently
  // dead event stream.
  //
  // FIX: drop the early return. Read the token from `?token=` *or*
  // localStorage and (re)write the cookie on every load.
  it.fails('rewrites the cookie from the stored token when the URL has none', async () => {
    const env = makeEnv({ href: 'https://clarp.test/', stored: TOKEN });
    const net = await loadNet();
    net.bootstrapAuth();
    expect(net.getAuthToken()).toBe(TOKEN);
    expect(env.doc.cookie).toContain(`claude_pwa_token=${TOKEN}`);
  });

  // BUG. The cookie is written with `Path=/; SameSite=Lax` and nothing else
  // (web/src/lib/net.js:33-34). With no Max-Age and no Expires it is a
  // *session* cookie: it dies when the browser is closed. localStorage does
  // not. So the very first browser restart puts every install into the split
  // state above, without any user action at all.
  //
  // FIX: `Max-Age=<long>` (plus `Secure` when the origin is https), and treat
  // the localStorage copy as the source of truth that reseeds it.
  it.fails('gives the cookie a lifetime instead of leaving it session-scoped', async () => {
    const env = makeEnv({ href: `https://clarp.test/?token=${TOKEN}` });
    const net = await loadNet();
    net.bootstrapAuth();
    const written = env.jar[env.jar.length - 1];
    expect(written).toMatch(/max-age=|expires=/i);
  });
});

describe('the header transport survives what the cookie transport does not', () => {
  // Axiom, and the reason the split is invisible: with the cookie gone the
  // header path is untouched, so every diagnostic the client has — the health
  // record, the auth banner, the roster — reports a healthy client.
  it('still authenticates fetch from localStorage with no cookie at all', async () => {
    makeEnv({ href: 'https://clarp.test/', stored: TOKEN });
    const seen = [];
    const native = vi.fn(async (input, init) => {
      seen.push(new Headers((init && init.headers) || {}).get('Authorization'));
      return { ok: true, status: 200 };
    });
    window.fetch = native;
    const net = await loadNet();
    net.installAuthFetch();
    await window.fetch('/agents/snapshot');
    expect(seen[0]).toBe(`Bearer ${TOKEN}`);
    expect(net.health.lastFetchAt).toBeGreaterThan(0);
  });

  // BUG (design). `withToken()` is the one hook the headerless transports go
  // through, and it is the identity function (web/src/lib/net.js:106-108).
  // The server does accept `?token=` (server/server.py:682-686), so there is
  // a working fallback available and it is not used. With the cookie missing
  // there is nothing left for EventSource to present.
  //
  // FIX: either make withToken() append the token when one is stored (the
  // server already supports it), or commit to the cookie and guarantee it is
  // present before connectSSE() runs. Doing neither is what leaves the gap.
  it.fails('gives EventSource a credential when the cookie is unavailable', async () => {
    makeEnv({ href: 'https://clarp.test/', stored: TOKEN });
    const net = await loadNet();
    const url = net.withToken('/events');
    expect(url).toContain(TOKEN);
  });
});

describe('a 401 only the event stream sees', () => {
  // Axiom: the auth banner in App.svelte:207 is driven entirely by
  // onAuthChange, and onAuthChange only ever fires from the fetch wrapper
  // (web/src/lib/net.js:91). EventSource surfaces no status code to JS, and
  // sse.svelte.js never probes for one, so a stream-only 401 cannot reach it.
  it('never reaches the auth listener, because only fetch reports status', async () => {
    makeEnv({ href: 'https://clarp.test/', stored: TOKEN });
    window.fetch = vi.fn(async () => ({ ok: true, status: 200 }));
    const net = await loadNet();
    const seen = [];
    net.onAuthChange(v => seen.push(v));
    net.installAuthFetch();
    // Everything the app does over fetch is fine; only /events is refused.
    await window.fetch('/agents/snapshot');
    await window.fetch('/log?session=lena');
    expect(seen).toEqual([]);
    // Health agrees: the client looks perfectly well.
    expect(net.health.fetchErrors).toBe(0);
    expect(net.health.rejected).toBe(0);
  });
});

describe('assess() when the stream is alive and every fetch is refused', () => {
  // Axiom, and the inverse of the split above: when the *cookie* is the one
  // that still works, SSE keeps delivering events and noteSse() keeps
  // advancing lastSseAt, so the plain silence test would call the client OK.
  // The refusal check runs first (static/lib/client-health.js:69-71), so the
  // verdict is still UNAUTHORIZED. That ordering is what makes issue #10's
  // fix hold in both directions.
  it('still reports unauthorized, not ok', () => {
    const h = createHealth(0);
    for (let i = 0; i < 20; i++) {
      noteFetch(h, { path: '/log', ok: false, status: 401, at: 1000 + i * 1000 });
      noteSse(h, { type: 'agent-activity', at: 1000 + i * 1000 });
    }
    const v = assess(h, { now: 25_000 });
    expect(v.state).toBe(Health.UNAUTHORIZED);
    expect(h.lastSseAt).toBe(20_000);
  });

  // Axiom: the threshold is one. `rejected` counts *consecutive* 401s and is
  // reset by any other status (client-health.js:45), so the guard reduces to
  // "the most recent response was a 401" — a single refusal arriving after a
  // hundred successes flips the whole client to UNAUTHORIZED and pops the
  // "Server rejected the saved token" banner (App.svelte:207-212).
  //
  // Scope violations return 403, not 401 (server/server.py:747, :2252,
  // :3343), so a limited-scope device does not trip this today. It is a
  // one-sample verdict on a shared signal all the same; requiring two
  // consecutive refusals would cost nothing, since a wrong token refuses
  // every request.
  it('needs exactly one refusal to condemn the whole client', () => {
    const h = createHealth(0);
    for (let i = 0; i < 100; i++) noteFetch(h, { path: '/log', ok: true, status: 200, at: 1000 + i });
    expect(assess(h, { now: 1200 }).state).toBe(Health.OK);
    noteFetch(h, { path: '/orchestrator/settings', ok: false, status: 401, at: 1200 });
    expect(h.rejected).toBe(1);
    expect(assess(h, { now: 1300 }).state).toBe(Health.UNAUTHORIZED);
  });
});
