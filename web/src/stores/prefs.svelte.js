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
});

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
