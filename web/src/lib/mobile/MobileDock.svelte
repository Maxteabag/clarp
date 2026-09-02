<script>
  // Phone dock: identity block, connection dot, and a chat button that
  // toggles the composer overlay.
  import {
    AVATAR_PALETTE, app, avatarUrl, chipLabel, shortActivityText, statusFor,
    unreadAgentCount,
  } from '../../stores/app.svelte.js';
  import { activityStatusClass } from '../render.js';
  import ConnDot from '../ConnDot.svelte';
  import DockControls from '../DockControls.svelte';

  let { chatOpen = $bindable(), onTapAgent, onHoldAgent } = $props();

  let label = $derived(chipLabel(app.session));
  let status = $derived(statusFor(app.session));
  let activity = $derived(
    shortActivityText(status) || (status.busy ? 'Working' : 'Idle'));
  let unread = $derived((app.tick, unreadAgentCount()));

  // Tap opens the quick switcher, a 600ms hold opens the full overview.
  let holdTimer = null;
  let holdFired = false;

  function down() {
    holdFired = false;
    clearTimeout(holdTimer);
    holdTimer = setTimeout(() => { holdFired = true; onHoldAgent(); }, 600);
  }
  function up(e) {
    clearTimeout(holdTimer);
    if (holdFired) { holdFired = false; return; }
    onTapAgent();
    e.preventDefault();
  }
</script>

<nav class="dock" aria-label="Controls">
  <button
    id="session"
    class="session"
    aria-label="Agent"
    onpointerdown={down}
    onpointerup={up}
    onpointercancel={() => { clearTimeout(holdTimer); holdFired = false; }}
    onclick={e => { e.preventDefault(); e.stopPropagation(); }}
  >
    <span
      class="avatar"
      aria-hidden="true"
      style="background-color:{AVATAR_PALETTE[label] || 'var(--ochre)'};background-image:url('{avatarUrl(label, app.session)}')"
    >
      {#if !avatarUrl(label, app.session)}
        <span class="avatar-letter">{label.slice(0, 1)}</span>
      {/if}
      {#if unread > 0}
        <span class="dock-badge" aria-hidden="true">{unread > 9 ? '9+' : unread}</span>
      {/if}
    </span>
    <span class="agent-text">
      <span class="agent-label">{label}</span>
      <span class="agent-activity-label {activityStatusClass(status.activity_status)}" title={activity}>
        {activity}
      </span>
    </span>
    <ConnDot />
  </button>

  <span class="dock-spacer"></span>

  <button id="chatBtn" class="chat-btn" aria-label="Type message"
          onclick={() => chatOpen = !chatOpen}>
    <svg class="btn-icon" viewBox="0 0 24 24" width="20" height="20" aria-hidden="true" focusable="false">
      <path fill="currentColor" d="M4 4h16a1 1 0 0 1 1 1v10a1 1 0 0 1-1 1H9l-5 4V5a1 1 0 0 1 1-1z"/>
    </svg>
  </button>

  <DockControls />
</nav>
