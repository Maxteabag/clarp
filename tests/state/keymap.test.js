// The keyboard layer, pinned against the ways the old modal manager misfired.
//
// Each describe below is a class of "the shortcut didn't work" that the single
// global NORMAL/INSERT switch produced: keys firing while a dialog was open,
// keys firing while typing, pane keys acting on a pane that wasn't there, and
// a footer whose list had drifted from what the keyboard actually did.

import { describe, it, expect } from 'vitest';
import {
  KEYMAP, resolveAction, resolveContexts, visibleShortcuts, contextLabel,
  normalizeKey, formatKey,
} from '@core/keymap.js';

const desktop = (over = {}) => ({ desktop: true, paneCount: 1, sessionCount: 3, ...over });

describe('context stack', () => {
  it('orders most specific first', () => {
    expect(resolveContexts(desktop({ paneCount: 2 }))).toEqual(['pane.multi', 'pane', 'global']);
  });

  it('drops pane contexts while a text field has focus', () => {
    expect(resolveContexts(desktop({ insert: true }))).toEqual(['insert', 'global']);
  });

  it('gives a dialog the keyboard outright', () => {
    expect(resolveContexts(desktop({ overlay: 'switcher' }))).toEqual(['overlay.switcher', 'overlay']);
  });
});

describe('typing never triggers a command', () => {
  it.each(['v', 's', 'x', 'z', 'o', 't', 'r', '/', '1'])('%s is inert in insert', key => {
    expect(resolveAction(key, desktop({ insert: true, paneCount: 2 }))).toBeNull();
  });

  it('esc still leaves insert', () => {
    expect(resolveAction('esc', desktop({ insert: true }))?.action).toBe('leave-insert');
  });

  it('leaves Enter to the composer rather than dispatching it', () => {
    // The composer owns Enter; the keymap only advertises it. Dispatching it
    // here too would send the message twice.
    expect(resolveAction('enter', desktop({ insert: true }))).toBeNull();
    expect(visibleShortcuts(desktop({ insert: true })).map(s => s.action)).toContain('send');
  });
});

describe('an open dialog swallows pane and global keys', () => {
  it.each(['v', 's', 'x', 'z', 'o', 't', '1'])('%s does nothing behind a dialog', key => {
    expect(resolveAction(key, desktop({ overlay: 'overview', paneCount: 2 }))).toBeNull();
  });

  it('esc closes the dialog instead of leaving insert', () => {
    expect(resolveAction('esc', desktop({ overlay: 'overview' }))?.action).toBe('close-overlay');
  });
});

describe('pane keys require a pane to act on', () => {
  it('does not offer close, zoom, balance or move with a single pane', () => {
    const ctx = desktop({ paneCount: 1 });
    for (const key of ['x', 'z', '=', 'h', 'j', 'k', 'l', 'H']) {
      expect(resolveAction(key, ctx), key).toBeNull();
    }
  });

  it('navigates with the arrow keys as well as hjkl', () => {
    const ctx = desktop({ paneCount: 2 });
    expect(resolveAction('left', ctx)?.action).toBe('nav-left');
    expect(resolveAction('right', ctx)?.action).toBe('nav-right');
    expect(resolveAction('up', ctx)?.action).toBe('nav-up');
    expect(resolveAction('down', ctx)?.action).toBe('nav-down');
  });

  it('maps a real arrow keydown through to a pane move', () => {
    const ctx = desktop({ paneCount: 2 });
    expect(resolveAction(normalizeKey({ key: 'ArrowLeft' }), ctx)?.action).toBe('nav-left');
    expect(resolveAction(normalizeKey({ key: 'ArrowDown' }), ctx)?.action).toBe('nav-down');
  });

  it('leaves the arrows to the caret while typing', () => {
    const ctx = desktop({ paneCount: 2, insert: true });
    for (const key of ['left', 'right', 'up', 'down']) {
      expect(resolveAction(key, ctx), key).toBeNull();
    }
  });

  it('offers them once a second pane exists', () => {
    const ctx = desktop({ paneCount: 2 });
    expect(resolveAction('x', ctx)?.action).toBe('close-pane');
    expect(resolveAction('z', ctx)?.action).toBe('toggle-zoom');
    expect(resolveAction('h', ctx)?.action).toBe('nav-left');
    expect(resolveAction('H', ctx)?.action).toBe('shrink');
  });

  it('splits with one pane, since that is how you get a second', () => {
    expect(resolveAction('v', desktop({ paneCount: 1 }))?.action).toBe('split-vertical');
  });

  it('keeps jump keys off when there are no sessions', () => {
    expect(resolveAction('1', desktop({ sessionCount: 0 }))).toBeNull();
  });
});

