import { describe, it, expect } from 'vitest';
import {
  createPaneTree,
  splitPane,
  closePane,
  focusPane,
  navigatePanes,
  toggleZoom,
  resizeSplit,
  setSplitRatio,
  equalizeSplits,
  getLeafPanes,
  paneRects,
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

  it('moves geometrically through a 4x4 grid and stops at its edges', () => {
    const leaf = id => ({ kind: 'leaf', id, session: id, hideTools: false });
    const split = (id, direction, first, second) => ({
      kind: 'split', id, direction, ratio: 0.5, first, second,
    });
    const column = (n) => split(`rows-${n}`, 'horizontal',
      split(`rows-${n}-top`, 'horizontal', leaf(`p${n}1`), leaf(`p${n}2`)),
      split(`rows-${n}-bottom`, 'horizontal', leaf(`p${n}3`), leaf(`p${n}4`)));
    const root = split('cols', 'vertical',
      split('cols-left', 'vertical', column(1), column(2)),
      split('cols-right', 'vertical', column(3), column(4)));
    let grid = { root, activeId: 'p22', zoomedPaneId: null };

    expect(paneRects(root)).toHaveLength(16);
    grid = navigatePanes(grid, 'right');
    expect(grid.activeId).toBe('p32');
    grid = navigatePanes(grid, 'down');
    expect(grid.activeId).toBe('p33');
    grid = navigatePanes(grid, 'left');
    expect(grid.activeId).toBe('p23');
    grid = navigatePanes({ ...grid, activeId: 'p13' }, 'left');
    expect(grid.activeId).toBe('p13');
  });

  it('keeps focus and the visible zoomed pane together while navigating', () => {
    let tree = createPaneTree('p1');
    tree = splitPane(tree, tree.activeId, 'vertical', 'p2');
    tree = toggleZoom(tree);
    const p1 = tree.root.first.id;

    tree = navigatePanes(tree, 'left');
    expect(tree.activeId).toBe(p1);
    expect(tree.zoomedPaneId).toBe(p1);
  });

  it('does not move sideways from a pane that already spans that workspace edge', () => {
    const leaf = id => ({ kind: 'leaf', id, session: id, hideTools: false });
    const top = {
      kind: 'split', id: 'top', direction: 'vertical', ratio: 0.5,
      first: leaf('top-left'), second: leaf('top-right'),
    };
    const root = {
      kind: 'split', id: 'root', direction: 'horizontal', ratio: 0.5,
      first: top, second: leaf('bottom'),
    };
    const tree = { root, activeId: 'bottom', zoomedPaneId: null };

    expect(navigatePanes(tree, 'left').activeId).toBe('bottom');
    expect(navigatePanes(tree, 'right').activeId).toBe('bottom');
    expect(navigatePanes({ ...tree, activeId: 'top-left' }, 'right').activeId)
      .toBe('top-right');
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

  it('resizes only the split nearest the active pane', () => {
    let tree = createPaneTree('p1');
    tree = splitPane(tree, tree.activeId, 'vertical', 'p2');
    tree = splitPane(tree, tree.activeId, 'horizontal', 'p3');
    tree = resizeSplit(tree, tree.activeId, 0.1);
    expect(tree.root.ratio).toBe(0.5);
    expect(tree.root.second.ratio).toBeCloseTo(0.6);
  });

  it('sets a split ratio by id and returns the same tree when unchanged', () => {
    let tree = createPaneTree('p1');
    tree = splitPane(tree, tree.activeId, 'vertical', 'p2');
    const next = setSplitRatio(tree, tree.root.id, 0.3);
    expect(next.root.ratio).toBeCloseTo(0.3);
    expect(setSplitRatio(next, next.root.id, 0.3)).toBe(next);
    expect(setSplitRatio(next, next.root.id, 0.02).root.ratio).toBe(0.15);
  });
});
