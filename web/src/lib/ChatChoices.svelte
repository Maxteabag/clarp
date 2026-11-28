<script>
  // A chat's own choices in its profile (#653), the same two its '…' menu holds.
  import { CHAT_TOOL_ACTIVITY, toolActivityKey } from '@core/history-fold.js';
  import { OVERRIDE_KEYS, OVERRIDES } from '@core/coordination.js';
  import { t } from '../stores/i18n.svelte.js';
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

  async function pickCoordination(select) {
    busy = true;
    const answer = await setChatCoordinationChoice(session, select.value);
    busy = false;
    // Not saved: the select goes back to the chat's choice (the derived value did not move).
    if (!answer) select.value = chatCoordinationChoice(session);
  }
</script>

<div class="chat-choices" aria-label={t('chat_choices.label')}>
  {#if live.hiddenCoordination}
    <label>
      <span>{t('chat_choices.coordination')}</span>
      <select value={coordination} disabled={busy} onchange={e => pickCoordination(e.currentTarget)}>
        {#each OVERRIDES as value (value)}<option {value}>{t(OVERRIDE_KEYS[value])}</option>{/each}
      </select>
    </label>
  {/if}
  <label>
    <span>{t('chat_choices.tool_activity')}</span>
    <select value={tools} disabled={!chatChoicesReady(session)}
            onchange={e => setChatToolActivity(session, e.currentTarget.value)}>
      {#each CHAT_TOOL_ACTIVITY as value (value)}<option {value}>{t(toolActivityKey(value))}</option>{/each}
    </select>
  </label>
</div>
