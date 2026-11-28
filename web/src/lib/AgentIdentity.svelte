<script>
  // The transcript header's identity block: avatar, name, activity line, and
  // the unread badge. Tap opens the quick switcher, a 600ms hold opens the
  // full overview — the same gesture the dock's session button carries, kept
  // on both so desktop still has it after the dock drops its identity block.
  import {
    AVATAR_PALETTE, app, avatarUrl, chipLabel, hostBadgeFor, shortActivityText, statusFor,
    unreadAgentCount,
  } from '../stores/app.svelte.js';
  import { activityStatusClass } from './render.js';
  import { movedLine } from '@core/agent-moved.js';
  import { PRIMARY, isDown } from '../stores/reach.svelte.js';
  import HostBadge from './HostBadge.svelte';

  let { session = app.session, onTap, onHold } = $props();

  let label = $derived(chipLabel(session));
  let status = $derived(statusFor(session));
  // While the serving Host does not answer nothing is known of what the agent does now: no word.
  let activity = $derived(isDown(PRIMARY) ? (movedLine(status.moved) || '') :
    movedLine(status.moved) || shortActivityText(status) || (status.busy ? 'Working' : 'Idle'));
  let unread = $derived((app.tick, unreadAgentCount()));
  let host = $derived((app.tick, hostBadgeFor(session)));

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
  <span
    class="history-agent-avatar"
    aria-hidden="true"
    style="background-color:{AVATAR_PALETTE[label] || 'var(--ochre)'};background-image:url('{avatarUrl(label, session)}')"
  >
    {#if !avatarUrl(label, session)}
      <span class="avatar-letter">{label.slice(0, 1)}</span>
    {/if}
    {#if unread > 0}
      <span class="history-unread-badge">{unread > 9 ? '9+' : unread}</span>
    {/if}
  </span>
  <span class="history-agent-copy">
    <span class="history-agent-name">{label}</span>
    <span
      class="history-agent-status {activityStatusClass(status.activity_status)}"
      title={activity}
    >{#if host}<HostBadge name={host} />{#if activity} · {/if}{/if}{activity}</span>
  </span>
</button>
