// Is this client still talking to the server?
//
// A wedged window looks exactly like an idle one: the transcript sits there,
// the composer accepts text, nothing errors. On 2026-09-01 a window stopped
// making any request at all for five minutes while messages were typed into
// it, and there was no way to tell from the screen. The only reliable signal
// is time since the last thing the server actually answered.

export const Health = {
  OK: 'ok',
  QUIET: 'quiet',   // nothing recently, but nothing is expected either
  STALE: 'stale',   // should have heard something by now
  WEDGED: 'wedged', // long past due; the client is not talking to anyone
};

export function createHealth(now = Date.now()) {
  return {
    startedAt: now,
    lastFetchAt: 0,
    lastFetchPath: '',
    lastSseAt: 0,
    lastSseType: '',
    fetches: 0,
    fetchErrors: 0,
    sseEvents: 0,
  };
}

export function noteFetch(health, { path = '', ok = true, at = Date.now() } = {}) {
  if (!health) return health;
  health.fetches += 1;
  if (!ok) health.fetchErrors += 1;
  if (ok) {
    health.lastFetchAt = at;
    health.lastFetchPath = path;
  }
  return health;
}

export function noteSse(health, { type = '', at = Date.now() } = {}) {
  if (!health) return health;
  health.sseEvents += 1;
  health.lastSseAt = at;
  health.lastSseType = type;
  return health;
}

// The transcript is polled and SSE is kept open, so silence on both for long
// enough is not quiet — it is broken.
export function assess(health, { now = Date.now(), staleMs = 45000, wedgedMs = 120000 } = {}) {
  if (!health) return { state: Health.QUIET, sinceMs: 0, reason: 'no health record' };

  const last = Math.max(health.lastFetchAt, health.lastSseAt);
  if (!last) {
    // Nothing has ever come back. Before that is damning, allow for boot.
    const age = now - health.startedAt;
    if (age >= wedgedMs) return { state: Health.WEDGED, sinceMs: age, reason: 'never reached the server' };
    if (age >= staleMs) return { state: Health.STALE, sinceMs: age, reason: 'no response since load' };
    return { state: Health.QUIET, sinceMs: age, reason: 'starting up' };
  }

  const sinceMs = now - last;
  if (sinceMs >= wedgedMs) return { state: Health.WEDGED, sinceMs, reason: 'no server contact' };
  if (sinceMs >= staleMs) return { state: Health.STALE, sinceMs, reason: 'no server contact' };
  return { state: Health.OK, sinceMs, reason: '' };
}

export function formatAge(ms) {
  if (!Number.isFinite(ms) || ms < 0) return '—';
  if (ms < 1000) return `${Math.round(ms)}ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`;
  const m = Math.floor(ms / 60000);
  return `${m}m ${Math.round((ms % 60000) / 1000)}s`;
}

// ---- module identity ----------------------------------------------------
//
// A hot reload replaces a store module while the DOM stays bound to the old
// copy, so updates land in one instance and the screen reads the other. That
// is what wedged the window: typed messages went into a store nothing
// rendered. The registry lives on globalThis, which HMR does not replace, so a
// second registration for the same module is proof it happened.

export function registerModule(scope, name, id) {
  const host = scope || {};
  const registry = host.__clarpModules || (host.__clarpModules = {});
  const seen = registry[name] || (registry[name] = []);
  if (!seen.includes(id)) seen.push(id);
  return registry;
}

export function duplicatedModules(scope) {
  const registry = (scope && scope.__clarpModules) || {};
  return Object.entries(registry)
    .filter(([, ids]) => ids.length > 1)
    .map(([name, ids]) => ({ name, instances: ids.length }));
}
