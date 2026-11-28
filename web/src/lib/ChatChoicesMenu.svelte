<script>
  // The chat's quick '…' menu (#653): its own 'Messages between agents' (kept by the Host) and
  // its own 'Tool activity' (kept on this device), each a submenu that shows its current value.
  import { CHAT_TOOL_ACTIVITY, toolActivityKey } from '@core/history-fold.js';
  import { OVERRIDE_KEYS, OVERRIDES } from '@core/coordination.js';
  import { t } from '../stores/i18n.svelte.js';
  import {
    chatChoicesReady, chatCoordinationChoice, chatToolActivity, setChatCoordinationChoice,
    setChatToolActivity,
  } from '../stores/chat-choices.svelte.js';
  import { live } from '../stores/live.svelte.js';
  import { app } from '../stores/app.svelte.js';

  let { session, active = true } = $props();

  let open = $state(false);
  /** The open submenu: '' | 'coordination' | 'tools'. */
  let sub = $state('');
  let busy = $state(false);

  let coordination = $derived((app.tick, chatCoordinationChoice(session)));
  let tools = $derived(chatToolActivity(session));
  let ready = $derived(chatChoicesReady(session));

  function close() { open = false; sub = ''; }

  async function pickCoordination(choice) {
    close();
    if (busy) return;
    busy = true;
    await setChatCoordinationChoice(session, choice);
    busy = false;
  }

  function pickTools(mode) {
    close();
    setChatToolActivity(session, mode);
  }

  function windowPointerDown(e) {
    if (open && !e.target.closest('.chat-menu-wrap')) close();
  }
</script>

<svelte:window onpointerdowncapture={windowPointerDown}
               onkeydown={e => { if (e.key === 'Escape') close(); }} />

<div class="chat-menu-wrap">
  <button id={active ? 'historyChatMenu' : undefined} class="history-btn chat-menu-btn"
          aria-haspopup="menu" aria-expanded={open} aria-label={t('chat_choices.menu')} title={t('chat_choices.menu')}
          onclick={() => { open = !open; sub = ''; }}>…</button>
  {#if open}
    <div class="chat-menu" role="menu" aria-label={t('chat_choices.menu')}>
      {#if live.hiddenCoordination}
        <button class="chat-menu-row" role="menuitem" aria-haspopup="menu" aria-expanded={sub === 'coordination'}
                disabled={busy} onclick={() => sub = sub === 'coordination' ? '' : 'coordination'}>
          <span>{t('chat_choices.coordination')}</span><span class="chat-menu-value">{t(OVERRIDE_KEYS[coordination])}</span>
          <span class="chat-menu-caret" class:open={sub === 'coordination'} aria-hidden="true">›</span>
        </button>
        {#if sub === 'coordination'}
          <div class="chat-submenu" role="menu" aria-label={t('chat_choices.coordination_here')}>
            {#each OVERRIDES as value (value)}
              <button role="menuitemradio" aria-checked={coordination === value} class:checked={coordination === value}
                      onclick={() => pickCoordination(value)}>{t(OVERRIDE_KEYS[value])}</button>
            {/each}
          </div>
        {/if}
      {/if}
      <button class="chat-menu-row" role="menuitem" aria-haspopup="menu" aria-expanded={sub === 'tools'}
              disabled={!ready} onclick={() => sub = sub === 'tools' ? '' : 'tools'}>
        <span>{t('chat_choices.tool_activity')}</span><span class="chat-menu-value">{t(toolActivityKey(tools))}</span>
        <span class="chat-menu-caret" class:open={sub === 'tools'} aria-hidden="true">›</span>
      </button>
      {#if sub === 'tools'}
        <div class="chat-submenu" role="menu" aria-label={t('chat_choices.tool_activity_here')}>
          {#each CHAT_TOOL_ACTIVITY as value (value)}
            <button role="menuitemradio" aria-checked={tools === value} class:checked={tools === value}
                    onclick={() => pickTools(value)}>{t(toolActivityKey(value))}</button>
          {/each}
        </div>
      {/if}
    </div>
  {/if}
</div>
