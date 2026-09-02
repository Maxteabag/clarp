// Per-device view preferences.

export const prefs = $state({
  hideTools: localStorage.getItem('historyHideTools') !== '0',
});

export function toggleTools() {
  prefs.hideTools = !prefs.hideTools;
  try { localStorage.setItem('historyHideTools', prefs.hideTools ? '1' : '0'); } catch (_) {}
}
