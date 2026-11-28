<script>
  // Desktop conversation rail. Every active agent is a chat; clicking one
  // switches the transcript.
  //
  // The rail does not decide its own width. DesktopShell owns the resizable
  // layout and says whether the rail is collapsed; `forced` means the window
  // is too narrow to expand, so the toggle is disabled rather than hidden and
  // the control does not move.
  import {
    AVATAR_PALETTE, app, avatarUrl, hostBadgeFor, isUserNotificationUnread, listedHostIds, setSession,
    shortActivityPhase, shortActivityText, statusFor,
  } from '../../stores/app.svelte.js';
  import { orderChats } from '../chat-order.js';
  import { movedLine } from '@core/agent-moved.js';
  import { resolveAvatarUrl } from '@core/avatar.js';
  import { mergeChats, PRIMARY } from '@core/all-hosts.js';
  import { COPY, sinceTime } from '@core/host-reach.js';
  import { hostsState, openOnHost, readHosts } from '../../stores/hosts.svelte.js';
  import { PRIMARY as SERVING, downHosts, isDown, openPanel, reach } from '../../stores/reach.svelte.js';
  import HostBadge from '../HostBadge.svelte';
  import { input, orderedSessions, setRegion } from '../../stores/input.svelte.js';

  let { collapsed = false, forced = false, onToggle, onOpenOverview } = $props();

  // Which row the keyboard is on, or -1 when the rail does not have it. Read
  // at the top level so the tracking sees both fields.
  // The keyboard walks this Host's chats (stores/input.svelte.js), so the highlight follows the
  // session it is on, wherever the merged list puts that row.
  let selectedSid = $derived((app.tick, input.region === 'sidebar' ? orderedSessions()[input.chatIndex] || '' : ''));
  let selected = $derived(selectedSid ? chats.findIndex(c => !c.remote && c.sid === selectedSid) : -1);
  let listEl = $state(null);

  // Keep the keyboard selection on screen; a rail taller than the window would
  // otherwise move the highlight out of view.
  $effect(() => {
    if (selected < 0 || !listEl) return;
    listEl.children[selected]?.scrollIntoView({ block: 'nearest' });
  });

  // The other Hosts read now (the all-Hosts list, stores/hosts.svelte.js), in saved order.
  let others = $derived(readHosts());

  let chats = $derived.by(() => {
    app.tick;
    // Ordering lives in chat-order.js so it can be tested without a live
    // roster — see tests/state/chat-order.test.js. With other Hosts read, their rows join
    // by the rules every client shares (@core/all-hosts.js).
    const own = orderChats(app.agentsBySession, app.status, app.availableSessions)
      .map(row => ({ ...row, moved: statusFor(row.sid).moved || null }));
    const groups = [{ hostId: PRIMARY, rows: own },
      ...others.map(h => ({ hostId: h.id, rows: hostsState.live[h.id]?.rows || [] }))];
    return mergeChats(groups, new Set(listedHostIds())).map(row => {
      const remote = row.hostId === PRIMARY ? null : others.find(h => h.id === row.hostId);
      const s = remote ? row.status : statusFor(row.sid);
      return {
        ...row,
        remote,
        current: !remote && row.sid === app.session,
        busy: !!s.busy,
        host: remote ? hostBadgeFor(row.sid, remote.serverId || remote.id, s) : hostBadgeFor(row.sid),
        unread: !remote && isUserNotificationUnread(row.sid),
        // Its Host does not answer: the row stays, dimmed, with an offline dot.
        hostDown: isDown(remote ? remote.id : SERVING),
        // Another Host's portraits need that Host's sign-in; the bundled ones are this app's.
        avatar: remote ? resolveAvatarUrl({}, row.name) : avatarUrl(row.name, row.sid),
        // A moved agent's row says where it went (contract 125).
        line: movedLine(s.moved) || shortActivityText(s) || shortActivityPhase(s)
              || (s.busy ? 'Working' : 'Idle'),
      };
    });
  });

  // One line per Host that does not answer, opening its full panel (HostDownDialog). The serving
  // Host's own line only while other Hosts are listed: alone, its chat shows the panel itself.
  let down = $derived(downHosts([...(others.length ? [SERVING] : []), ...others.map(h => h.id)])
    .map(h => ({ id: h.id, text: COPY.row(h.label, sinceTime(h.since, reach.now)) })));

  function open(chat) {
    if (chat.remote) openOnHost(chat.remote, chat.sid);
    else setSession(chat.sid);
  }
