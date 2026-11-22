// Per-device view preferences.

export const prefs = $state({
  hideTools: localStorage.getItem('historyHideTools') !== '0',
  // 'Show the Host on each chat': on unless turned off. Kept on this device, never on the Host.
  showHostBadge: localStorage.getItem('chatShowHostBadge') !== '0',
});

export function setShowHostBadge(on) {
  prefs.showHostBadge = !!on;
  try { localStorage.setItem('chatShowHostBadge', on ? '1' : '0'); } catch (_) {}
}

export function toggleTools() {
  prefs.hideTools = !prefs.hideTools;
  try { localStorage.setItem('historyHideTools', prefs.hideTools ? '1' : '0'); } catch (_) {}
}
