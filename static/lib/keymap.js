// Hierarchical keymap: one table of bindings, one context stack, one resolver.
//
// Modelled on sqlit's core/keymap + binding_contexts + key_router split. The
// old modal manager was a single global NORMAL/INSERT switch with a `switch`
// statement, which is why shortcuts felt random: a dialog could be open and
// pane keys still fired, the mode could say INSERT while focus sat elsewhere,
// and nothing scoped a key to the pane you were actually looking at.
//
// Here a key resolves against an ordered stack of contexts, most specific
// first. A binding is eligible when its context is on the stack and its guard
// passes. Nothing else can fire. The same table drives the shortcut bar, so
// what the footer shows and what the keyboard does cannot drift apart.

// ---- key normalization ---------------------------------------------------

const NAMED = {
  Escape: 'esc',
  Enter: 'enter',
  Tab: 'tab',
  Backspace: 'backspace',
  Delete: 'del',
  ArrowLeft: 'left',
  ArrowRight: 'right',
  ArrowUp: 'up',
  ArrowDown: 'down',
  PageUp: 'pgup',
  PageDown: 'pgdn',
  Home: 'home',
  End: 'end',
  ' ': 'space',
};

// A printable character carries its own shift state ('H' is already shifted),
// so prefixing shift+ there would need every binding written twice.
export function normalizeKey(e) {
  if (!e || typeof e.key !== 'string') return '';
  const named = NAMED[e.key];
  const base = named || (e.key.length === 1 ? e.key : e.key.toLowerCase());
  let out = base;
  if (e.altKey) out = `alt+${out}`;
  if (e.shiftKey && named) out = `shift+${out}`;
  if (e.ctrlKey) out = `ctrl+${out}`;
  if (e.metaKey) out = `meta+${out}`;
  return out;
}

const KEY_DISPLAY = {
  esc: 'Esc', enter: '↵', space: '␣', tab: '⇥', del: 'Del',
  left: '←', right: '→', up: '↑', down: '↓',
  pgup: 'PgUp', pgdn: 'PgDn',
};

export function formatKey(key) {
  if (KEY_DISPLAY[key]) return KEY_DISPLAY[key];
  return key
    .replace(/^ctrl\+/, '^')
    .replace(/^meta\+/, '⌘')
    .replace(/^alt\+/, '⌥')
    .replace(/^shift\+/, '⇧');
}

// ---- context stack -------------------------------------------------------

// Returned most specific first; the resolver takes the first eligible match,
// so a pane binding always beats a global one on the same key.
export function resolveContexts(ctx = {}) {
  // A dialog owns the keyboard outright. Letting global keys through while a
  // dialog was open was the single biggest source of "it did something else".
  if (ctx.overlay) return [`overlay.${ctx.overlay}`, 'overlay'];

  const stack = [];
  if (ctx.insert) stack.push('insert');
  // The rail and the panes are two focus regions, and only one has the
  // keyboard: in the rail, j/k walk chats instead of moving between panes.
  if (ctx.desktop && !ctx.insert && ctx.region === 'sidebar') stack.push('sidebar');
  if (ctx.desktop && !ctx.insert && ctx.region !== 'sidebar') {
    stack.push('pane');
    if (ctx.paneCount > 1) stack.push('pane.multi');
    if (ctx.zoomed) stack.push('pane.zoomed');
  }
  stack.push('global');
  // Most specific first: pane.multi beats pane beats global. Ties keep the
  // push order, captured before sorting because sort mutates the array.
  const order = new Map(stack.map((c, i) => [c, i]));
  return stack.sort((a, b) => b.split('.').length - a.split('.').length
    || order.get(a) - order.get(b));
}

// ---- guards --------------------------------------------------------------

export const GUARDS = {
  hasPanes: ctx => (ctx.paneCount || 0) > 0,
  hasMultiplePanes: ctx => (ctx.paneCount || 0) > 1,
  isDesktop: ctx => !!ctx.desktop,
  hasMultipleDesktopPanes: ctx => !!ctx.desktop && (ctx.paneCount || 0) > 1,
  hasSessions: ctx => (ctx.sessionCount || 0) > 0,
  isZoomed: ctx => !!ctx.zoomed,
};

// ---- the table -----------------------------------------------------------