</script>

<aside class="sidebar" class:collapsed>
  <header class="sidebar-head">
    {#if !collapsed}<span class="sidebar-title">Chats</span>{/if}
    <button
      class="sidebar-toggle"
      aria-label={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
      title={forced ? 'Window is too narrow to expand' : (collapsed ? 'Expand' : 'Collapse')}
      disabled={forced}
      onclick={onToggle}
    >
      <svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true">
        <path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"
              stroke-linejoin="round"
              d={collapsed ? 'M9 6l6 6-6 6' : 'M15 6l-6 6 6 6'} />
      </svg>
    </button>
  </header>

  {#if !collapsed}
    {#each down as line (line.id)}
      <button type="button" class="side-unreachable" onclick={() => openPanel(line.id)}>
        <span class="side-offline-dot" aria-hidden="true"></span>{line.text}
      </button>
    {/each}
  {/if}
  <nav class="sidebar-list" aria-label="Conversations" bind:this={listEl}>
    {#each chats as chat, i (chat.key)}
      <button
        class="side-row"
        class:current={chat.current}
        class:selected={i === selected}
        onpointerenter={() => { if (input.region === 'sidebar') setRegion('panes'); }}
        class:remote={!!chat.remote}
        class:host-down={chat.hostDown}
        title={(collapsed ? `${chat.name} — ${chat.line}` : chat.line)
               + (chat.remote ? ` · opens on ${chat.remote.name || chat.remote.url}` : '')}
        onclick={() => open(chat)}
      >
        <span
          class="side-avatar"
          style="background-color:{AVATAR_PALETTE[chat.name] || 'var(--ochre)'};background-image:url('{chat.avatar}')"
        >
          {#if !chat.avatar}
            <span class="avatar-letter">{chat.name.slice(0, 1)}</span>
          {/if}
          <span class="side-dot" class:busy={chat.busy && !chat.hostDown} class:unread={chat.unread}
                class:offline={chat.hostDown}></span>
        </span>
        {#if !collapsed}
          <span class="side-copy">
            <span class="side-name">{chat.name}</span>
            <span class="side-line"><HostBadge name={chat.host} />{chat.line}</span>
          </span>
        {/if}
      </button>
    {/each}
  </nav>

  <footer class="sidebar-foot">
    <a href="/viz" class="sidebar-all" title="Fleet map">◌ {#if !collapsed}<span>Fleet map</span>{/if}</a>
    <button class="sidebar-all" onclick={onOpenOverview}
            title="All agents — start, stop, relaunch">
      <svg viewBox="0 0 24 24" width="15" height="15" aria-hidden="true">
        <path fill="currentColor" d="M4 5h16v2H4zm0 6h16v2H4zm0 6h16v2H4z"/>
      </svg>
      {#if !collapsed}<span>All agents</span>{/if}
    </button>
  </footer>
</aside>

<style>
/* The rail fills whatever pane DesktopShell gives it. Its width is not
   decided here — see the pixel bounds in DesktopShell.svelte. The
   container-type lets the rows scale with the pane, not the window. */
.sidebar {
  width: 100%;
  height: 100%;
  container-type: inline-size;
  display: flex;
  flex-direction: column;
  min-width: 0;
  background: var(--ink-soft);
  border-right: 1px solid var(--ink-edge);
}

.sidebar-head {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: calc(env(safe-area-inset-top) + 12px) 10px 10px 14px;
  border-bottom: 1px solid var(--ink-edge);
}
.sidebar.collapsed .sidebar-head { justify-content: center; padding-inline: 0; }
.sidebar-title {
  font: 600 10px/1 var(--font-ui);
  letter-spacing: .18em;
  text-transform: uppercase;
  color: var(--washi-low);
  white-space: nowrap;
}
.sidebar-toggle {
  flex: 0 0 auto;
  display: grid;
  place-items: center;
  width: 26px; height: 26px;
  border: 1px solid var(--ink-edge);
  border-radius: 7px;
  background: transparent;
  color: var(--washi-low);
  cursor: pointer;
  transition: color var(--t-snap) var(--ease), border-color var(--t-snap) var(--ease);
}
.sidebar-toggle:hover:not(:disabled) { color: var(--washi); border-color: var(--ink-edge-hi); }
.sidebar-toggle:disabled { opacity: .35; cursor: default; }

.sidebar-list {
  flex: 1 1 auto;
  overflow-y: auto;
  overflow-x: hidden;
  padding: 8px;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.sidebar.collapsed .sidebar-list { padding: 8px 6px; align-items: center; }

.sidebar .side-row {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  padding: 7px 9px;
  border: 0;
  border-radius: 9px;
  background: transparent;
  color: var(--washi-dim);
  font: inherit;
  text-align: left;
  cursor: pointer;
  transition: background var(--t-snap) var(--ease), color var(--t-snap) var(--ease);
}
.sidebar.collapsed .side-row { width: auto; padding: 6px; }
.sidebar .side-row:hover { background: var(--ink-soft-2); color: var(--washi); }
/* Where the keyboard is in the rail. Distinct from .current, which is the
   conversation actually open: you walk the list before choosing. */
.side-unreachable {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 7px 14px;
  border: 0;
  border-bottom: 1px solid var(--ink-edge);
  background: transparent;
  font: 400 11px/1.3 var(--font-ui);
  color: var(--washi-dim);
  text-align: left;
  cursor: pointer;
}
.side-unreachable:hover { color: var(--washi); background: var(--ink-soft-2); }
.side-offline-dot {
  flex: 0 0 auto;
  width: 7px; height: 7px;
  border-radius: 50%;
  border: 1.5px solid var(--washi-low);
}
.sidebar .side-row.host-down { opacity: .55; }
.side-dot.offline { background: var(--ink-soft); box-shadow: 0 0 0 2px var(--ink-soft), inset 0 0 0 2px var(--washi-low); }
.sidebar .side-row.selected {
  outline: 1px solid var(--accent-blue);
  outline-offset: -1px;
  background: color-mix(in srgb, var(--accent-blue) 9%, transparent);
}
.sidebar .side-row.current {
  background: color-mix(in srgb, var(--accent-blue) 16%, transparent);
  color: var(--washi);
}

.side-avatar {
  position: relative;
  flex: 0 0 30px;
  width: 30px;
  height: 30px;
  border-radius: 9px;
  background-size: cover;
  background-position: center;
  background-color: var(--ink-soft-2);
  box-shadow: inset 0 0 0 1px rgba(192, 202, 245, .14);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  color: var(--ink);
  font-size: 18px;
}
/* Status dot. Absent = idle; the dot only appears when there is something
   to say, so a quiet roster stays quiet. */
.side-dot {
  position: absolute;
  right: -3px; bottom: -3px;
  width: 10px; height: 10px;
  border-radius: 50%;
  background: transparent;
  box-shadow: 0 0 0 2px var(--ink-soft);
}
.sidebar .side-row.current .side-dot { box-shadow: 0 0 0 2px var(--ink-soft-2); }
.side-dot.busy   { background: var(--moss); }
.side-dot.unread { background: var(--vermillion); }
/* Room to breathe: on wide windows the rail claims up to 320px, and the
   avatars grow into it instead of leaving the extra width empty. */
@container (min-width: 300px) {
  .side-avatar {
    flex-basis: 56px;
    width: 56px;
    height: 56px;
    border-radius: 16px;
    font-size: 32px;
  }
  .side-dot { width: 13px; height: 13px; }
  .side-name { font-size: 15px; }
  .side-line { font-size: 12px; }
}

.side-copy {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}
.side-name {
  font: 500 13px/1.2 var(--font-ui);
  color: inherit;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.side-line {
  font: 400 11px/1.2 var(--font-ui);
  color: var(--washi-low);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.sidebar-foot {
  flex: 0 0 auto;
  padding: 8px;
  border-top: 1px solid var(--ink-edge);
}
.sidebar-all {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  width: 100%;
  padding: 8px;
  border: 1px solid var(--ink-edge);
  border-radius: 9px;
  background: transparent;
  color: var(--washi-low);
  font: 500 12px/1 var(--font-ui);
  cursor: pointer;
  transition: color var(--t-snap) var(--ease), border-color var(--t-snap) var(--ease);
}
.sidebar-all:hover { color: var(--washi); border-color: var(--ink-edge-hi); }
</style>
