<script>
  // Desktop conversation rail. Every active agent is a chat; clicking one
  // switches the transcript.
  //
  // Two things decide the width, and they are not the same thing. `isDesktop`
  // is a property of the machine (set by the OS probe in index.html) and says
  // whether this rail exists at all. Collapsing is a property of the *window*
  // — a laptop with a half-width window has a pointer and a keyboard but no
  // room for a 264px rail — so it is a media query, and the user's own toggle
  // only applies while there is room to honour it.
  import {
    AVATAR_PALETTE, app, avatarUrl, isUserNotificationUnread, setSession,
    shortActivityPhase, shortActivityText, statusFor,
  } from '../../stores/app.svelte.js';
  import { orderChats } from '../chat-order.js';
  import { input, setRegion } from '../../stores/input.svelte.js';

  let { onOpenOverview } = $props();

  const NARROW = '(max-width: 1080px)';

  let userCollapsed = $state(localStorage.getItem('sidebarCollapsed') === '1');
  let forced = $state(window.matchMedia(NARROW).matches);

  $effect(() => {
    const mq = window.matchMedia(NARROW);
    const sync = () => { forced = mq.matches; };
    mq.addEventListener('change', sync);
    return () => mq.removeEventListener('change', sync);
  });

  let collapsed = $derived(forced || userCollapsed);

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

  function toggle() {
    userCollapsed = !collapsed;
    localStorage.setItem('sidebarCollapsed', userCollapsed ? '1' : '0');
  }

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
          line: shortActivityText(s) || shortActivityPhase(s)
                || (s.busy ? 'Working' : 'Idle'),
        };
      });
  });
</script>

<aside class="sidebar" class:collapsed>
  <header class="sidebar-head">
    {#if !collapsed}<span class="sidebar-title">Chats</span>{/if}
    <button
      class="sidebar-toggle"
      aria-label={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
      title={forced ? 'Window is too narrow to expand' : (collapsed ? 'Expand' : 'Collapse')}
      disabled={forced}
      onclick={toggle}
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
        title={collapsed ? `${chat.name} — ${chat.line}` : chat.line}
        onclick={() => setSession(chat.sid)}
      >
        <span
          class="side-avatar"
          style="background-color:{AVATAR_PALETTE[chat.name] || 'var(--ochre)'};background-image:url('{avatarUrl(chat.name, chat.sid)}')"
        >
          {#if !avatarUrl(chat.name, chat.sid)}
            <span class="avatar-letter">{chat.name.slice(0, 1)}</span>
          {/if}
          <span class="side-dot" class:busy={chat.busy} class:unread={chat.unread}></span>
        </span>
        {#if !collapsed}
          <span class="side-copy">
            <span class="side-name">{chat.name}</span>
            <span class="side-line">{chat.line}</span>
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
