<script>
  import PaneLeaf from './PaneLeaf.svelte';
  import SplitResizer from './SplitResizer.svelte';
  import PaneNode from './PaneNode.svelte';
  import { resizeActiveSplit, equalizePaneSplits, panesState } from '../../stores/panes.svelte.js';
  import { findPane } from '@core/pane-tree.js';

  let { node, onTapAgent, onHoldAgent } = $props();

  // If zoomed, render only the zoomed leaf
  let isZoomed = $derived(!!panesState.tree.zoomedPaneId);
  let zoomedNode = $derived.by(() => {
    if (!isZoomed) return null;
    return findPane(panesState.tree.root, panesState.tree.zoomedPaneId);
  });
</script>

{#if isZoomed && zoomedNode}
  <PaneLeaf pane={zoomedNode} {onTapAgent} {onHoldAgent} />
{:else if node.kind === 'leaf'}
  <PaneLeaf pane={node} {onTapAgent} {onHoldAgent} />
{:else if node.kind === 'split'}
  <div
    class="pane-split-container {node.direction}"
    style="--first-ratio: {node.ratio || 0.5}; --second-ratio: {1 - (node.ratio || 0.5)};"
  >
    <div class="pane-split-child" style="flex: {node.ratio || 0.5};">
      <PaneNode node={node.first} {onTapAgent} {onHoldAgent} />
    </div>

    <SplitResizer
      direction={node.direction}
      onResize={(delta) => resizeActiveSplit(delta)}
      onReset={() => equalizePaneSplits()}
    />

    <div class="pane-split-child" style="flex: {1 - (node.ratio || 0.5)};">
      <PaneNode node={node.second} {onTapAgent} {onHoldAgent} />
    </div>
  </div>
{/if}

<style>
  .pane-split-container {
    display: flex;
    flex: 1 1 0;
    min-width: 0;
    min-height: 0;
    width: 100%;
    height: 100%;
    overflow: hidden;
  }
  .pane-split-container.vertical {
    flex-direction: row;
  }
  .pane-split-container.horizontal {
    flex-direction: column;
  }
  .pane-split-child {
    display: flex;
    min-width: 0;
    min-height: 0;
    overflow: hidden;
  }
</style>
