import { registerModule } from '@core/client-health.js';
import { instanceId } from '../lib/net.js';
import {
  createPaneTree,
  splitPane,
  closePane,
  focusPane,
  navigatePanes,
  toggleZoom,
  resizeSplit,
  equalizeSplits,
  getLeafPanes,
  findPane,
  setPaneSession,
} from '@core/pane-tree.js';
import { app, setSession } from './app.svelte.js';

// Layout only. The keyboard lives in input.svelte.js, which calls into these;
// keeping the tree free of key handling is what stops mode and focus drifting
// apart.
registerModule(globalThis, 'panes', instanceId('panes'));

export const panesState = $state({
  tree: createPaneTree(app.session || ''),
});

// The open chat can change from anywhere (sidebar, quick switch, a focus
// broadcast, hands-free routing). Whatever changed it, the active pane shows
// it, so app.session and the active leaf never disagree.
$effect.root(() => {
  $effect(() => {
    const session = app.session;
    const active = findPane(panesState.tree.root, panesState.tree.activeId);
    if (session && active && active.session !== session) {
      panesState.tree = setPaneSession(panesState.tree, panesState.tree.activeId, session);
    }
  });
});

export function splitActivePane(direction = 'vertical', newSession = app.session) {
  panesState.tree = splitPane(panesState.tree, panesState.tree.activeId, direction, newSession);
  const activeLeaf = findPane(panesState.tree.root, panesState.tree.activeId);
  if (activeLeaf && activeLeaf.session) {
    setSession(activeLeaf.session);
  }
}

export function closeActivePane(targetId = panesState.tree.activeId) {
  panesState.tree = closePane(panesState.tree, targetId);
  const activeLeaf = findPane(panesState.tree.root, panesState.tree.activeId);
  if (activeLeaf && activeLeaf.session) {
    setSession(activeLeaf.session);
  }
}

export function focusActivePane(targetId) {
  panesState.tree = focusPane(panesState.tree, targetId);
  const activeLeaf = findPane(panesState.tree.root, panesState.tree.activeId);
  if (activeLeaf && activeLeaf.session) {
    setSession(activeLeaf.session);
  }
}

export function navigateActivePane(direction) {
  panesState.tree = navigatePanes(panesState.tree, direction);
  const activeLeaf = findPane(panesState.tree.root, panesState.tree.activeId);
  if (activeLeaf && activeLeaf.session) {
    setSession(activeLeaf.session);
  }
}

// Point an existing pane at a different conversation, and open it.
export function retargetPane(paneId, session) {
  const target = paneId || panesState.tree.activeId;
  panesState.tree = focusPane(setPaneSession(panesState.tree, target, session), target);
  setSession(session);
}

export function toggleZoomActive() {
  panesState.tree = toggleZoom(panesState.tree);
}

export function resizeActiveSplit(delta) {
  panesState.tree = resizeSplit(panesState.tree, panesState.tree.activeId, delta);
}

export function equalizePaneSplits() {
  panesState.tree = equalizeSplits(panesState.tree);
}

export function getActiveLeafPanes() {
  return getLeafPanes(panesState.tree.root);
}