describe('space opens the quick switcher', () => {
  it('is available from the panes and from the rail', () => {
    expect(resolveAction('space', desktop())?.action).toBe('quick-switch');
    expect(resolveAction('space', desktop({ region: 'sidebar' }))?.action).toBe('quick-switch');
  });

  it('types a space instead while a text field has focus', () => {
    expect(resolveAction('space', desktop({ insert: true }))).toBeNull();
  });

  it('does nothing behind another dialog', () => {
    expect(resolveAction('space', desktop({ overlay: 'overview' }))).toBeNull();
  });

  it('leaves the palette its own keys once open', () => {
    // The palette is an overlay, so only esc resolves; everything else falls
    // through to its input, which is what makes typing a name work.
    const palette = desktop({ overlay: 'quick', insert: true });
    expect(resolveAction('esc', palette)?.action).toBe('close-overlay');
    for (const key of ['space', 'a', 'v', 'j', 'down', 'enter']) {
      expect(resolveAction(key, palette), key).toBeNull();
    }
  });

  it('is advertised in the footer', () => {
    expect(visibleShortcuts(desktop()).map(s => s.action)).toContain('quick-switch');
  });
});

describe('the chat rail is its own focus region', () => {
  const rail = desktop({ region: 'sidebar', paneCount: 2 });

  it('takes the keyboard away from the panes', () => {
    expect(resolveContexts(rail)).toEqual(['sidebar', 'global']);
    expect(resolveAction('v', rail)).toBeNull();
    expect(resolveAction('x', rail)).toBeNull();
  });

  it('walks the list with j/k and the arrows', () => {
    expect(resolveAction('j', rail)?.action).toBe('chat-next');
    expect(resolveAction('down', rail)?.action).toBe('chat-next');
    expect(resolveAction('k', rail)?.action).toBe('chat-prev');
    expect(resolveAction('up', rail)?.action).toBe('chat-prev');
  });

  it('opens with enter and leaves with esc', () => {
    expect(resolveAction('enter', rail)?.action).toBe('chat-open');
    expect(resolveAction('esc', rail)?.action).toBe('leave-sidebar');
    expect(resolveAction('left', rail)?.action).toBe('leave-sidebar');
  });

  it('is entered with c from the panes, not from inside itself', () => {
    expect(resolveAction('c', desktop())?.action).toBe('focus-sidebar');
    expect(resolveAction('c', rail)).toBeNull();
  });

  it('keeps global keys working inside the rail', () => {
    expect(resolveAction('t', rail)?.action).toBe('toggle-tools');
    expect(resolveAction('1', rail)?.action).toBe('jump-agent-1');
  });

  it('says CHATS in the footer and lists its own keys', () => {
    expect(contextLabel(rail)).toBe('CHATS');
    const actions = visibleShortcuts(rail).map(s => s.action);
    expect(actions).toEqual(expect.arrayContaining(['chat-next', 'chat-prev', 'chat-open', 'leave-sidebar']));
    expect(actions).not.toContain('split-vertical');
  });
});

describe('specific context wins over general', () => {
  it('resolves esc to the innermost owner', () => {
    expect(resolveAction('esc', desktop({ overlay: 'start', insert: true }))?.action).toBe('close-overlay');
    expect(resolveAction('esc', desktop({ insert: true }))?.action).toBe('leave-insert');
    expect(resolveAction('esc', desktop())?.action).toBe('blur');
  });

  it('reports which context claimed the key', () => {
    expect(resolveAction('v', desktop())?.context).toBe('pane');
    expect(resolveAction('t', desktop())?.context).toBe('global');
  });
});

