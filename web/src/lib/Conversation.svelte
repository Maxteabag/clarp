<script>
  // The transcript pane: header, banner, turns. Identical on both platforms,
  // so it stays shared; the shells differ in what surrounds it, not in this.
  import AgentBanner from './AgentBanner.svelte';
  import AgentIdentity from './AgentIdentity.svelte';
  import ConnDot from './ConnDot.svelte';
  import Transcript from './Transcript.svelte';
  import { reload } from '../stores/conversations.svelte.js';
  import { prefs, toggleTools } from '../stores/prefs.svelte.js';

  let {
    session, showConnDot = false, showActions = true, quietIdentity = false,
    onTapAgent, onHoldAgent,
  } = $props();
</script>

<section id="history" class="history" aria-label="Conversation">
  <header class="history-head">
    <AgentIdentity {session} quietIdle={quietIdentity} onTap={onTapAgent} onHold={onHoldAgent} />
    {#if showConnDot}<ConnDot />{/if}
    {#if showActions}
      <div class="history-actions">
        <button
          id="historyToolsToggle"
          class="history-btn"
          class:active={!prefs.hideTools}
          aria-label="Toggle tools"
          title="Show/hide tool calls"
          onclick={toggleTools}
        >⚙</button>
        <button id="historyRefresh" class="history-btn" aria-label="Refresh"
                onclick={() => reload(session)}>↻</button>
      </div>
    {/if}
  </header>
  <AgentBanner {session} />
  <Transcript {session} />
</section>
