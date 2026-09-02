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
  };
}

export function navigatePanes(tree, direction) {
  const leaves = getLeafPanes(tree.root);
  if (leaves.length <= 1) return tree;

  const currentIndex = leaves.findIndex(l => l.id === tree.activeId);
  if (currentIndex === -1) return tree;

  let nextIndex = currentIndex;
  if (direction === 'left' || direction === 'up' || direction === 'prev') {
    nextIndex = (currentIndex - 1 + leaves.length) % leaves.length;
  } else {
    nextIndex = (currentIndex + 1) % leaves.length;
  }

  return {
    ...tree,
    activeId: leaves[nextIndex].id,
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

export function resizeSplit(tree, targetId, delta) {
  function transform(node) {
    if (!node || node.kind === 'leaf') return node;
    const hasTargetInFirst = !!findPane(node.first, targetId);
    const hasTargetInSecond = !!findPane(node.second, targetId);

    if (hasTargetInFirst || hasTargetInSecond) {
      const nextRatio = Math.max(0.15, Math.min(0.85, (node.ratio || 0.5) + delta));
      return {
        ...node,
        ratio: nextRatio,
        first: transform(node.first),
        second: transform(node.second),
      };
    }
    return {
      ...node,
      first: transform(node.first),
      second: transform(node.second),
    };
  }

  return {
    ...tree,
    root: transform(tree.root),
  };
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