// `group` orders the shortcut bar. `hidden` keeps aliases out of it.
// `allowInInsert` is the only way a binding fires while a text field has
// focus, so typing can never trigger a command by accident.
export const KEYMAP = [
  // insert
  { key: 'esc', action: 'leave-insert', context: 'insert', label: 'Normal', group: 'mode', allowInInsert: true },
  { key: 'enter', action: 'send', context: 'insert', label: 'Send', group: 'mode', allowInInsert: true, passive: true },

  // overlay
  { key: 'esc', action: 'close-overlay', context: 'overlay', label: 'Close', group: 'mode', allowInInsert: true },

  // entering insert
  { key: 'i', action: 'focus-composer', context: 'global', label: 'Type', group: 'mode' },
  { key: 'a', action: 'focus-composer', context: 'global', hidden: true },
  { key: 'enter', action: 'focus-composer', context: 'global', hidden: true },

  // Universal workspace chords. Option/Alt alone remains convenient in pane
  // mode, but is never claimed from a text field (Option+Arrow is word-wise
  // caret movement on macOS). Ctrl+Alt is the explicit composer-safe chord.
  { key: 'alt+left', action: 'nav-left', context: 'global', label: 'Move pane', displayKey: '⌥←↓↑→', group: 'nav', when: 'hasMultipleDesktopPanes' },
  { key: 'alt+down', action: 'nav-down', context: 'global', hidden: true, when: 'hasMultipleDesktopPanes' },
  { key: 'alt+up', action: 'nav-up', context: 'global', hidden: true, when: 'hasMultipleDesktopPanes' },
  { key: 'alt+right', action: 'nav-right', context: 'global', hidden: true, when: 'hasMultipleDesktopPanes' },
  { key: 'ctrl+alt+left', action: 'nav-left', context: 'global', label: 'Move pane', displayKey: '^⌥←↓↑→', group: 'nav', when: 'hasMultipleDesktopPanes', allowInInsert: true },
  { key: 'ctrl+alt+down', action: 'nav-down', context: 'global', hidden: true, when: 'hasMultipleDesktopPanes', allowInInsert: true },
  { key: 'ctrl+alt+up', action: 'nav-up', context: 'global', hidden: true, when: 'hasMultipleDesktopPanes', allowInInsert: true },
  { key: 'ctrl+alt+right', action: 'nav-right', context: 'global', hidden: true, when: 'hasMultipleDesktopPanes', allowInInsert: true },
  { key: 'alt+v', action: 'split-vertical', context: 'global', label: 'Split right', group: 'layout', hidden: true, when: 'isDesktop' },
  { key: 'alt+s', action: 'split-horizontal', context: 'global', label: 'Split down', group: 'layout', hidden: true, when: 'isDesktop' },
  { key: 'alt+x', action: 'close-pane', context: 'global', label: 'Close pane', group: 'layout', hidden: true, when: 'hasMultipleDesktopPanes' },
  { key: 'alt+z', action: 'toggle-zoom', context: 'global', label: 'Zoom pane', group: 'layout', when: 'hasMultipleDesktopPanes' },
  { key: 'alt+=', action: 'equalize', context: 'global', label: 'Balance panes', group: 'layout', hidden: true, when: 'hasMultipleDesktopPanes' },
  { key: 'ctrl+alt+v', action: 'split-vertical', context: 'global', label: 'Split right', group: 'layout', hidden: true, when: 'isDesktop', allowInInsert: true },
  { key: 'ctrl+alt+s', action: 'split-horizontal', context: 'global', label: 'Split down', group: 'layout', hidden: true, when: 'isDesktop', allowInInsert: true },
  { key: 'ctrl+alt+x', action: 'close-pane', context: 'global', label: 'Close pane', group: 'layout', hidden: true, when: 'hasMultipleDesktopPanes', allowInInsert: true },
  { key: 'ctrl+alt+z', action: 'toggle-zoom', context: 'global', label: 'Zoom pane', displayKey: '^⌥z', group: 'layout', when: 'hasMultipleDesktopPanes', allowInInsert: true },
  { key: 'ctrl+alt+=', action: 'equalize', context: 'global', label: 'Balance panes', group: 'layout', hidden: true, when: 'hasMultipleDesktopPanes', allowInInsert: true },

  // pane layout
  { key: 'v', action: 'split-vertical', context: 'pane', label: 'Split right', group: 'layout' },
  { key: '|', action: 'split-vertical', context: 'pane', hidden: true },
  { key: 's', action: 'split-horizontal', context: 'pane', label: 'Split down', group: 'layout' },
  { key: '-', action: 'split-horizontal', context: 'pane', hidden: true },
  { key: 'x', action: 'close-pane', context: 'pane.multi', label: 'Close pane', group: 'layout', when: 'hasMultiplePanes' },
  { key: 'q', action: 'close-pane', context: 'pane.multi', hidden: true, when: 'hasMultiplePanes' },
  { key: 'z', action: 'toggle-zoom', context: 'pane.multi', label: 'Zoom pane', group: 'layout', when: 'hasMultiplePanes' },
  { key: '=', action: 'equalize', context: 'pane.multi', label: 'Balance panes', group: 'layout', when: 'hasMultiplePanes' },

  // pane navigation
  { key: 'h', action: 'nav-left', context: 'pane.multi', label: 'Move', displayKey: 'hjkl / ←↓↑→', group: 'nav', when: 'hasMultiplePanes' },
  { key: 'j', action: 'nav-down', context: 'pane.multi', hidden: true, when: 'hasMultiplePanes' },
  { key: 'k', action: 'nav-up', context: 'pane.multi', hidden: true, when: 'hasMultiplePanes' },
  { key: 'l', action: 'nav-right', context: 'pane.multi', hidden: true, when: 'hasMultiplePanes' },
  { key: 'left', action: 'nav-left', context: 'pane.multi', hidden: true, when: 'hasMultiplePanes' },
  { key: 'down', action: 'nav-down', context: 'pane.multi', hidden: true, when: 'hasMultiplePanes' },
  { key: 'up', action: 'nav-up', context: 'pane.multi', hidden: true, when: 'hasMultiplePanes' },
  { key: 'right', action: 'nav-right', context: 'pane.multi', hidden: true, when: 'hasMultiplePanes' },
  { key: 'H', action: 'shrink', context: 'pane.multi', label: 'Shrink pane', group: 'nav', when: 'hasMultiplePanes' },
  { key: 'L', action: 'grow', context: 'pane.multi', label: 'Grow pane', hidden: true, group: 'nav', when: 'hasMultiplePanes' },

  // quick switch
  { key: 'space', action: 'quick-switch', context: 'global', label: 'Go to', group: 'nav' },
  { key: 'ctrl+k', action: 'commands', context: 'global', label: 'Commands', group: 'mode', when: 'isDesktop', allowInInsert: true },
  { key: 'meta+k', action: 'commands', context: 'global', hidden: true, when: 'isDesktop', allowInInsert: true },

  // chat rail
  { key: 'c', action: 'focus-sidebar', context: 'pane', label: 'Chats', group: 'nav' },
  { key: 'j', action: 'chat-next', context: 'sidebar', label: 'Next', displayKey: 'j / ↓', group: 'nav' },
  { key: 'down', action: 'chat-next', context: 'sidebar', hidden: true },
  { key: 'k', action: 'chat-prev', context: 'sidebar', label: 'Prev', displayKey: 'k / ↑', group: 'nav' },
  { key: 'up', action: 'chat-prev', context: 'sidebar', hidden: true },
  { key: 'enter', action: 'chat-open', context: 'sidebar', label: 'Open', group: 'mode' },
  { key: 'l', action: 'chat-open', context: 'sidebar', hidden: true },
  { key: 'right', action: 'chat-open', context: 'sidebar', hidden: true },
  { key: 'esc', action: 'leave-sidebar', context: 'sidebar', label: 'Panes', group: 'mode', allowInInsert: true },
  { key: 'h', action: 'leave-sidebar', context: 'sidebar', hidden: true },
  { key: 'left', action: 'leave-sidebar', context: 'sidebar', hidden: true },

  // global
  { key: 't', action: 'toggle-tools', context: 'global', label: 'Tools', group: 'view' },
  { key: 'alt+t', action: 'toggle-tools', context: 'global', hidden: true },
  { key: 'r', action: 'refresh', context: 'global', label: 'Reload', group: 'view' },
  { key: 'alt+r', action: 'refresh', context: 'global', hidden: true },
  { key: '/', action: 'search', context: 'global', label: 'Search', group: 'view' },
  { key: 'f', action: 'search', context: 'global', hidden: true },
  { key: 'o', action: 'overview', context: 'global', label: 'Overview', group: 'view' },
  { key: 'ctrl+.', action: 'stop-agent', context: 'global', label: 'Stop agent', group: 'view', allowInInsert: true },
  { key: 'ctrl+alt+m', action: 'silence-audio', context: 'global', label: 'Silence audio', group: 'view', allowInInsert: true },
  { key: 'ctrl+shift+space', action: 'toggle-mic', context: 'global', label: 'Talk', group: 'view', allowInInsert: true },
  { key: '?', action: 'help', context: 'global', label: 'Keys', group: 'view' },
  { key: 'esc', action: 'blur', context: 'global', hidden: true, allowInInsert: true },
];

