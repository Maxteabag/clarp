// The Host's backend catalogue (/agent-model-options), cached on the device and
// shared by every surface that names a backend: labels, details, and the
// supports_* flags. Components never hardcode a provider id; a CLI the Host
// adds tomorrow shows up here with its own label.

import { backendLabel, backendDetail } from '@core/agent-launch.js';

// The last catalogue the Host sent, kept on the device (localStorage is per
// origin, so per Host): pickers list its harnesses and models at once, and a
// forced load refreshes them quietly.
const CACHE_KEY = 'agentModelOptions';

function cached() {
  try {
    const d = JSON.parse(localStorage.getItem(CACHE_KEY) || 'null');
    return d && d.providers ? d : null;
  } catch (_) { return null; }
}

export const backends = $state({
  catalogue: cached(),
  // Whether this page has the Host's own answer yet, not only the cached one.
  fresh: false,
  loading: false,
});

let inflight = null;

export async function loadBackendCatalogue({ force = false } = {}) {
  if (backends.catalogue && backends.fresh && !force) return backends.catalogue;
  if (inflight) return inflight;
  backends.loading = true;
  inflight = fetch('/agent-model-options')
    .then(r => (r.ok ? r.json() : null))
    .then(d => {
      if (!d || !d.providers) return backends.catalogue;
      backends.fresh = true;
      const text = JSON.stringify(d);
      // An unchanged answer leaves the shown catalogue (and what was picked
      // from it) alone.
      if (backends.catalogue && text === JSON.stringify(backends.catalogue)) return backends.catalogue;
      try { localStorage.setItem(CACHE_KEY, text); } catch (_) { /* full or blocked */ }
      backends.catalogue = d;
      return backends.catalogue;
    })
    .catch(() => backends.catalogue)
    .finally(() => {
      backends.loading = false;
      inflight = null;
    });
  return inflight;
}

export function labelFor(backend) {
  return backendLabel(backend, backends.catalogue);
}

export function detailFor(backend) {
  return backendDetail(backend, backends.catalogue);
}
