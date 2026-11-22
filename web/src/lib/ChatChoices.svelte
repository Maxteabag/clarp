<script>
  // A chat's own choices in its profile (#653), the same two its '…' menu holds.
  import { CHAT_TOOL_ACTIVITY, CHAT_TOOL_ACTIVITY_LABELS } from '@core/history-fold.js';
  import { OVERRIDE_LABELS, OVERRIDES } from '@core/coordination.js';
  import {
    chatChoicesReady, chatCoordinationChoice, chatToolActivity, setChatCoordinationChoice,
    setChatToolActivity,
  } from '../stores/chat-choices.svelte.js';
  import { live } from '../stores/live.svelte.js';
  import { app } from '../stores/app.svelte.js';

  let { session } = $props();

  let busy = $state(false);
  let coordination = $derived((app.tick, chatCoordinationChoice(session)));
  let tools = $derived(chatToolActivity(session));

  async function pickCoordination(choice) {
    busy = true;
    await setChatCoordinationChoice(session, choice);
    busy = false;
  }
</script>

<div class="chat-choices" aria-label="This chat's settings">
  {#if live.hiddenCoordination}
    <label>
      <span>Messages between agents</span>
      <select value={coordination} disabled={busy} onchange={e => pickCoordination(e.currentTarget.value)}>
        {#each OVERRIDES as value (value)}<option {value}>{OVERRIDE_LABELS[value]}</option>{/each}
      </select>
    </label>
  {/if}
  <label>
    <span>Tool activity</span>
    <select value={tools} disabled={!chatChoicesReady(session)}
            onchange={e => setChatToolActivity(session, e.currentTarget.value)}>
      {#each CHAT_TOOL_ACTIVITY as value (value)}<option {value}>{CHAT_TOOL_ACTIVITY_LABELS[value]}</option>{/each}
    </select>
  </label>
</div>
