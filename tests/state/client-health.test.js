// A wedged client looks identical to an idle one on screen. These pin the
// signal that separates them, and the hot-reload duplicate-store condition
// that caused the wedge on 2026-09-01.

import { describe, it, expect } from 'vitest';
import {
  Health, createHealth, noteFetch, noteSse, assess, formatAge,
  registerModule, duplicatedModules,
} from '@core/client-health.js';

describe('assess', () => {
  it('is ok right after a response', () => {
    const h = noteFetch(createHealth(0), { path: '/log', at: 1000 });
    expect(assess(h, { now: 1200 }).state).toBe(Health.OK);
  });

  it('goes stale, then wedged, as the silence grows', () => {
    const h = noteFetch(createHealth(0), { at: 1000 });
    expect(assess(h, { now: 1000 + 46000 }).state).toBe(Health.STALE);
    expect(assess(h, { now: 1000 + 121000 }).state).toBe(Health.WEDGED);
  });

  it('counts an SSE event as contact', () => {
    const h = createHealth(0);
    noteFetch(h, { at: 1000 });
    noteSse(h, { type: 'agent-activity', at: 100000 });
    expect(assess(h, { now: 101000 }).state).toBe(Health.OK);
  });

  it('reports how long the silence has lasted', () => {
    const h = noteFetch(createHealth(0), { at: 1000 });
    expect(assess(h, { now: 61000 }).sinceMs).toBe(60000);
  });

  it('allows for boot before judging a client that has heard nothing', () => {
    const h = createHealth(0);
    expect(assess(h, { now: 5000 }).state).toBe(Health.QUIET);
    expect(assess(h, { now: 50000 }).state).toBe(Health.STALE);
    expect(assess(h, { now: 200000 }).state).toBe(Health.WEDGED);
    expect(assess(h, { now: 200000 }).reason).toBe('never reached the server');
  });

  it('does not treat a failed request as contact', () => {
    const h = createHealth(0);
    noteFetch(h, { at: 1000, ok: false });
    expect(assess(h, { now: 200000 }).state).toBe(Health.WEDGED);
    expect(h.fetchErrors).toBe(1);
  });

  // Issue #10: a server that answers 401 to everything is reachable. Calling
  // that "never reached the server" sends people to debug the network when the
  // problem is the saved token.
  it('separates a server that refuses from one that is absent', () => {
    const h = createHealth(0);
    for (let i = 0; i < 50; i++) noteFetch(h, { path: '/events', ok: false, status: 401, at: 1000 + i * 3000 });
    const v = assess(h, { now: 200000 });
    expect(v.state).toBe(Health.UNAUTHORIZED);
    expect(v.reason).toBe('server rejected the token');
    expect(h.lastResponseAt).toBe(1000 + 49 * 3000);
    expect(h.lastFetchAt).toBe(0);
  });

  it('does not call an answering server unreachable', () => {
    const h = createHealth(0);
    noteFetch(h, { path: '/log', ok: false, status: 500, at: 1000 });
    const v = assess(h, { now: 200000 });
    expect(v.state).toBe(Health.WEDGED);
    expect(v.reason).not.toBe('never reached the server');
    expect(v.reason).toMatch(/500/);
  });

  it('keeps counting error responses as contact after an earlier success', () => {
    const h = createHealth(0);
    noteFetch(h, { path: '/status', ok: true, status: 200, at: 1000 });
    for (let at = 50000; at <= 200000; at += 50000) {
      noteFetch(h, { path: '/status', ok: false, status: 503, at });
    }

    const v = assess(h, { now: 200001 });
    expect(v.sinceMs).toBe(1);
    expect(v.reason).toMatch(/503/);
    expect(v.reason).not.toBe('no server contact');
  });

  it('clears the unauthorized verdict once a request succeeds', () => {
    const h = createHealth(0);
    noteFetch(h, { path: '/events', ok: false, status: 401, at: 1000 });
    expect(assess(h, { now: 2000 }).state).toBe(Health.UNAUTHORIZED);
    noteFetch(h, { path: '/log', ok: true, status: 200, at: 3000 });
    expect(assess(h, { now: 4000 }).state).toBe(Health.OK);
    expect(h.rejected).toBe(0);
  });

  it('reports unauthorized before quiet even right after load', () => {
    const h = createHealth(0);
    noteFetch(h, { path: '/agents/snapshot', ok: false, status: 401, at: 500 });
    expect(assess(h, { now: 600 }).state).toBe(Health.UNAUTHORIZED);
  });

  it('still treats a network failure as no contact', () => {
    const h = createHealth(0);
    noteFetch(h, { path: '/log', ok: false, at: 1000 });
    expect(h.lastResponseAt).toBe(0);
    expect(assess(h, { now: 200000 }).reason).toBe('never reached the server');
  });

  it('counts traffic for the panel', () => {
    const h = createHealth(0);
    noteFetch(h, { path: '/log', at: 1 });
    noteFetch(h, { path: '/sessions', at: 2 });
    noteSse(h, { type: 'transcript-updated', at: 3 });
    expect(h).toMatchObject({ fetches: 2, sseEvents: 1, lastFetchPath: '/sessions' });
  });

  it('is null-safe', () => {
    expect(assess(null).state).toBe(Health.QUIET);
    expect(() => noteFetch(null, {})).not.toThrow();
    expect(() => noteSse(null, {})).not.toThrow();
  });
});

describe('formatAge', () => {
  it.each([[250, '250ms'], [1500, '1.5s'], [65000, '1m 5s']])('%i -> %s', (ms, out) => {
    expect(formatAge(ms)).toBe(out);
  });

  it('handles nonsense', () => {
    expect(formatAge(-1)).toBe('—');
    expect(formatAge(NaN)).toBe('—');
  });
});

describe('duplicate store detection', () => {
  it('sees nothing wrong with one instance per module', () => {
    const scope = {};
    registerModule(scope, 'history', 'a');
    registerModule(scope, 'panes', 'b');
    expect(duplicatedModules(scope)).toEqual([]);
  });

  it('catches a module registered twice — the hot-reload wedge', () => {
    // The registry lives on globalThis, which HMR does not replace, so a
    // second id for one module means two live copies of that store.
    const scope = {};
    registerModule(scope, 'history', 'first');
    registerModule(scope, 'history', 'second');
    expect(duplicatedModules(scope)).toEqual([{ name: 'history', instances: 2 }]);
  });

  it('is idempotent for the same instance re-registering', () => {
    const scope = {};
    registerModule(scope, 'history', 'a');
    registerModule(scope, 'history', 'a');
    expect(duplicatedModules(scope)).toEqual([]);
  });

  it('reports every duplicated module', () => {
    const scope = {};
    for (const [m, i] of [['history', 1], ['history', 2], ['panes', 1], ['panes', 2], ['input', 1]]) {
      registerModule(scope, m, `${m}-${i}`);
    }
    expect(duplicatedModules(scope).map(d => d.name).sort()).toEqual(['history', 'panes']);
  });

  it('survives a scope with no registry', () => {
    expect(duplicatedModules(undefined)).toEqual([]);
    expect(duplicatedModules({})).toEqual([]);
  });
});
