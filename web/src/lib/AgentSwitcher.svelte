<script>
  import HostBadge from './HostBadge.svelte';
  // Quick-switch popup: one tap on the identity block, active agents only.
  import {
    app, isUserNotificationUnread, refreshAgentSnapshot,
    avatarUrl, setSession, shortActivityPhase, statusFor,
  } from '../stores/app.svelte.js';
  import { movedLine } from '@core/agent-moved.js';
  import { resolveAvatarUrl } from '@core/avatar.js';
  import { PRIMARY, mergeChats } from '@core/all-hosts.js';
  import { listedHostIds, hostBadgeFor } from '../stores/app.svelte.js';
  import { hostsState, openOnHost, readHosts } from '../stores/hosts.svelte.js';
  import { sinceTime } from '@core/host-reach.js';
  import { i18n, t } from '../stores/i18n.svelte.js';
  import { PRIMARY as SERVING, downHosts, isDown, openPanel, reach } from '../stores/reach.svelte.js';

  let { open = $bindable() } = $props();

  $effect(() => {
    if (!open) return;
    refreshAgentSnapshot().catch(() => {});
  });

  let items = $derived.by(() => {
    app.tick;
    const own = Object.entries(app.agentsBySession)
      .filter(([sid, info]) => info && info.name && app.availableSessions.includes(sid))
      .map(([sid, info]) => ({
        sid, name: info.name, lastActivity: Number(statusFor(sid).last_activity) || 0,
        moved: statusFor(sid).moved || null,
      }));
    // The other Hosts read now join by the shared rules (@core/all-hosts.js).
    const others = readHosts();
    const groups = [{ hostId: PRIMARY, rows: own },
      ...others.map(h => ({ hostId: h.id, rows: hostsState.live[h.id]?.rows || [] }))];
    return mergeChats(groups, new Set(listedHostIds())).map(row => {
      const remote = row.hostId === PRIMARY ? null : others.find(h => h.id === row.hostId);
      const s = remote ? row.status : statusFor(row.sid);
      const isCurrent = !remote && row.sid === app.session;
      const isBusy = !!s.busy;
      const isUnread = !remote && isUserNotificationUnread(row.sid);
      return {
        key: row.key, sid: row.sid, name: row.name, remote,
        hostDown: isDown(remote ? remote.id : SERVING),
        avatar: remote ? resolveAvatarUrl({}, row.name) : avatarUrl(row.name, row.sid),
        host: remote ? hostBadgeFor(row.sid, remote.serverId || remote.id, s) : hostBadgeFor(row.sid),
        cls: isCurrent ? 'current' : isBusy ? 'busy' : isUnread ? 'unread' : '',
        text: s.moved ? movedLine(s.moved)
          : isCurrent ? 'current'
          : isBusy ? (shortActivityPhase(s) || 'working')
          : isUnread ? 'new' : 'idle',
      };
    });
  });

  // One line per Host that does not answer, above its dimmed rows; it opens the full panel.
  let down = $derived.by(() => {
    const others = readHosts();
    return downHosts([...(others.length ? [SERVING] : []), ...others.map(h => h.id)])
      .map(h => ({ id: h.id, text: t('host_down.row', { host: h.label, time: sinceTime(h.since, reach.now, t, i18n.locale) }) }));
  });

  function pick(item) {
    if (item.remote) openOnHost(item.remote, item.sid);
    else setSession(item.sid);
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
    {#each down as line (line.id)}
      <button class="switcher-host-down" onclick={() => { openPanel(line.id); open = false; }}>
        <span class="switcher-offline-dot" aria-hidden="true"></span>{line.text}
      </button>
    {/each}
    {#each items as item, i (item.key)}
      <button class="switcher-item" class:host-down={item.hostDown} style="animation-delay:{i * 35}ms" onclick={() => pick(item)}>
        <span class="switcher-avatar {item.cls}"
              style="background-image:url('{item.avatar}')">
          {#if !item.avatar}
            <span class="avatar-letter">{item.name.slice(0, 1)}</span>
          {/if}
        </span>
        <span class="switcher-name">{item.name}{#if item.host}<HostBadge name={item.host} />{/if}</span>
        <span class="switcher-state {item.cls}">{item.text}</span>
      </button>
    {/each}
  </div>
{/if}

<style>
.switcher-host-down {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 8px 12px;
  border: 0;
  border-bottom: 1px solid var(--ink-edge);
  background: transparent;
  color: var(--washi-dim);
  font: 400 12px/1.3 var(--font-ui);
  text-align: left;
  cursor: pointer;
}
.switcher-offline-dot {
  flex: 0 0 auto;
  width: 7px; height: 7px;
  border-radius: 50%;
  border: 1.5px solid var(--washi-low);
}
.agent-switcher :global(.switcher-item.host-down) { opacity: .55; }
</style>
