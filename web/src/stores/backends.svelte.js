// The Host's backend catalogue (/agent-model-options), loaded once and
// shared by every surface that names a backend: labels, details, and the
// supports_* flags. Components never hardcode a provider id; a CLI the Host
// adds tomorrow shows up here with its own label.

import { backendLabel, backendDetail } from '@core/agent-launch.js';

export const backends = $state({
  catalogue: null,
  loading: false,
});

let inflight = null;

export async function loadBackendCatalogue({ force = false } = {}) {
  if (backends.catalogue && !force) return backends.catalogue;
  if (inflight) return inflight;
  backends.loading = true;
  inflight = fetch('/agent-model-options')
    .then(r => (r.ok ? r.json() : null))
    .then(d => {
      if (d && d.providers) backends.catalogue = d;
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
