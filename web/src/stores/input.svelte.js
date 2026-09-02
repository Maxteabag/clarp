// Keyboard input: the context snapshot, and the one handler that dispatches.
//
// The whole keyboard surface is this file plus @core/keymap.js. There is a
// single window listener and a single source of truth for which bindings are
// live, so a key cannot be claimed twice or fire against stale state.

import { registerModule } from '@core/client-health.js';
import { instanceId } from '../lib/net.js';
import { normalizeKey, resolveAction } from '@core/keymap.js';
import { getLeafPanes } from '@core/pane-tree.js';
import { app, isDesktop, setSession } from './app.svelte.js';
import { reload } from './conversations.svelte.js';
import { toggleTools } from './prefs.svelte.js';
import { composerRef } from './composer.svelte.js';
import { orderChats } from '../lib/chat-order.js';
import {
  panesState, splitActivePane, closeActivePane, focusActivePane,
  navigateActivePane, toggleZoomActive, equalizePaneSplits, resizeActiveSplit,
  retargetPane,
} from './panes.svelte.js';

registerModule(globalThis, 'input', instanceId('input'));

export const input = $state({
  // A text field owns the keyboard. Set from the field's own focus/blur, never
  // inferred, so it cannot disagree with where the caret actually is.
  insert: false,
  // Which dialog is open, '' for none. A dialog takes the keyboard outright.
  overlay: '',
  // The pane under the pointer. Pane actions prefer it over the focused pane,
  // so the keys act on what you are looking at.
  hoveredPaneId: '',
  // Which region has the keyboard: the pane workspace or the chat rail.
  region: 'panes',
  // Selected row in the rail, an index into the order the rail renders.
  chatIndex: 0,
});

// The rail's own order, from the same function the rail renders with, so a
// number key and a selection always mean the row you are looking at. Indexing
// app.availableSessions instead would drift the moment the rail re-sorted.
export function orderedSessions() {
  return orderChats(app.agentsBySession, app.status, app.availableSessions)
    .map(row => row.sid);
}

export function setRegion(region) {
  input.region = region === 'sidebar' ? 'sidebar' : 'panes';
}

// Entering the rail starts on the chat that is already open, so the first
// j or k moves from where you are rather than from the top.
function enterSidebar() {
  const sids = orderedSessions();
  const at = sids.indexOf(app.session);
  input.chatIndex = at >= 0 ? at : 0;
  input.region = 'sidebar';
}

function moveChat(delta) {
  const sids = orderedSessions();
  if (!sids.length) return;
  input.chatIndex = (input.chatIndex + delta + sids.length) % sids.length;
}

// Dialogs register their open state here rather than each installing a window
// listener of its own — competing listeners were part of the old flakiness.
export function setOverlay(name, open) {
  if (open) input.overlay = name;
  else if (input.overlay === name) input.overlay = '';
}

export function setInsert(on) {
  input.insert = !!on;
}

export function setHoveredPane(paneId) {
  input.hoveredPaneId = paneId || '';
}

// The pane a pane-scoped action applies to: what the pointer is over, else
// what has focus.
export function targetPaneId() {
  const leaves = getLeafPanes(panesState.tree.root);
  if (input.hoveredPaneId && leaves.some(l => l.id === input.hoveredPaneId)) {
    return input.hoveredPaneId;
  }
  return panesState.tree.activeId;
}

export function keyContext() {
  return {
    desktop: isDesktop,
    insert: input.insert,
    overlay: input.overlay,
    region: input.region,
    paneCount: getLeafPanes(panesState.tree.root).length,
    zoomed: !!panesState.tree.zoomedPaneId,
    sessionCount: (app.availableSessions || []).length,
    hovering: !!input.hoveredPaneId,
  };
}

// Actions the shell owns (dialogs, search) are injected once at startup; the
// rest are wired straight to the stores.
let shell = {};

export function initKeyboard(handlers = {}) {
  shell = handlers;
}

const ACTIONS = {
  'focus-composer': () => composerRef.focus(),
  'leave-insert': () => {
    setInsert(false);
    document.activeElement?.blur?.();
  },
  'blur': () => document.activeElement?.blur?.(),
  'close-overlay': () => shell.onCloseOverlay?.(input.overlay),

  'split-vertical': () => splitOn('vertical'),
  'split-horizontal': () => splitOn('horizontal'),
  'close-pane': () => closeActivePane(targetPaneId()),
  'toggle-zoom': () => { focusTarget(); toggleZoomActive(); },
  'equalize': () => equalizePaneSplits(),
  'shrink': () => { focusTarget(); resizeActiveSplit(-0.05); },
  'grow': () => { focusTarget(); resizeActiveSplit(0.05); },

  'nav-left': () => navigateActivePane('left'),
  'nav-right': () => navigateActivePane('right'),
  'nav-up': () => navigateActivePane('up'),
  'nav-down': () => navigateActivePane('down'),

  'quick-switch': () => shell.onQuickSwitch?.(),
  'focus-sidebar': () => enterSidebar(),
  'leave-sidebar': () => setRegion('panes'),
  'chat-next': () => moveChat(1),
  'chat-prev': () => moveChat(-1),
  'chat-open': () => {
    const sid = orderedSessions()[input.chatIndex];
    if (sid) setSession(sid);
    setRegion('panes');
  },

  'toggle-tools': () => toggleTools(),
  'refresh': () => reload(app.session),
  'search': () => shell.onSearch?.(),
  'overview': () => shell.onOverview?.(),
  'help': () => shell.onHelp?.(),
};

for (let n = 1; n <= 9; n++) {
  ACTIONS[`jump-agent-${n}`] = () => {
    const target = orderedSessions()[n - 1];
    if (target) setSession(target);
  };
}

// A pane action on a hovered pane focuses it first, so the layout change and
// the highlight agree afterwards.
function focusTarget() {
  const id = targetPaneId();
  if (id !== panesState.tree.activeId) focusActivePane(id);
}

function splitOn(direction) {
  focusTarget();
  const used = new Set(getLeafPanes(panesState.tree.root).map(l => l.session));
  const next = (app.availableSessions || []).find(s => !used.has(s)) || app.session;
  splitActivePane(direction, next);
}

// What the quick switcher decided, applied. Kept here rather than in the
// component so the palette only has to say what it wants.
export function applyQuickChoice(decision, onCreate) {
  if (!decision) return;
  if (decision.action === 'create') { onCreate?.(decision.name); return; }
  if (decision.action === 'focus') { focusActivePane(decision.paneId); return; }
  retargetPane(decision.paneId, decision.session);
}

export function handleGlobalKey(e) {
  // The browser's own keys are never ours.
  if (e.key === 'F5' || e.key === 'F12') return false;
  if ((e.ctrlKey || e.metaKey) && (e.key === 'r' || e.key === 'R')) return false;
  if (e.isComposing) return false;

  const hit = resolveAction(normalizeKey(e), keyContext());
  if (!hit) return false;

  const run = ACTIONS[hit.action];
  if (!run) return false;

  e.preventDefault();
  run();
  return true;
}
