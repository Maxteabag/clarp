<script>
  import Conversation from '../Conversation.svelte';
  import {
    panesState,
    focusActivePane,
    closeActivePane,
    splitActivePane,
    toggleZoomActive,
  } from '../../stores/panes.svelte.js';
  import { agentSnapshot, avatarUrl, AVATAR_PALETTE } from '../../stores/app.svelte.js';
  import { input, setHoveredPane } from '../../stores/input.svelte.js';

  let { pane, paneIndex = 1, onTapAgent, onHoldAgent } = $props();

  let isActive = $derived(panesState.tree.activeId === pane.id);
  // Pane keys act on the pane under the pointer, so show which one that is.
  let isHovered = $derived(input.hoveredPaneId === pane.id && !isActive);
  let agentMeta = $derived.by(() => {
    const list = agentSnapshot.agents || [];
    return list.find(a => a.session === pane.session) || { persona: pane.session, backend: 'claude' };
  });

  function onClickPane() {
    if (!isActive) {
      focusActivePane(pane.id);
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
  onkeydown={(e) => { if (e.key === 'Enter') onClickPane(); }}
  aria-label={`Pane ${paneIndex}: ${pane.session}`}
>
  <header class="pane-header">
    <div class="pane-title-group">
      <span class="pane-num-badge">[{paneIndex}]</span>
      <span
        class="pane-avatar"
        style="background-color:{AVATAR_PALETTE[agentMeta.persona] || 'var(--ochre)'};background-image:url('{avatarUrl(agentMeta.persona, pane.session)}')"
      ></span>
      <span class="pane-name">{agentMeta.persona || pane.session}</span>
      {#if agentMeta.backend}
        <span class="pane-backend-tag">{agentMeta.backend}</span>
      {/if}
    </div>

    <div class="pane-controls">
      <button class="pane-btn" title="Split Vertically (v)" onclick={(e) => { e.stopPropagation(); splitActivePane('vertical'); }}>
        <svg viewBox="0 0 24 24" width="12" height="12" fill="currentColor">
          <path d="M4 4h7v16H4V4zm9 0h7v16h-7V4z"/>
        </svg>
      </button>
      <button class="pane-btn" title="Split Horizontally (s)" onclick={(e) => { e.stopPropagation(); splitActivePane('horizontal'); }}>
        <svg viewBox="0 0 24 24" width="12" height="12" fill="currentColor">
          <path d="M4 4h16v7H4V4zm0 9h16v7H4v-7z"/>
        </svg>
      </button>
      <button class="pane-btn" title="Zoom / Maximize (z)" onclick={(e) => { e.stopPropagation(); toggleZoomActive(); }}>
        <svg viewBox="0 0 24 24" width="12" height="12" fill="currentColor">
          <path d="M4 4h6v2H6v4H4V4zm16 0v6h-2V6h-4V4h6zm0 16h-6v-2h4v-4h2v6zm-16 0v-6h2v4h4v2H4z"/>
        </svg>
      </button>
      <button class="pane-btn close" title="Close Pane (x)" onclick={(e) => { e.stopPropagation(); closeActivePane(pane.id); }}>
        ✕
      </button>
    </div>
  </header>

  <div class="pane-content">
    <Conversation session={pane.session} showConnDot {onTapAgent} {onHoldAgent} />
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
    background: var(--ink, #16161e);
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 4px;
    overflow: hidden;
    transition: border-color 0.15s ease, box-shadow 0.15s ease;
  }
  .pane-leaf.active {
    border-color: #7aa2f7;
    box-shadow: 0 0 0 1px rgba(122, 162, 247, 0.4), 0 4px 12px rgba(0, 0, 0, 0.3);
  }
  /* Where a pane key would land right now, when that is not the focused pane. */
  .pane-leaf.hovered {
    border-color: rgba(122, 162, 247, 0.55);
    border-style: dashed;
  }
  .pane-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 4px 8px;
    background: #1a1b26;
    border-bottom: 1px solid rgba(255, 255, 255, 0.06);
    user-select: none;
  }
  .pane-leaf.active .pane-header {
    background: #24283b;
    border-bottom-color: rgba(122, 162, 247, 0.2);
  }
  .pane-title-group {
    display: flex;
    align-items: center;
    gap: 6px;
    font-family: var(--font-mono, monospace);
    font-size: 11px;
    font-weight: 600;
  }
  .pane-num-badge {
    color: #ff9e64;
  }
  .pane-avatar {
    width: 16px;
    height: 16px;
    border-radius: 50%;
    background-size: cover;
    background-position: center;
    border: 1px solid rgba(255, 255, 255, 0.15);
  }
  .pane-name {
    color: #c0caf5;
  }
  .pane-leaf.active .pane-name {
    color: #7aa2f7;
  }
  .pane-backend-tag {
    font-size: 9px;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    padding: 1px 4px;
    background: rgba(255, 255, 255, 0.06);
    border-radius: 3px;
    color: #787c99;
  }
  .pane-controls {
    display: flex;
    align-items: center;
    gap: 4px;
  }
  .pane-btn {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 20px;
    height: 20px;
    background: transparent;
    border: 1px solid transparent;
    border-radius: 3px;
    color: #787c99;
    cursor: pointer;
    font-size: 10px;
    padding: 0;
  }
  .pane-btn:hover {
    background: rgba(255, 255, 255, 0.1);
    color: #c0caf5;
    border-color: rgba(255, 255, 255, 0.15);
  }
  .pane-btn.close:hover {
    color: #f7768e;
  }
  .pane-content {
    flex: 1 1 auto;
    display: flex;
    flex-direction: column;
    min-height: 0;
    overflow: hidden;
  }
</style>
