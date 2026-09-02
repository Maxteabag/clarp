import { describe, it, expect } from 'vitest';
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
} from '../../static/lib/pane-tree.js';

describe('PaneTree - Split & Navigation Logic', () => {
  it('creates an initial tree with a single leaf pane', () => {
    const tree = createPaneTree('rachel-0150');
    expect(tree.root.kind).toBe('leaf');
    expect(tree.root.session).toBe('rachel-0150');
    expect(tree.activeId).toBe(tree.root.id);
    expect(getLeafPanes(tree.root).length).toBe(1);
  });

  it('splits a pane vertically into two side-by-side panes', () => {
    const tree = createPaneTree('rachel-0150');
    const firstId = tree.root.id;

    const nextTree = splitPane(tree, firstId, 'vertical', 'bella-807a');
    expect(nextTree.root.kind).toBe('split');
    expect(nextTree.root.direction).toBe('vertical');
    expect(nextTree.root.ratio).toBe(0.5);
    expect(nextTree.root.first.session).toBe('rachel-0150');
    expect(nextTree.root.second.session).toBe('bella-807a');
    // Active ID should shift to the newly created pane
    expect(nextTree.activeId).toBe(nextTree.root.second.id);
    expect(getLeafPanes(nextTree.root).length).toBe(2);
  });

  it('splits a pane horizontally into stacked panes (nested split)', () => {
    let tree = createPaneTree('rachel-0150');
    tree = splitPane(tree, tree.activeId, 'vertical', 'bella-807a');
    const bellaId = tree.activeId;

    // Split Bella horizontally into Bella (top) and Adam (bottom)
    tree = splitPane(tree, bellaId, 'horizontal', 'adam');

    const leaves = getLeafPanes(tree.root);
    expect(leaves.length).toBe(3);
    expect(leaves.map(l => l.session)).toEqual(['rachel-0150', 'bella-807a', 'adam']);
    expect(tree.activeId).toBe(leaves[2].id);
  });

  it('closes a pane and collapses the split parent to the remaining sibling', () => {
    let tree = createPaneTree('rachel-0150');
    tree = splitPane(tree, tree.activeId, 'vertical', 'bella-807a');
    const bellaId = tree.activeId;

    tree = closePane(tree, bellaId);
    expect(tree.root.kind).toBe('leaf');
    expect(tree.root.session).toBe('rachel-0150');
    expect(tree.activeId).toBe(tree.root.id);
    expect(getLeafPanes(tree.root).length).toBe(1);
  });

  it('navigates pane focus using spatial directions', () => {
    let tree = createPaneTree('p1');
    tree = splitPane(tree, tree.activeId, 'vertical', 'p2'); // [p1 | p2]
    const p2Id = tree.activeId;
    const p1Id = tree.root.first.id;

    // Navigate left from p2 -> p1
    let navTree = navigatePanes(tree, 'left');
    expect(navTree.activeId).toBe(p1Id);

    // Navigate right from p1 -> p2
    navTree = navigatePanes(navTree, 'right');
    expect(navTree.activeId).toBe(p2Id);
  });

  it('toggles zoom on the active pane without losing tree hierarchy', () => {
    let tree = createPaneTree('p1');
    tree = splitPane(tree, tree.activeId, 'vertical', 'p2');
    expect(tree.zoomedPaneId).toBeNull();

    tree = toggleZoom(tree);
    expect(tree.zoomedPaneId).toBe(tree.activeId);

    tree = toggleZoom(tree);
    expect(tree.zoomedPaneId).toBeNull();
  });

  it('resizes split ratios and equalizes', () => {
    let tree = createPaneTree('p1');
    tree = splitPane(tree, tree.activeId, 'vertical', 'p2');
    expect(tree.root.ratio).toBe(0.5);

    tree = resizeSplit(tree, tree.activeId, 0.1);
    expect(tree.root.ratio).toBeCloseTo(0.6);

    tree = equalizeSplits(tree);
    expect(tree.root.ratio).toBe(0.5);
  });
});