for (let n = 1; n <= 9; n++) {
  KEYMAP.push({
    key: String(n),
    action: `jump-agent-${n}`,
    context: 'global',
    hidden: n > 1,
    label: n === 1 ? 'Jump' : undefined,
    displayKey: n === 1 ? '1-9' : undefined,
    group: 'nav',
    when: 'hasSessions',
  });
}

// ---- resolution ----------------------------------------------------------

function eligible(def, ctx, contexts) {
  if (!contexts.includes(def.context)) return false;
  if (ctx.insert && !def.allowInInsert) return false;
  if (def.when && !GUARDS[def.when]?.(ctx)) return false;
  return true;
}

// Returns { action, context, key } or null. Null means "not ours" — the caller
// must let the event through untouched rather than swallowing it.
export function resolveAction(key, ctx = {}, keymap = KEYMAP) {
  if (!key) return null;
  const contexts = resolveContexts(ctx);
  for (const context of contexts) {
    for (const def of keymap) {
      if (def.key !== key) continue;
      if (def.context !== context) continue;
      // Passive entries document a key the owning component already handles
      // (Enter in the composer). They belong in the bar, not in dispatch.
      if (def.passive) continue;
      if (!eligible(def, ctx, contexts)) continue;
      return { action: def.action, context, key };
    }
  }
  return null;
}

