// Pure binary split tree engine for desktop tiling panes.

let paneIdCounter = 0;
function nextPaneId() {
  return `pane-${++paneIdCounter}`;
}

export function createPaneTree(initialSession = 'claude') {
  const root = {
    kind: 'leaf',
    id: nextPaneId(),
    session: initialSession,
    hideTools: false,
  };
  return {
    root,
    activeId: root.id,
    zoomedPaneId: null,
  };
}

export function getLeafPanes(node) {
  if (!node) return [];
  if (node.kind === 'leaf') return [node];
  return [...getLeafPanes(node.first), ...getLeafPanes(node.second)];
}

export function findPane(node, id) {
  if (!node) return null;
  if (node.kind === 'leaf') return node.id === id ? node : null;
  return findPane(node.first, id) || findPane(node.second, id);
}

export function splitPane(tree, targetId, direction, newSession) {
  const newLeaf = {
    kind: 'leaf',
    id: nextPaneId(),
    session: newSession || 'claude',
    hideTools: false,
  };

  function transform(node) {
    if (!node) return null;
    if (node.kind === 'leaf') {
      if (node.id === targetId) {
        return {
          kind: 'split',
          id: `split-${++paneIdCounter}`,
          direction: direction || 'vertical',
          ratio: 0.5,
          first: { ...node },
          second: newLeaf,
        };
      }
      return { ...node };
    }
    return {
      ...node,
      first: transform(node.first),
      second: transform(node.second),
    };
  }

  const newRoot = transform(tree.root);
  return {
    ...tree,
    root: newRoot,
    activeId: newLeaf.id,
    zoomedPaneId: null,
  };
}

export function closePane(tree, targetId) {
  const leaves = getLeafPanes(tree.root);
  if (leaves.length <= 1) {
    // Cannot close the last remaining pane
    return tree;
  }

  function transform(node) {
    if (!node || node.kind === 'leaf') return node;

    // Check if one of our direct children is the leaf to be closed
    if (node.first.kind === 'leaf' && node.first.id === targetId) {
      return transform(node.second);
    }
    if (node.second.kind === 'leaf' && node.second.id === targetId) {
      return transform(node.first);
    }

    return {
      ...node,
      first: transform(node.first),
      second: transform(node.second),
    };
  }

  const newRoot = transform(tree.root);
  const remainingLeaves = getLeafPanes(newRoot);
  let nextActiveId = tree.activeId;
  if (tree.activeId === targetId) {
    nextActiveId = remainingLeaves[0] ? remainingLeaves[0].id : null;
  }

  return {
    ...tree,
    root: newRoot,
    activeId: nextActiveId,
    zoomedPaneId: tree.zoomedPaneId === targetId ? null : tree.zoomedPaneId,
  };
}

export function focusPane(tree, targetId) {
  const exists = findPane(tree.root, targetId);
  if (!exists) return tree;
  return {
    ...tree,
    activeId: targetId,
    // Zoom is a workspace view, not a lock on one pane. If another pane is
    // focused from the command palette or a directional key, show that pane
    // instead of leaving the viewport on a now-inactive leaf.
    zoomedPaneId: tree.zoomedPaneId ? targetId : null,
  };
}

// Project the binary split tree into normalized screen rectangles. Keeping
// this in the pure tree module makes keyboard navigation use the exact same
// ratios as PaneForge without consulting DOM order or stale measurements.
export function paneRects(node, box = { x: 0, y: 0, width: 1, height: 1 }) {
  if (!node) return [];
  if (node.kind === 'leaf') {
    return [{
      id: node.id,
      x: box.x,
      y: box.y,
      width: box.width,
      height: box.height,
      left: box.x,
      right: box.x + box.width,
      top: box.y,
      bottom: box.y + box.height,
      cx: box.x + box.width / 2,
      cy: box.y + box.height / 2,
    }];
  }

  const ratio = clampRatio(node.ratio ?? 0.5);
  if (node.direction === 'horizontal') {
    const firstHeight = box.height * ratio;
    return [
      ...paneRects(node.first, { ...box, height: firstHeight }),
      ...paneRects(node.second, {
        ...box,
        y: box.y + firstHeight,
        height: box.height - firstHeight,
      }),
    ];
  }

  const firstWidth = box.width * ratio;
  return [
    ...paneRects(node.first, { ...box, width: firstWidth }),
    ...paneRects(node.second, {
      ...box,
      x: box.x + firstWidth,
      width: box.width - firstWidth,
    }),
  ];
}

function intervalGap(a0, a1, b0, b1) {
  if (a1 < b0) return b0 - a1;
  if (b1 < a0) return a0 - b1;
  return 0;
}

function compareScore(a, b) {
  for (let i = 0; i < a.length; i++) {
    if (Math.abs(a[i] - b[i]) > 1e-9) return a[i] - b[i];
  }
  return 0;
}

