import { describe, it, expect } from 'vitest';
import { quickSwitchRows, resolveChoice, clampIndex } from '@core/quick-switch.js';
import { createPaneTree, splitPane, getLeafPanes, setPaneSession } from '@core/pane-tree.js';

const chat = (name, session, over = {}) => ({ name, session, busy: false, ...over });

describe('quickSwitchRows', () => {
  it('puts live chats before contacts', () => {
    const rows = quickSwitchRows({
      chats: [chat('Freya', 'freya-3694')],
      contacts: [{ name: 'Adam' }],
    });
    expect(rows.map(r => [r.kind, r.name])).toEqual([['chat', 'Freya'], ['contact', 'Adam']]);
  });

  it('labels a contact as starting a new session', () => {
    const [row] = quickSwitchRows({ contacts: [{ name: 'Nadia' }] });
    expect(row.session).toBe('');
    expect(row.detail).toBe('Start a new session');
  });

  it('reports what a live chat is doing', () => {
    const [busy] = quickSwitchRows({ chats: [chat('Freya', 'f', { busy: true })] });
    expect(busy.detail).toBe('Working');
    const [idle] = quickSwitchRows({ chats: [chat('Sam', 's')] });
    expect(idle.detail).toBe('Idle');
  });

  it('floats name matches above workspace and model matches', () => {
    // buildAgentOverview returns all three; only the first is a name match.
    const rows = quickSwitchRows({
      chats: [chat('Gordon', 'g'), chat('Josh', 'j'), chat('Adam', 'a')],
    }, 12, 'ad');
    expect(rows[0].name).toBe('Adam');
  });

  it('prefers a prefix over a substring', () => {
    const rows = quickSwitchRows({
      chats: [chat('Nadia', 'n'), chat('Adam', 'a')],
    }, 12, 'ad');
    expect(rows.map(r => r.name)).toEqual(['Adam', 'Nadia']);
  });

  it('keeps the given order when nothing matches by name', () => {
    const rows = quickSwitchRows({ chats: [chat('Zed', 'z'), chat('Yan', 'y')] }, 12, 'qqq');
    expect(rows.map(r => r.name)).toEqual(['Zed', 'Yan']);
  });

  it('keeps the given order with no query at all', () => {
    const rows = quickSwitchRows({ chats: [chat('Zed', 'z'), chat('Adam', 'a')] });
    expect(rows.map(r => r.name)).toEqual(['Zed', 'Adam']);
  });

  it('keeps the list short enough to scan', () => {
    const chats = Array.from({ length: 40 }, (_, i) => chat(`A${i}`, `s${i}`));
    expect(quickSwitchRows({ chats })).toHaveLength(12);
  });

  it('gives every row a stable key', () => {
    const rows = quickSwitchRows({ chats: [chat('Freya', 'f')], contacts: [{ name: 'Freya' }] });
    expect(new Set(rows.map(r => r.key)).size).toBe(2);
  });
});

describe('resolveChoice', () => {
  // Two panes: pane A shows freya, pane B (active) shows adam.
  function twoPanes() {
    let tree = createPaneTree('freya-3694');
    tree = splitPane(tree, tree.activeId, 'vertical', 'adam');
    const leaves = getLeafPanes(tree.root);
    return { tree, leaves, activeId: tree.activeId };
  }

  it('focuses the pane already showing the session', () => {
    const { leaves, activeId } = twoPanes();
    const target = leaves.find(l => l.session === 'freya-3694');
    const row = { kind: 'chat', name: 'Freya', session: 'freya-3694' };
    expect(resolveChoice(row, leaves, activeId)).toEqual({
      action: 'focus', paneId: target.id, session: 'freya-3694',
    });
  });

  it('retargets the active pane when nothing is showing the session', () => {
    const { leaves, activeId } = twoPanes();
    const row = { kind: 'chat', name: 'Sam', session: 'sam-1' };
    expect(resolveChoice(row, leaves, activeId)).toEqual({
      action: 'switch', paneId: activeId, session: 'sam-1',
    });
  });

  it('stays put when the active pane already shows it', () => {
    const { leaves, activeId } = twoPanes();
    const active = leaves.find(l => l.id === activeId);
    const row = { kind: 'chat', name: 'Adam', session: active.session };
    expect(resolveChoice(row, leaves, activeId).action).toBe('switch');
  });

  it('creates a session for a contact', () => {
    const { leaves, activeId } = twoPanes();
    expect(resolveChoice({ kind: 'contact', name: 'Nadia', session: '' }, leaves, activeId))
      .toEqual({ action: 'create', name: 'Nadia' });
  });

  it('creates when a chat row somehow has no session', () => {
    expect(resolveChoice({ kind: 'chat', name: 'Ghost', session: '' }, [], '').action).toBe('create');
  });

  it('is null-safe', () => {
    expect(resolveChoice(null, [], '')).toBeNull();
  });
});

describe('setPaneSession', () => {
  it('retargets one leaf and leaves the rest alone', () => {
    let tree = createPaneTree('freya-3694');
    tree = splitPane(tree, tree.activeId, 'vertical', 'adam');
    const before = getLeafPanes(tree.root);
    const target = before[0];

    const next = setPaneSession(tree, target.id, 'sam-1');
    const after = getLeafPanes(next.root);

    expect(after.find(l => l.id === target.id).session).toBe('sam-1');
    for (const leaf of after) {
      if (leaf.id === target.id) continue;
      expect(leaf.session).toBe(before.find(b => b.id === leaf.id).session);
    }
  });

  it('does not mutate the tree it was given', () => {
    let tree = createPaneTree('freya-3694');
    tree = splitPane(tree, tree.activeId, 'vertical', 'adam');
    const original = getLeafPanes(tree.root)[0].session;
    setPaneSession(tree, getLeafPanes(tree.root)[0].id, 'other');
    expect(getLeafPanes(tree.root)[0].session).toBe(original);
  });

  it('ignores an unknown pane id', () => {
    const tree = createPaneTree('freya-3694');
    const next = setPaneSession(tree, 'nope', 'sam-1');
    expect(getLeafPanes(next.root)[0].session).toBe('freya-3694');
  });
});

describe('clampIndex', () => {
  it.each([[0, 3, 0], [2, 3, 2], [3, 3, 0], [-1, 3, 2], [4, 3, 1]])(
    'wraps %i over %i to %i', (index, length, expected) => {
      expect(clampIndex(index, length)).toBe(expected);
    });

  it('is 0 for an empty list', () => {
    expect(clampIndex(5, 0)).toBe(0);
  });
});