// ---- shortcut bar --------------------------------------------------------

const GROUP_ORDER = ['mode', 'layout', 'nav', 'view'];

// What the footer shows: exactly the bindings that would fire right now.
export function visibleShortcuts(ctx = {}, keymap = KEYMAP) {
  const contexts = resolveContexts(ctx);
  const out = [];
  const seen = new Set();
  for (const def of keymap) {
    if (def.hidden || !def.label) continue;
    if (!eligible(def, ctx, contexts)) continue;
    if (seen.has(def.action)) continue;
    seen.add(def.action);
    out.push({
      key: def.displayKey || formatKey(def.key),
      label: def.label,
      group: def.group || 'view',
      action: def.action,
    });
  }
  return out.sort((a, b) => GROUP_ORDER.indexOf(a.group) - GROUP_ORDER.indexOf(b.group));
}

// Searchable command-palette entries. Derive these from the same keymap as
// dispatch and the status line, but evaluate pane mode even if the palette
// opened while a text field had focus. Unavailable actions (for example close
// with one pane) remain absent.
export function commandItems(ctx = {}, keymap = KEYMAP) {
  const base = { ...ctx, overlay: '', insert: false, region: 'panes' };
  const contexts = resolveContexts(base);
  const byAction = new Map();
  for (const def of keymap) {
    if (!def.label || def.passive || def.action === 'commands') continue;
    if (!eligible(def, base, contexts)) continue;
    // Prefer the concise pane-mode key over a global compatibility chord,
    // and a visible teaching binding over a hidden alias.
    const priority = (def.hidden ? 10 : 0) + (def.context === 'global' ? 2 : 0);
    const existing = byAction.get(def.action);
    if (existing && existing.priority <= priority) continue;
    byAction.set(def.action, {
      action: def.action,
      label: def.label,
      key: def.displayKey || formatKey(def.key),
      group: def.group || 'view',
      priority,
    });
  }
  const out = [...byAction.values()];
  return out.sort((a, b) => GROUP_ORDER.indexOf(a.group) - GROUP_ORDER.indexOf(b.group)
    || a.label.localeCompare(b.label));
}

// The label shown in the footer's badge: what the keyboard is currently
// talking to, so a dead pane focus is visible instead of mysterious.
export function contextLabel(ctx = {}) {
  if (ctx.overlay) return ctx.overlay.toUpperCase();
  if (ctx.insert) return ctx.zoomed ? 'INSERT · ZOOM' : 'INSERT';
  if (ctx.desktop && ctx.region === 'sidebar') return 'CHATS';
  if (ctx.desktop && ctx.zoomed) return 'ZOOM';
  if (ctx.desktop) return 'PANE';
  return 'NORMAL';
}
