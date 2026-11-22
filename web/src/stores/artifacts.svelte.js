// The native forms (render_spec artifacts, issue 428) of each open chat, drawn in its
// transcript. Rows come from GET /artifacts?session=&type=render_spec and are read again when
// an artifact-updated event names the chat; each card loads its own form when drawn.

import { clog } from '../lib/net.js';

const LIMIT = 50;

/** session -> { rows, loaded } */
export const artifactState = $state({ bySession: {} });

const inflight = new Map();
const again = new Set();

/** The render_spec rows of a chat (newest 50), loading them the first time. */
export function renderSpecsFor(session) {
  if (!session) return [];
  const entry = artifactState.bySession[session];
  if (!entry) { queueMicrotask(() => loadRenderSpecs(session)); return []; }
  return entry.rows;
}

export async function loadRenderSpecs(session) {
  if (!session) return;
  if (inflight.has(session)) { again.add(session); return inflight.get(session); }
  if (!artifactState.bySession[session]) artifactState.bySession[session] = { rows: [], loaded: false };
  const run = (async () => {
    try {
      const q = new URLSearchParams({ session, type: 'render_spec', limit: String(LIMIT), order: 'created' });
      const r = await fetch('/artifacts?' + q);
      if (!r.ok) { clog('renderSpecList', `status=${r.status}`); return; }
      const body = await r.json();
      const rows = (Array.isArray(body && body.artifacts) ? body.artifacts : [])
        .filter(row => row && row.type === 'render_spec' && row.session === session && !row.deleted_at);
      artifactState.bySession[session] = { rows, loaded: true };
    } catch (e) {
      clog('renderSpecList', 'failed');
    } finally {
      inflight.delete(session);
      if (again.delete(session)) loadRenderSpecs(session);
    }
  })();
  inflight.set(session, run);
  return run;
}

/** An artifact-updated event: read the chat's list again when it is one we draw. */
export function noteArtifactUpdated(ev) {
  const session = ev && ev.session;
  if (session && artifactState.bySession[session]) loadRenderSpecs(session);
}

/** After a reconnect every list may be stale. */
export function refreshRenderSpecs() {
  for (const session of Object.keys(artifactState.bySession)) loadRenderSpecs(session);
}
