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
  const ESSENTIAL = new Set([
    'leave-insert', 'send', 'focus-composer', 'split-vertical', 'toggle-zoom',
    'nav-left', 'quick-switch', 'commands', 'chat-next', 'chat-open', 'leave-sidebar',
  ]);
  let shown = $derived(shortcuts.filter(sc => ESSENTIAL.has(sc.action)).slice(0, 4));
</script>

<footer class="shortcut-bar" aria-label="Keyboard shortcuts">
  <div class="mode-badge" data-scope={scope} title="Current keyboard context">
    <span class="mode-indicator"></span>
    <span class="mode-name">{label}</span>
  </div>

  <div class="shortcut-list">
    {#each shown as sc (sc.action)}
      <span class="sc-item"><kbd>{sc.key}</kbd><span>{sc.label}</span></span>
    {/each}
  </div>

  <span class="bar-meta">{ctx.paneCount} {ctx.paneCount === 1 ? 'pane' : 'panes'} <i>·</i> <kbd>?</kbd> all keys</span>
</footer>

<style>
  .shortcut-bar {
    /* A flex item in the desktop column, not an overlay: it takes its own
       row at the bottom and nothing has to reserve space for it. */
    flex: none;
    display: flex;
    align-items: center;
    min-height: 25px;
    gap: 12px;
    padding: 3px 9px;
    background: #15161e;
    border-top: 1px solid #2a2c3b;
    font-family: var(--font-mono, monospace);
    font-size: 11px;
    color: #85889f;
    user-select: none;
    overflow-x: auto;
    white-space: nowrap;
  }
  .mode-badge {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 0;
    background: transparent;
    border: 0;
    color: #b8bbd0;
    font-weight: 600;
    letter-spacing: .09em;
    flex: none;
  }
  .mode-badge[data-scope='insert'] {
    background: transparent;
    color: #a7b99d;
  }
  .mode-badge[data-scope='overlay'] {
    background: transparent;
    color: #b5a68f;
  }
  .mode-indicator {
    width: 5px;
    height: 5px;
    border-radius: 50%;
    background: currentColor;
  }
  .shortcut-list {
    display: flex;
    align-items: center;
    gap: 13px;
    font-size: 10px;
    color: #73768e;
  }
  .sc-item {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    color: #777a91;
  }
  .sc-item kbd {
    display: inline-block;
    padding: 0;
    background: transparent;
    border: 0;
    border-radius: 0;
    color: #aeb1c7;
    font-size: 9px;
    font-weight: 600;
    line-height: 1.1;
  }
  .mode-badge[data-scope='insert'] ~ .shortcut-list .sc-item kbd {
    color: #a7b99d;
  }
  .bar-meta {
    margin-left: auto;
    display: inline-flex;
    align-items: center;
    gap: 5px;
    color: #5c5f75;
    font-size: 9px;
  }
  .bar-meta i { color: #393b4c; font-style: normal; }
  .bar-meta kbd { color: #8c8fa7; font: inherit; }
  @media (max-width: 900px) {
    .shortcut-list .sc-item:nth-child(n+3) { display: none; }
    .bar-meta { display: none; }
  }
  :global(html[data-theme="day"]) .shortcut-bar {
    color: var(--washi-dim);
    background: var(--ink-soft);
    border-color: var(--ink-edge);
  }
  :global(html[data-theme="day"]) .mode-badge,
  :global(html[data-theme="day"]) .sc-item,
  :global(html[data-theme="day"]) .sc-item kbd { color: var(--washi); }
  :global(html[data-theme="day"]) .shortcut-list,
  :global(html[data-theme="day"]) .bar-meta { color: var(--washi-low); }
</style>
