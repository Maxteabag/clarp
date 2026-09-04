<script>
  // Quick-switch popup: one tap on the identity block, active agents only.
  import {
    app, isUserNotificationUnread, refreshAgentSnapshot,
    setSession, shortActivityPhase, statusFor,
  } from '../stores/app.svelte.js';
  import AgentAvatar from './AgentAvatar.svelte';

  let { open = $bindable() } = $props();

  $effect(() => {
    if (!open) return;
    refreshAgentSnapshot().catch(() => {});
  });

  let items = $derived.by(() => {
    app.tick;
    return Object.entries(app.agentsBySession)
      .filter(([sid, info]) => info && info.name && app.availableSessions.includes(sid))
      .map(([sid, info]) => {
        const s = statusFor(sid);
        const isCurrent = sid === app.session;
        const isBusy = !!s.busy;
        const isUnread = isUserNotificationUnread(sid);
        return {
          sid,
          name: info.name,
          cls: isCurrent ? 'current' : isBusy ? 'busy' : isUnread ? 'unread' : '',
          text: isCurrent ? 'current'
            : isBusy ? (shortActivityPhase(s) || 'working')
            : isUnread ? 'new' : 'idle',
        };
      });
  });

  function pick(sid) {
    setSession(sid);
    open = false;
  }

  // Outside-click and Escape close it. Capture phase so a click that also
  // lands on a button below still dismisses.
  function windowPointerDown(e) {
    if (!open) return;
    if (e.target.closest('.agent-switcher') || e.target.closest('#session')
        || e.target.closest('#historyAgent')) return;
    open = false;
  }
</script>

<svelte:window
  onpointerdowncapture={windowPointerDown}
  onkeydown={e => { if (e.key === 'Escape') open = false; }}
/>

{#if open}
  <div class="agent-switcher" role="menu" aria-label="Quick switch agent">
    {#each items as item, i (item.sid)}
      <button class="switcher-item" style="animation-delay:{i * 35}ms" onclick={() => pick(item.sid)}>
        <AgentAvatar class="switcher-avatar {item.cls}" name={item.name} session={item.sid} />
        <span class="switcher-name">{item.name}</span>
        <span class="switcher-state {item.cls}">{item.text}</span>
      </button>
    {/each}
  </div>
{/if}
