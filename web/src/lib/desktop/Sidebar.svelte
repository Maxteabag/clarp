<script>
  // Desktop conversation rail. Every active agent is a chat; clicking one
  // switches the transcript.
  //
  // The rail does not decide its own width. DesktopShell owns the resizable
  // layout and says whether the rail is collapsed; `forced` means the window
  // is too narrow to expand, so the toggle is disabled rather than hidden and
  // the control does not move.
  import {
    app, isUserNotificationUnread, setSession,
    shortActivityPhase, shortActivityText, statusFor,
  } from '../../stores/app.svelte.js';
  import { orderChats } from '../chat-order.js';
  import { input, setRegion } from '../../stores/input.svelte.js';
  import AgentAvatar from '../AgentAvatar.svelte';

  let { collapsed = false, forced = false, onToggle, onOpenOverview } = $props();

  // Which row the keyboard is on, or -1 when the rail does not have it. Read
  // at the top level so the tracking sees both fields.
  let selected = $derived(input.region === 'sidebar' ? input.chatIndex : -1);
  let listEl = $state(null);

  // Keep the keyboard selection on screen; a rail taller than the window would
  // otherwise move the highlight out of view.
  $effect(() => {
    if (selected < 0 || !listEl) return;
    listEl.children[selected]?.scrollIntoView({ block: 'nearest' });
  });

  let chats = $derived.by(() => {
    app.tick;
    // Ordering lives in chat-order.js so it can be tested without a live
    // roster — see tests/state/chat-order.test.js.
    return orderChats(app.agentsBySession, app.status, app.availableSessions)
      .map(row => {
        const s = statusFor(row.sid);
        return {
          ...row,
          current: row.sid === app.session,
          busy: !!s.busy,
          unread: isUserNotificationUnread(row.sid),
          line: s.busy
            ? (shortActivityText(s) || shortActivityPhase(s) || 'working')
            : isUserNotificationUnread(row.sid) ? 'new reply' : '',
        };
      });
  });
</script>

<aside class="sidebar" class:collapsed>
  <header class="sidebar-head">
    {#if !collapsed}<span class="sidebar-title">Clarp</span>{/if}
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

  <nav class="sidebar-list" aria-label="Conversations" bind:this={listEl}>
    {#each chats as chat, i (chat.sid)}
      <button
        class="side-row"
        class:current={chat.current}
        class:selected={i === selected}
        onpointerenter={() => { if (input.region === 'sidebar') setRegion('panes'); }}
        title={collapsed ? `${chat.name}${chat.line ? ` — ${chat.line}` : ''}` : (chat.line || chat.name)}
        onclick={() => setSession(chat.sid)}
      >
        <AgentAvatar class="side-avatar" name={chat.name} session={chat.sid}>
          <span class="side-dot" class:busy={chat.busy} class:unread={chat.unread}></span>
        </AgentAvatar>
        {#if !collapsed}
          <span class="side-copy">
            <span class="side-name">{chat.name}</span>
            {#if chat.line}<span class="side-line">{chat.line}</span>{/if}
          </span>
        {/if}
      </button>
    {/each}
  </nav>

  <footer class="sidebar-foot">
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
.sidebar .side-row.selected {
  outline: 1px solid var(--accent-blue);
  outline-offset: -1px;
  background: color-mix(in srgb, var(--accent-blue) 9%, transparent);
}
.sidebar .side-row.current {
  background: color-mix(in srgb, var(--accent-blue) 16%, transparent);
  color: var(--washi);
}

:global(.side-avatar) {
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
  :global(.side-avatar) {
    flex-basis: 30px;
    width: 30px;
    height: 30px;
    border-radius: 8px;
    font-size: 14px;
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

/* Desktop rail: compact roster, one signal per row, no repeated "idle" text. */
.sidebar {
  background: #171821;
  border-right-color: #303143;
}
.sidebar-head {
  min-height: 38px;
  padding: 7px 8px 6px 12px;
  border-bottom-color: #292b3a;
}
.sidebar-title {
  color: #8d90aa;
  font-size: 10px;
  letter-spacing: .15em;
}
.sidebar-toggle {
  width: 22px;
  height: 22px;
  border-color: transparent;
  border-radius: 3px;
  color: #5f6279;
}
.sidebar:not(:hover) .sidebar-toggle { opacity: .42; }
.sidebar-toggle:hover:not(:disabled) { background: #222431; border-color: #383a4e; }
.sidebar-list { gap: 1px; padding: 6px; }
.sidebar.collapsed .sidebar-list { padding: 6px 5px; }
.sidebar .side-row {
  position: relative;
  min-height: 37px;
  gap: 9px;
  padding: 5px 7px;
  border-radius: 4px;
  color: #8f92aa;
}
.sidebar.collapsed .side-row { padding: 4px; }
.sidebar .side-row:hover { background: #20212d; }
.sidebar .side-row.current {
  background: #242634;
  color: #d0d2e2;
}
.sidebar .side-row.current::before {
  content: '';
  position: absolute;
  left: 0;
  top: 9px;
  bottom: 9px;
  width: 2px;
  border-radius: 2px;
  background: #9da0ba;
}
.sidebar .side-row.selected {
  outline-color: #777b97;
  background: #222431;
}
:global(.side-avatar) {
  --avatar-size: 27px;
  --avatar-radius: 7px;
  flex-basis: 27px;
  width: 27px;
  height: 27px;
  border-radius: 7px;
}
.side-dot {
  right: -2px;
  bottom: -2px;
  width: 8px;
  height: 8px;
  box-shadow: 0 0 0 2px #171821;
}
.sidebar .side-row.current .side-dot { box-shadow: 0 0 0 2px #242634; }
.side-copy { gap: 1px; }
.side-name { color: inherit; font-size: 11px; letter-spacing: .015em; }
.side-line { color: #696c83; font-size: 9px; }
.side-dot.busy { background: #8dad79; }
.side-dot.unread { background: #c58a9a; }
.sidebar-foot { padding: 6px; border-top-color: #292b3a; }
.sidebar-all {
  min-height: 29px;
  padding: 6px;
  border-color: transparent;
  border-radius: 3px;
  color: #686b81;
  font-size: 10px;
}
.sidebar-all:hover { background: #20212d; border-color: #343649; color: #aeb1c7; }

:global(html[data-theme="day"]) .sidebar { background: var(--ink-soft); border-color: var(--ink-edge); }
:global(html[data-theme="day"]) .sidebar-head,
:global(html[data-theme="day"]) .sidebar-foot { border-color: var(--ink-edge); }
:global(html[data-theme="day"]) .sidebar-title,
:global(html[data-theme="day"]) .sidebar-toggle,
:global(html[data-theme="day"]) .side-line,
:global(html[data-theme="day"]) .sidebar-all { color: var(--washi-low); }
:global(html[data-theme="day"]) .sidebar .side-row { color: var(--washi-dim); }
:global(html[data-theme="day"]) .sidebar .side-row:hover { background: var(--ink-soft-2); }
:global(html[data-theme="day"]) .sidebar .side-row.current {
  color: var(--washi);
  background: color-mix(in srgb, var(--accent-blue) 13%, transparent);
}
:global(html[data-theme="day"]) .sidebar .side-row.current::before { background: var(--accent-blue); }
:global(html[data-theme="day"]) .side-dot { box-shadow: 0 0 0 2px var(--ink-soft); }
</style>
