<script>
  // The transcript pane: header, banner, turns. Identical on both platforms,
  // so it stays shared; the shells differ in what surrounds it, not in this.
  import AgentBanner from './AgentBanner.svelte';
  import AgentIdentity from './AgentIdentity.svelte';
  import ConnDot from './ConnDot.svelte';
  import Transcript from './Transcript.svelte';
  import StatusLine from './live/StatusLine.svelte';
  import { reload } from '../stores/conversations.svelte.js';
  import { live, setExplanationsEnabled } from '../stores/live.svelte.js';
  import { prefs, toggleTools } from '../stores/prefs.svelte.js';

  // `active` is false for a chat kept mounted behind the open one: it keeps
  // its DOM and scroll position but is hidden and gives up the element ids.
  let { session, showConnDot = false, onTapAgent, onHoldAgent, active = true } = $props();
</script>

<section id={active ? 'history' : undefined} class="history" class:hidden={!active}
         aria-label="Conversation" aria-hidden={active ? undefined : 'true'}>
  <header class="history-head">
    <AgentIdentity {session} onTap={onTapAgent} onHold={onHoldAgent} />
    {#if showConnDot}<ConnDot />{/if}
    <div class="history-actions">
      <button
        id={active ? 'historyToolsToggle' : undefined}
        class="history-btn"
        class:active={!prefs.hideTools}
        aria-label="Toggle tools"
        title="Show/hide tool calls"
        onclick={toggleTools}
      >⚙</button>
      {#if live.explanationSetting}
        <!-- Host setting (docs/live-items.md §6): tool rows show a plain-language
             explanation under the command, or the raw command when off. -->
        <button
          id={active ? 'historyExplainToggle' : undefined}
          class="history-btn"
          class:active={live.explanations.enabled}
          aria-pressed={live.explanations.enabled}
          aria-label="Explain tool calls"
          title={live.explanations.enabled ? 'Tool explanations on (Host setting)' : 'Tool explanations off: showing raw commands'}
          onclick={() => setExplanationsEnabled(!live.explanations.enabled)}
        >ⓘ</button>
      {/if}
      <button id={active ? 'historyRefresh' : undefined} class="history-btn" aria-label="Refresh"
              onclick={() => reload(session)}>↻</button>
    </div>
  </header>
  <AgentBanner {session} />
  <Transcript {session} {active} />
  <StatusLine {session} />
</section>
