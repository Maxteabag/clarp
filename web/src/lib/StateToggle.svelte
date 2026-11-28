<script>
  // The state inspector's button, in the layout where it is placed (the
  // sidebar's foot, the phone dock) so it never covers a header or composer
  // control. Ctrl+Shift+D opens the same panel (StateInspector).
  import { app } from '../stores/app.svelte.js';
  import { inspector, toggleInspector } from '../stores/inspector.svelte.js';

  // compact: the dot alone, for a collapsed rail or the phone dock.
  let { compact = false } = $props();
</script>

<button
  class="state-inspector-toggle"
  class:active={inspector.open}
  class:compact
  title="Toggle Svelte State Inspector (Ctrl+Shift+D)"
  aria-label="Toggle Svelte State Inspector"
  aria-pressed={inspector.open}
  onclick={toggleInspector}
>
  <span class="inspector-pulse" class:live={app.conn === 'live'}></span>
  {#if !compact}<span class="inspector-label">STATE</span>{/if}
</button>

<style>
  .state-inspector-toggle {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 6px;
    padding: 3px 8px;
    background: rgba(22, 22, 30, 0.85);
    border: 1px solid rgba(255, 255, 255, 0.15);
    border-radius: 4px;
    color: #a9b1d6;
    font-family: monospace;
    font-size: 11px;
    font-weight: 600;
    cursor: pointer;
    transition: all 0.15s ease;
  }
  .state-inspector-toggle.compact {
    padding: 0;
    width: 22px;
    height: 22px;
    flex: 0 0 auto;
    position: relative;
  }
  /* A finger-sized target around the small dot (44 px), so a near miss on the
     phone dock does not land on the button beside it. */
  .state-inspector-toggle.compact::after {
    content: '';
    position: absolute;
    inset: -11px;
  }
  .state-inspector-toggle:hover, .state-inspector-toggle.active {
    background: #24283b;
    border-color: #7aa2f7;
    color: #c0caf5;
  }
  .inspector-pulse {
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: #f7768e;
  }
  .inspector-pulse.live {
    background: #9ece6a;
    box-shadow: 0 0 6px rgba(158, 206, 106, 0.6);
  }
</style>
