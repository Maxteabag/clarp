<script>
  // One chat's own 'Show messages between agents' (POST /agent-coordination): like every chat
  // (the Host's setting), or always shown or hidden in this chat. The Host keeps it, so every
  // device follows; the snapshot row says the chat's choice (coordination_override).
  import { statusFor } from '../stores/app.svelte.js';
  import { coordination, setChatCoordination } from '../stores/coordination.svelte.js';

  let { session, active = true } = $props();

  let open = $state(false);
  let busy = $state(false);
  let row = $derived(statusFor(session));
  let choice = $derived(row.coordination_override || 'default');
  let shown = $derived((row.coordination_visibility || coordination.visibility) === 'show');

  const LABELS = { default: 'Like all chats', show: 'Show in this chat', hide: 'Hide in this chat' };

  async function pick(visibility) {
    open = false;
    if (visibility === choice || busy) return;
    busy = true;
    await setChatCoordination(session, visibility);
    busy = false;
  }

  function windowPointerDown(e) {
    if (open && !e.target.closest('.coordination-menu-wrap')) open = false;
  }
</script>

<svelte:window onpointerdowncapture={windowPointerDown}
               onkeydown={e => { if (e.key === 'Escape') open = false; }} />

<div class="coordination-menu-wrap">
  <button id={active ? 'historyCoordination' : undefined} class="history-btn coordination-btn"
          class:active={shown} aria-haspopup="menu" aria-expanded={open} disabled={busy}
          aria-label="Messages between agents"
          title={shown ? 'Messages between agents are shown in this chat' : 'Messages between agents are hidden in this chat'}
          onclick={() => open = !open}>⇄</button>
  {#if open}
    <div class="coordination-menu" role="menu" aria-label="Messages between agents in this chat">
      {#each Object.keys(LABELS) as value (value)}
        <button role="menuitemradio" aria-checked={choice === value} class:checked={choice === value}
                onclick={() => pick(value)}>
          {LABELS[value]}{#if value === 'default'}<span class="coordination-menu-hint"> ({coordination.visibility === 'show' ? 'shown' : 'hidden'})</span>{/if}
        </button>
      {/each}
    </div>
  {/if}
</div>
