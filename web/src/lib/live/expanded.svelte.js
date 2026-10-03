// Expand/collapse state of live rows, keyed by item (or group/turn) id, so a
// row keeps it while it updates in place and after the pane re-renders.

import { SvelteMap } from 'svelte/reactivity';

const open = new SvelteMap();

export function isOpen(id, fallback = false) {
  return open.has(id) ? open.get(id) : fallback;
}

export function toggle(id, fallback = false) {
  open.set(id, !isOpen(id, fallback));
}
