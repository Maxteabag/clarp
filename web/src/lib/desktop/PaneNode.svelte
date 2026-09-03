<script>
  import { untrack } from 'svelte';
  import { PaneGroup, Pane, PaneResizer } from 'paneforge';
  import PaneLeaf from './PaneLeaf.svelte';
  import PaneNode from './PaneNode.svelte';
  import { equalizePaneSplits, panesState, setSplitRatio } from '../../stores/panes.svelte.js';
  import { findPane } from '@core/pane-tree.js';

  let { node, onTapAgent, onHoldAgent } = $props();

  // If zoomed, render only the zoomed leaf
  let isZoomed = $derived(!!panesState.tree.zoomedPaneId);
  let zoomedNode = $derived.by(() => {
    if (!isZoomed) return null;
    return findPane(panesState.tree.root, panesState.tree.zoomedPaneId);
  });

  // The tree names a split by its seam: 'vertical' is a vertical cut, panes
  // side by side. PaneForge names it by the axis the panes run along, so the
  // same layout is 'horizontal' there.
  let groupDirection = $derived(node.direction === 'horizontal' ? 'vertical' : 'horizontal');

  // The tree's ratio is the one source of truth for a split; PaneForge is its
  // view. A drag reports up through onLayoutChange, and keyboard resizes or
  // "balance" change the ratio and flow down through setLayout.
  //
  // defaultSize is read once on purpose: a reactive default would make
  // PaneForge re-derive the layout on every drag.
  const initialFirst = untrack(() => (node.ratio ?? 0.5) * 100);
  let group = $state(null);
  let firstPct = $derived((node.ratio ?? 0.5) * 100);
  $effect(() => {
    if (!group) return;
    const layout = group.getLayout();
    if (layout.length === 2 && Math.abs(layout[0] - firstPct) > 0.01) {
      group.setLayout([firstPct, 100 - firstPct]);
    }
  });
  function onLayoutChange(layout) {
    if (layout.length === 2) setSplitRatio(node.id, layout[0] / 100);
  }
</script>

{#if isZoomed && zoomedNode}
  <PaneLeaf pane={zoomedNode} {onTapAgent} {onHoldAgent} />
{:else if node.kind === 'leaf'}
  <PaneLeaf pane={node} {onTapAgent} {onHoldAgent} />
{:else if node.kind === 'split'}
  <PaneGroup direction={groupDirection} class="pane-split" bind:this={group} {onLayoutChange}>
    <Pane id="{node.id}-first" class="pane-split-child" order={1} defaultSize={initialFirst} minSize={15}>
      <PaneNode node={node.first} {onTapAgent} {onHoldAgent} />
    </Pane>
    <PaneResizer class="split-resizer" ondblclick={() => equalizePaneSplits()} aria-label="Resize panes" />
    <Pane id="{node.id}-second" class="pane-split-child" order={2} defaultSize={100 - initialFirst} minSize={15}>
      <PaneNode node={node.second} {onTapAgent} {onHoldAgent} />
    </Pane>
  </PaneGroup>
{/if}

<style>
  /* PaneForge components, so :global — see DesktopShell.svelte. */
  :global(.pane-split) { flex: 1 1 0; min-width: 0; min-height: 0; }
  :global(.pane-split-child) { display: flex; min-width: 0; min-height: 0; }

  :global(.split-resizer) {
    position: relative;
    z-index: 5;
    flex: 0 0 auto;
    background: rgba(255, 255, 255, 0.08);
    transition: background var(--t-snap) var(--ease);
  }
  :global(.split-resizer[data-direction="horizontal"]) { width: 6px; margin: 0 -3px; }
  :global(.split-resizer[data-direction="vertical"]) { height: 6px; margin: -3px 0; }
  :global(.split-resizer:hover),
  :global(.split-resizer[data-active]) { background: var(--accent-blue); }
</style>
