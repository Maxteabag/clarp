<script>
  // The transcript header's identity block: avatar, name, activity line, and
  // the unread badge. Tap opens the quick switcher, a 600ms hold opens the
  // full overview — the same gesture the dock's session button carries, kept
  // on both so desktop still has it after the dock drops its identity block.
  import {
    app, chipLabel, shortActivityText, statusFor,
    unreadAgentCount,
  } from '../stores/app.svelte.js';
  import { activityStatusClass } from './render.js';
  import AgentAvatar from './AgentAvatar.svelte';

  let { session = app.session, quietIdle = false, onTap, onHold } = $props();

  let label = $derived(chipLabel(session));
  let status = $derived(statusFor(session));
  let activity = $derived(
    shortActivityText(status) || (status.busy ? 'Working' : 'Idle'));
  let showActivity = $derived(
    !quietIdle || status.busy || !['', 'idle', 'done'].includes(activity.trim().toLowerCase()));
  let unread = $derived((app.tick, unreadAgentCount()));

  let holdTimer = null;
  let holdFired = false;

  function pointerdown() {
    holdFired = false;
    clearTimeout(holdTimer);
    holdTimer = setTimeout(() => { holdFired = true; onHold(); }, 600);
  }
  function pointerup(e) {
    clearTimeout(holdTimer);
    if (holdFired) { holdFired = false; return; }
    onTap();
    e.preventDefault();
  }
  function pointercancel() {
    clearTimeout(holdTimer);
    holdFired = false;
  }
</script>

<button
  id="historyAgent"
  class="history-agent"
  type="button"
  onpointerdown={pointerdown}
  onpointerup={pointerup}
  onpointercancel={pointercancel}
  onclick={e => { e.preventDefault(); e.stopPropagation(); }}
>
  <AgentAvatar class="history-agent-avatar" name={label} {session}>
    {#if unread > 0}
      <span class="history-unread-badge">{unread > 9 ? '9+' : unread}</span>
    {/if}
  </AgentAvatar>
  <span class="history-agent-copy">
    <span class="history-agent-name">{label}</span>
    {#if showActivity}
      <span
        class="history-agent-status {activityStatusClass(status.activity_status)}"
        title={activity}
      >{activity}</span>
    {/if}
  </span>
</button>