describe('mobile has no pane keys', () => {
  it('ignores split and navigation off the desktop layout', () => {
    const ctx = { desktop: false, paneCount: 1, sessionCount: 2 };
    expect(resolveAction('v', ctx)).toBeNull();
    expect(resolveAction('h', ctx)).toBeNull();
    expect(resolveAction('o', ctx)?.action).toBe('overview');
  });
});

describe('the shortcut bar shows exactly what will fire', () => {
  it('lists only resolvable bindings', () => {
    const ctx = desktop({ paneCount: 2 });
    for (const item of visibleShortcuts(ctx)) {
      const def = KEYMAP.find(d => d.action === item.action && !d.hidden);
      expect(resolveAction(def.key, ctx) || def.passive, item.action).toBeTruthy();
    }
  });

  it('hides pane actions that are currently impossible', () => {
    const single = visibleShortcuts(desktop({ paneCount: 1 })).map(s => s.action);
    expect(single).toContain('split-vertical');
    expect(single).not.toContain('close-pane');
    expect(single).not.toContain('toggle-zoom');
  });

  it('collapses aliases to one row per action', () => {
    const actions = visibleShortcuts(desktop({ paneCount: 2 })).map(s => s.action);
    expect(new Set(actions).size).toBe(actions.length);
    // `f` is an alias of `/`; only one Search row may appear.
    expect(actions.filter(a => a === 'search')).toHaveLength(1);
  });

  it('shows the jump range as one row', () => {
    const jump = visibleShortcuts(desktop()).find(s => s.action === 'jump-agent-1');
    expect(jump.key).toBe('1-9');
  });

  it('switches to the insert list while typing', () => {
    const actions = visibleShortcuts(desktop({ insert: true })).map(s => s.action);
    expect(actions).toEqual(expect.arrayContaining(['leave-insert', 'send']));
    expect(actions).not.toContain('split-vertical');
  });
});

describe('context label', () => {
  it.each([
    [{ overlay: 'voice' }, 'VOICE'],
    [{ desktop: true, insert: true }, 'INSERT'],
    [{ desktop: true }, 'PANE'],
    [{}, 'NORMAL'],
  ])('%o reads as %s', (ctx, label) => {
    expect(contextLabel(ctx)).toBe(label);
  });
});

describe('key normalization', () => {
  it.each([
    [{ key: 'Escape' }, 'esc'],
    [{ key: 'ArrowLeft' }, 'left'],
    [{ key: 'v' }, 'v'],
    [{ key: 'H', shiftKey: true }, 'H'],
    [{ key: 'd', ctrlKey: true }, 'ctrl+d'],
    [{ key: 'D', ctrlKey: true, shiftKey: true }, 'ctrl+D'],
    [{ key: 'Escape', ctrlKey: true }, 'ctrl+esc'],
  ])('%o -> %s', (event, expected) => {
    expect(normalizeKey(event)).toBe(expected);
  });

  it('survives a junk event', () => {
    expect(normalizeKey(null)).toBe('');
    expect(normalizeKey({})).toBe('');
  });

  it('formats keys for display', () => {
    expect(formatKey('esc')).toBe('Esc');
    expect(formatKey('left')).toBe('←');
    expect(formatKey('ctrl+d')).toBe('^d');
    expect(formatKey('v')).toBe('v');
  });
});

describe('table integrity', () => {
  it('never binds the same key twice in one context', () => {
    const seen = new Map();
    for (const def of KEYMAP) {
      const id = `${def.context}:${def.key}`;
      expect(seen.has(id), `${id} bound to both ${seen.get(id)} and ${def.action}`).toBe(false);
      seen.set(id, def.action);
    }
  });

  it('gives every visible binding a label and a group', () => {
    for (const def of KEYMAP) {
      if (def.hidden) continue;
      expect(def.label, def.action).toBeTruthy();
      expect(def.group, def.action).toBeTruthy();
    }
  });

  it('names a guard that exists', () => {
    for (const def of KEYMAP) {
      if (!def.when) continue;
      expect(resolveAction(def.key, { desktop: true, paneCount: 9, sessionCount: 9 }), def.action)
        .not.toBeUndefined();
    }
  });
});
