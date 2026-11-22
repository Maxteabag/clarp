// Per-device view preferences.

import { TOOL_ACTIVITY } from '@core/history-fold.js';

// Tool activity (static/lib/history-fold.js): Grouped by default. The old show/hide toggle
// ('historyHideTools' 0: tools shown) carries over as Always visible.
function savedToolActivity() {
  const saved = localStorage.getItem('chatToolActivity');
  if (TOOL_ACTIVITY.includes(saved)) return saved;
  return localStorage.getItem('historyHideTools') === '0' ? 'always' : 'grouped';
}

export const prefs = $state({
  toolActivity: savedToolActivity(),
  // The quiet mode the header's tools button goes back to.
  lastQuietMode: 'grouped',
  // 'Show the Host on each chat': on unless turned off. Kept on this device, never on the Host.
  showHostBadge: localStorage.getItem('chatShowHostBadge') !== '0',
  // 'Show a line where agent messages were' ('3 messages between agents' with its Show): on unless
  // turned off. Off draws no such line; the chat's own setting (⇄) still shows them.
  showCoordinationLine: localStorage.getItem('chatShowCoordinationLine') !== '0',
});

export function setShowCoordinationLine(on) {
  prefs.showCoordinationLine = !!on;
  try { localStorage.setItem('chatShowCoordinationLine', on ? '1' : '0'); } catch (_) {}
}

export function setShowHostBadge(on) {
  prefs.showHostBadge = !!on;
  try { localStorage.setItem('chatShowHostBadge', on ? '1' : '0'); } catch (_) {}
}

export function setToolActivity(mode) {
  if (!TOOL_ACTIVITY.includes(mode)) return;
  if (mode !== 'always') prefs.lastQuietMode = mode;
  prefs.toolActivity = mode;
  try { localStorage.setItem('chatToolActivity', mode); } catch (_) {}
}

/** The header's tools button: every tool call in view, or back to the quiet mode chosen. */
export function toggleTools() {
  setToolActivity(prefs.toolActivity === 'always' ? (prefs.lastQuietMode || 'grouped') : 'always');
}
