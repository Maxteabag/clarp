<script>
  // Rendered from the keymap, never from a hand-written list: the footer shows
  // exactly the bindings that would fire right now, and cannot drift.
  import { visibleShortcuts, contextLabel } from '@core/keymap.js';
  import { input, keyContext } from '../../stores/input.svelte.js';
  import { panesState } from '../../stores/panes.svelte.js';

  // Read the reactive state at the top level so @Observable-style tracking
  // sees it; a read buried in a helper would not re-run this.
  let ctx = $derived.by(() => {
    void input.insert; void input.overlay; void input.hoveredPaneId;
    void panesState.tree;
    return keyContext();
  });
  let shortcuts = $derived(visibleShortcuts(ctx));
  let label = $derived(contextLabel(ctx));
  let scope = $derived(ctx.overlay ? 'overlay' : ctx.insert ? 'insert' : 'normal');
</script>

<footer class="shortcut-bar" aria-label="Keyboard shortcuts">
  <div class="mode-badge" data-scope={scope}>
    <span class="mode-indicator"></span>
    <span class="mode-name">{label}</span>
  </div>

  {#if ctx.hovering && !ctx.insert && !ctx.overlay}
    <span class="target-hint" title="Pane keys act on the pane under the pointer">
      → hovered pane
    </span>
  {/if}

  <div class="shortcut-list">
    {#each shortcuts as sc, i (sc.action)}
      {#if i > 0}<span class="sc-divider">·</span>{/if}
      <span class="sc-item"><kbd>{sc.key}</kbd> {sc.label}</span>
    {/each}
  </div>
</footer>

<style>
  .shortcut-bar {
    /* A flex item in the desktop column, not an overlay: it takes its own
       row at the bottom and nothing has to reserve space for it. */
    flex: none;
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 3px 12px;
    background: #13141c;
    border-top: 1px solid rgba(255, 255, 255, 0.08);
    font-family: var(--font-mono, monospace);
    font-size: 11px;
    color: #a9b1d6;
    user-select: none;
    overflow-x: auto;
    white-space: nowrap;
  }
  .mode-badge {
    display: flex;
    align-items: center;
    gap: 5px;
    padding: 2px 7px;
    background: rgba(122, 162, 247, 0.15);
    border: 1px solid rgba(122, 162, 247, 0.4);
    border-radius: 3px;
    color: #7aa2f7;
    font-weight: 700;
    letter-spacing: 0.06em;
    flex: none;
  }
  .mode-badge[data-scope='insert'] {
    background: rgba(158, 206, 106, 0.15);
    border-color: rgba(158, 206, 106, 0.4);
    color: #9ece6a;
  }
  .mode-badge[data-scope='overlay'] {
    background: rgba(255, 158, 100, 0.15);
    border-color: rgba(255, 158, 100, 0.4);
    color: #ff9e64;
  }
  .mode-indicator {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: currentColor;
  }
  .target-hint {
    flex: none;
    color: #7aa2f7;
    font-size: 10px;
    opacity: 0.85;
  }
  .shortcut-list {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 11px;
    color: #787c99;
  }
  .sc-item {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    color: #c0caf5;
  }
  .sc-item kbd {
    display: inline-block;
    padding: 1px 4px;
    background: rgba(255, 255, 255, 0.08);
    border: 1px solid rgba(255, 255, 255, 0.15);
    border-radius: 3px;
    color: #ff9e64;
    font-size: 10px;
    font-weight: 600;
    line-height: 1.1;
  }
  .mode-badge[data-scope='insert'] ~ .shortcut-list .sc-item kbd {
    color: #9ece6a;
  }
  .sc-divider {
    color: rgba(255, 255, 255, 0.15);
  }
</style>
