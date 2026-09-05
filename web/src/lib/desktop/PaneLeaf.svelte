<script>
  import Conversation from '../Conversation.svelte';
  import {
    panesState,
    focusActivePane,
  } from '../../stores/panes.svelte.js';
  import { input, setHoveredPane, setInsert, setRegion } from '../../stores/input.svelte.js';

  let { pane, paneIndex = 1, onTapAgent, onHoldAgent } = $props();

  let isActive = $derived(panesState.tree.activeId === pane.id);
  // A restrained hover preview; click promotes it to keyboard focus.
  let isHovered = $derived(input.hoveredPaneId === pane.id && !isActive);
  function onClickPane(e) {
    if (!isActive) {
      focusActivePane(pane.id);
    }
    // Clicking a pane is an explicit focus change. Do not leave a hidden
    // caret in the global composer while the visible pane focus says another
    // thing; `i` returns to the draft without losing it.
    if (!e?.target?.closest?.('button, a, input, textarea, select, [contenteditable="true"]')) {
      setRegion('panes');
      setInsert(false);
      e?.currentTarget?.focus?.({ preventScroll: true });
    }
  }
</script>

<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
<section
  class="pane-leaf"
  class:active={isActive}
  class:hovered={isHovered}
  tabindex="-1"
  onclick={onClickPane}
  onpointerenter={() => setHoveredPane(pane.id)}
  onpointerleave={() => setHoveredPane('')}
  onkeydown={(e) => { if (e.key === 'Enter') onClickPane(e); }}
  aria-label={`Pane ${paneIndex}: ${pane.session}`}
  data-pane-id={pane.id}
>
  <div class="pane-content">
    <Conversation session={pane.session} showConnDot={isActive} showActions={false}
                  quietIdentity {onTapAgent} {onHoldAgent} />
  </div>
</section>

<style>
  .pane-leaf {
    position: relative;
    display: flex;
    flex-direction: column;
    flex: 1 1 0;
    min-width: 0;
    min-height: 0;
    height: 100%;
    container-type: inline-size;
    background: #171923;
    border: 1px solid #303347;
    border-radius: 5px;
    overflow: hidden;
    opacity: .76;
    transition: border-color 120ms ease, box-shadow 120ms ease,
                background 120ms ease, opacity 120ms ease;
  }
  .pane-leaf.active {
    z-index: 2;
    opacity: 1;
    background: #23273b;
    border: 2px solid #a7addb;
    box-shadow: inset 0 0 0 1px rgba(187, 192, 237, .13),
                0 0 0 1px rgba(97, 104, 149, .42);
  }
  .pane-leaf.active::before {
    content: '';
    position: absolute;
    z-index: 3;
    inset: 0 0 auto;
    height: 3px;
    background: #bbc0ed;
    pointer-events: none;
  }
  /* Where a pane key would land right now, when that is not the focused pane. */
  .pane-leaf.hovered {
    opacity: .9;
    border-color: #555a78;
  }
  .pane-content {
    flex: 1 1 auto;
    display: flex;
    flex-direction: column;
    min-height: 0;
    overflow: hidden;
  }
  .pane-leaf.active :global(.history) { background: #202438; }
  .pane-leaf.active :global(.history-head) { background: #292d44; }
  .pane-leaf:not(.active) :global(.history) { background: #171923; }
  .pane-leaf:not(.active) :global(.history-head) { background: #1b1d29; }
  :global(html[data-theme="day"]) .pane-leaf {
    background: var(--ink);
    border-color: var(--ink-edge);
  }
  :global(html[data-theme="day"]) .pane-leaf.active {
    border-color: var(--accent-blue);
    box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--accent-blue) 14%, transparent);
  }
  :global(html[data-theme="day"]) .pane-leaf.active::before { background: var(--accent-blue); }
  :global(html[data-theme="day"]) .pane-leaf.active :global(.history),
  :global(html[data-theme="day"]) .pane-leaf:not(.active) :global(.history) {
    background: var(--ink);
  }
  :global(html[data-theme="day"]) .pane-leaf.active :global(.history-head),
  :global(html[data-theme="day"]) .pane-leaf:not(.active) :global(.history-head) {
    background: var(--ink-soft);
  }
</style>
