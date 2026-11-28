<script>
  // The transcript pane: header, banner, turns. Identical on both platforms,
  // so it stays shared; the shells differ in what surrounds it, not in this.
  import AgentBanner from './AgentBanner.svelte';
  import AgentIdentity from './AgentIdentity.svelte';
  import ChatChoicesMenu from './ChatChoicesMenu.svelte';
  import ConnDot from './ConnDot.svelte';
  import Transcript from './Transcript.svelte';
  import StatusLine from './live/StatusLine.svelte';
  import HostDownPanel from './HostDownPanel.svelte';
  import { conversation, reload } from '../stores/conversations.svelte.js';
  import { PRIMARY, isDown, panelShown, unheard } from '../stores/reach.svelte.js';
  import { live, setExplanationsEnabled } from '../stores/live.svelte.js';
  import { toggleToolsFor, toolActivityFor } from '../stores/chat-choices.svelte.js';

  // `active` is false for a chat kept mounted behind the open one: it keeps
  // its DOM and scroll position but is hidden and gives up the element ids.
  let { session, showConnDot = false, onTapAgent, onHoldAgent, active = true } = $props();

  // Every chat here is the serving Host's. While it does not answer, the panel sits above the
  // saved messages; with nothing saved, the full page stands in for the transcript.
  let hostPanel = $derived(panelShown(PRIMARY));
  let nothingSaved = $derived(isDown(PRIMARY) && !conversation(session).turns.length);
</script>

<section id={active ? 'history' : undefined} class="history" class:hidden={!active} class:host-silent={unheard(PRIMARY)}
         aria-label="Conversation" aria-hidden={active ? undefined : 'true'}>
  <header class="history-head">
    <AgentIdentity {session} onTap={onTapAgent} onHold={onHoldAgent} />
    {#if showConnDot}<ConnDot />{/if}
    <div class="history-actions">
      <button
        id={active ? 'historyToolsToggle' : undefined}
        class="history-btn"
        class:active={toolActivityFor(session) === 'always'}
        aria-label="Toggle tools"
        title={toolActivityFor(session) === 'always' ? 'Every tool call in view (Tool activity in Chat settings or the chat\'s … menu)'
          : 'Tool calls grouped (Tool activity in Chat settings or the chat\'s … menu)'}
        onclick={() => toggleToolsFor(session)}
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
      <ChatChoicesMenu {session} {active} />
      <button id={active ? 'historyRefresh' : undefined} class="history-btn" aria-label="Refresh"
              onclick={() => reload(session)}>↻</button>
    </div>
  </header>
  {#if hostPanel && !nothingSaved}<HostDownPanel id={PRIMARY} />{/if}
  <AgentBanner {session} />
  {#if nothingSaved}
    <div class="host-down-page"><HostDownPanel id={PRIMARY} full /></div>
  {:else}
    <Transcript {session} {active} />
  {/if}
  <StatusLine {session} />
</section>

<style>
.host-down-page { flex: 1 1 auto; min-height: 0; overflow-y: auto; display: flex; }
/* Its Host does not answer: nothing pulses as if it were still working. */
.host-silent :global(.lv-dot), .host-silent :global(.lv-label), .host-silent :global(.tool-status),
.host-silent :global(.activity-dot), .host-silent :global(.banner-spinner) { animation: none !important; }
</style>
