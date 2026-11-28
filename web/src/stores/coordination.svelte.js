// 'Show messages between agents' (Host feature hidden_coordination, docs/protocol.md): the
// Host's setting (GET/POST /coordination-settings) and one chat's own (POST /agent-coordination).
// Both are the Host's, not this device's: a change reaches every device as an agent-roster
// coordination-changed event, and each open chat read under the old value reloads.

import { SSEType } from '@core/protocol.js';
import { clog } from '../lib/net.js';
import { handleSseEvent } from './conversations.svelte.js';
import { live } from './live.svelte.js';

export const coordination = $state({
  /** The Host's choice: 'show' | 'hide' (the Host's default is hide). */
  visibility: 'hide',
  loaded: false,
  error: '',
});

function changed(session, visibility) {
  // The same step the Host's agent-roster event takes, without waiting for it.
  handleSseEvent({ type: SSEType.AGENT_ROSTER, kind: 'coordination-changed',
                   ...(session ? { session } : {}), coordination_visibility: visibility });
}

export async function loadCoordinationSettings() {
  if (!live.hiddenCoordination) return;
  try {
    const r = await fetch('/coordination-settings');
    if (!r.ok) return;
    const d = await r.json();
    if (d && d.coordination_visibility) coordination.visibility = String(d.coordination_visibility);
    coordination.loaded = true;
  } catch (_) {}
}

/** The Host's setting. The switch moves at once and moves back on failure. */
export async function setCoordinationVisibility(visibility) {
  const before = coordination.visibility;
  coordination.visibility = visibility;
  coordination.error = '';
  try {
    const r = await fetch('/coordination-settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ coordination_visibility: visibility }),
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const d = await r.json();
    coordination.visibility = String(d.coordination_visibility || visibility);
    changed('', coordination.visibility);
  } catch (err) {
    coordination.visibility = before;
    coordination.error = 'chat_settings.not_saved';  // a catalog key; the dialog shows t()
    clog('coordinationSettingFail', err && err.message ? err.message : String(err));
  }
}

/** One chat's own choice: 'show', 'hide' or 'default' (follow the Host). Answers the effective value. */
export async function setChatCoordination(session, visibility) {
  if (!session) return null;
  try {
    const r = await fetch('/agent-coordination', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session, visibility }),
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const d = await r.json();
    const effective = String(d.coordination_visibility || '');
    if (effective) changed(session, effective);
    return { visibility: effective, override: d.coordination_override || null };
  } catch (err) {
    clog('chatCoordinationFail', `${session} ${err && err.message ? err.message : String(err)}`);
    return null;
  }
}