export function navigatePanes(tree, direction) {
  const rects = paneRects(tree.root);
  if (rects.length <= 1) return tree;
  const current = rects.find(rect => rect.id === tree.activeId);
  if (!current) return tree;

  // Explicit previous/next remains available to callers that want traversal.
  if (direction === 'prev' || direction === 'next') {
    const at = rects.indexOf(current);
    const delta = direction === 'prev' ? -1 : 1;
    const target = rects[(at + delta + rects.length) % rects.length];
    return {
      ...tree,
      activeId: target.id,
      zoomedPaneId: tree.zoomedPaneId ? target.id : null,
    };
  }

  const horizontal = direction === 'left' || direction === 'right';
  const negative = direction === 'left' || direction === 'up';
  if (!horizontal && direction !== 'up' && direction !== 'down') return tree;

  let best = null;
  let bestScore = null;
  for (const candidate of rects) {
    if (candidate.id === current.id) continue;
    const primaryDelta = horizontal
      ? candidate.cx - current.cx
      : candidate.cy - current.cy;
    if ((negative && primaryDelta >= -1e-9) || (!negative && primaryDelta <= 1e-9)) {
      continue;
    }
    const extendsPastEdge = direction === 'left'
      ? candidate.left < current.left - 1e-9
      : direction === 'right'
        ? candidate.right > current.right + 1e-9
        : direction === 'up'
          ? candidate.top < current.top - 1e-9
          : candidate.bottom > current.bottom + 1e-9;
    if (!extendsPastEdge) continue;

    const orthGap = horizontal
      ? intervalGap(current.top, current.bottom, candidate.top, candidate.bottom)
      : intervalGap(current.left, current.right, candidate.left, candidate.right);
    const primaryGap = horizontal
      ? (negative
          ? Math.max(0, current.left - candidate.right)
          : Math.max(0, candidate.left - current.right))
      : (negative
          ? Math.max(0, current.top - candidate.bottom)
          : Math.max(0, candidate.top - current.bottom));
    const orthCenter = horizontal
      ? Math.abs(candidate.cy - current.cy)
      : Math.abs(candidate.cx - current.cx);

    // A pane whose perpendicular span overlaps the current one always beats
    // a diagonal pane. Within that band, take the nearest edge, then the most
    // closely aligned centre. This is stable for regular grids and intuitive
    // for irregular nested splits.
    const score = [orthGap > 1e-9 ? 1 : 0, primaryGap, orthGap, orthCenter,
      Math.abs(primaryDelta)];
    if (!bestScore || compareScore(score, bestScore) < 0) {
      best = candidate;
      bestScore = score;
    }
  }

  // Directional navigation stops at an outer edge. Wrapping from the top row
  // to an unrelated bottom pane makes a spatial shortcut feel non-spatial.
  if (!best) return tree;

  return {
    ...tree,
    activeId: best.id,
    zoomedPaneId: tree.zoomedPaneId ? best.id : null,
  };
}

export function setPaneSession(tree, targetId, session) {
  function transform(node) {
    if (!node) return node;
    if (node.kind === 'leaf') {
      return node.id === targetId ? { ...node, session } : node;
    }
    return { ...node, first: transform(node.first), second: transform(node.second) };
  }
  return { ...tree, root: transform(tree.root) };
}

export function toggleZoom(tree, targetId = tree.activeId) {
  return {
    ...tree,
    zoomedPaneId: tree.zoomedPaneId === targetId ? null : targetId,
  };
}

function clampRatio(ratio) {
  return Math.max(0.15, Math.min(0.85, ratio));
}

// Nudge the split nearest the target pane. Every ancestor split contains the
// target too, but resizing all of them at once moved every seam on the path
// to the root; the pane's own boundary is the one the user means.
export function resizeSplit(tree, targetId, delta) {
  function transform(node) {
    if (!node || node.kind === 'leaf') return node;
    const inFirst = !!findPane(node.first, targetId);
    const inSecond = !!findPane(node.second, targetId);
    if (!inFirst && !inSecond) return node;
    const child = inFirst ? node.first : node.second;
    if (child.kind === 'split') {
      return inFirst
        ? { ...node, first: transform(node.first) }
        : { ...node, second: transform(node.second) };
    }
    return { ...node, ratio: clampRatio((node.ratio || 0.5) + delta) };
  }
  return { ...tree, root: transform(tree.root) };
}

// Set a split's ratio outright. The resizable-pane view reports drags this
// way; returning the same tree when nothing changed keeps a no-op layout
// report from re-rendering every pane.
export function setSplitRatio(tree, splitId, ratio) {
  const next = clampRatio(ratio);
  function transform(node) {
    if (!node || node.kind === 'leaf') return node;
    if (node.id === splitId) {
      return Math.abs((node.ratio || 0.5) - next) < 1e-4 ? node : { ...node, ratio: next };
    }
    const first = transform(node.first);
    const second = transform(node.second);
    return first === node.first && second === node.second ? node : { ...node, first, second };
  }
  const root = transform(tree.root);
  return root === tree.root ? tree : { ...tree, root };
}

export function equalizeSplits(tree) {
  function transform(node) {
    if (!node || node.kind === 'leaf') return node;
    return {
      ...node,
      ratio: 0.5,
      first: transform(node.first),
      second: transform(node.second),
    };
  }

  return {
    ...tree,
    root: transform(tree.root),
  };
}
