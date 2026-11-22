// One chat's own choices, as its '…' menu and its profile show them (#653):
//   Messages between agents: Follow Settings / Show / Hide, kept by the Host
//     (POST /agent-coordination; the snapshot row's coordination_override)
//   Tool activity: Follow Settings / Grouped / Always visible / Group old / Hide when done,
//     kept on this device, keyed by the Host's server id and the session

import { chatKey, chatToolChoice, effectiveToolActivity, FOLLOW } from '@core/history-fold.js';
import { app, statusFor } from './app.svelte.js';
import { setChatCoordination } from './coordination.svelte.js';
import { prefs, setChatToolActivityByKey, setToolActivity } from './prefs.svelte.js';

function keyOf(session) {
  return chatKey(app.hosts[0]?.id || '', session);
}

/** The Tool activity this chat folds by. */
export function toolActivityFor(session) {
  return effectiveToolActivity(prefs.toolActivity, prefs.chatToolActivity, keyOf(session));
}

/** This chat's own Tool activity, or 'follow'. */
export function chatToolActivity(session) {
  return chatToolChoice(prefs.chatToolActivity, keyOf(session));
}

/** Whether this chat's choices can be kept yet (the Host's server id is known). */
export function chatChoicesReady(session) {
  return !!keyOf(session);
}

/** Each chat's quiet mode before it was set to Always visible (key -> mode), for the toggle back. */
const lastQuiet = new Map();

export function setChatToolActivity(session, mode) {
  const key = keyOf(session);
  const before = chatToolChoice(prefs.chatToolActivity, key);
  if (key && mode === 'always' && before !== FOLLOW && before !== 'always') lastQuiet.set(key, before);
  setChatToolActivityByKey(key, mode);
}

/** This chat's own 'Messages between agents': 'default' (Follow Settings), 'show' or 'hide'. */
export function chatCoordinationChoice(session) {
  const own = statusFor(session).coordination_override;
  return own === 'show' || own === 'hide' ? own : 'default';
}

export function setChatCoordinationChoice(session, choice) {
  if (choice === chatCoordinationChoice(session)) return Promise.resolve(null);
  return setChatCoordination(session, choice);
}

/**
 * The header's tools button: every tool call in view, or back to the quiet mode. It changes the
 * chat's own choice when the chat has one (back to that chat's own quiet mode), else the
 * device's setting, as before.
 */
export function toggleToolsFor(session) {
  const own = chatToolActivity(session);
  if (own === FOLLOW) {
    setToolActivity(prefs.toolActivity === 'always' ? (prefs.lastQuietMode || 'grouped') : 'always');
    return;
  }
  const quiet = lastQuiet.get(keyOf(session)) || prefs.lastQuietMode || 'grouped';
  setChatToolActivity(session, own === 'always' ? quiet : 'always');
}
