<script>
  // Settings > Chats: how chats look. Kept in localStorage on this device, except 'Show
  // messages between agents', which is the Host's (static/lib/coordination.js).
  import { app, savedHosts } from '../stores/app.svelte.js';
  import { addHost, hostsState, knownHosts, removeHost, setAllHosts } from '../stores/hosts.svelte.js';
  import {
    prefs, setShowCoordinationLine, setShowHostBadge, setToolActivity,
  } from '../stores/prefs.svelte.js';
  import { TOOL_ACTIVITY, TOOL_ACTIVITY_LABELS } from '@core/history-fold.js';
  import { coordination, setCoordinationVisibility } from '../stores/coordination.svelte.js';
  import { live } from '../stores/live.svelte.js';
  import LanguageSetting from './LanguageSetting.svelte';
  import { formatRelative } from './i18n/format.js';
  import { lastSeenAt } from '@core/host-reach.js';
  import { t } from '../stores/i18n.svelte.js';
  import { isDown, openPanel, reach } from '../stores/reach.svelte.js';

  let { open = $bindable() } = $props();

  let hostCount = $derived(savedHosts().length);

  // Adding another Host: its address and a token paired with it (stores/hosts.svelte.js).
  let newUrl = $state('');
  let newToken = $state('');
  let adding = $state(false);
  let addError = $state('');
  async function add() {
    if (adding) return;
    adding = true;
    addError = await addHost(newUrl, newToken, app.hosts[0]?.id || '');
    adding = false;
    if (!addError) { newUrl = ''; newToken = ''; }
  }
  const STATE_WORDS = { live: 'Connected', connecting: 'Connecting', offline: 'Unreachable', refused: 'Token refused' };
  function keydown(e) {
    if (e.key === 'Escape') { open = false; e.stopPropagation(); }
  }
</script>

{#if open}
  <div class="start-dialog" onclick={e => { if (e.target === e.currentTarget) open = false; }}
       role="presentation">
    <div class="start-card" role="dialog" aria-label="Chat settings" tabindex="-1" onkeydown={keydown}>
      <h3>Chats</h3>

      <label class="start-field inline-toggle">
        <span>Show chats from all Hosts</span>
        <input type="checkbox" checked={hostsState.allHosts}
               onchange={e => setAllHosts(e.currentTarget.checked)} />
      </label>

      <div class="hosts-list" aria-label="Hosts">
        <div class="hosts-row primary"><span class="hosts-name">{app.hosts[0]?.name || 'This Host'}</span><span class="hosts-state">This Host</span></div>
        {#each knownHosts() as h (h.id)}
          <div class="hosts-row">
            <span class="hosts-name" title={h.url}>{h.name || h.url}</span>
            {#if hostsState.allHosts && isDown(h.id)}
              <!-- Its line opens the full Host-down panel (HostDownDialog). -->
              <button type="button" class="hosts-state hosts-down" onclick={() => { openPanel(h.id); open = false; }}>
                {t('host_down.list_row', { host: reach.hosts[h.id]?.label || h.name || h.url, when: formatRelative(lastSeenAt(reach.hosts[h.id]) || reach.now, reach.now) })}
              </button>
            {:else}
              <span class="hosts-state">{hostsState.allHosts ? (STATE_WORDS[hostsState.live[h.id]?.state] || 'Connecting') : 'Not read'}{#if h.via === 'host'} · via this Host{/if}</span>
            {/if}
            {#if h.via !== 'host'}
              <button class="hosts-remove" type="button" aria-label={`Remove ${h.name || h.url}`} title="Remove"
                      onclick={() => removeHost(h.id)}>×</button>
            {/if}
          </div>
        {/each}
      </div>
      <div class="hosts-add">
        <label class="start-field">
          <span>Host address</span>
          <input type="url" placeholder="https://studio.example.ts.net" bind:value={newUrl} autocomplete="off" />
        </label>
        <label class="start-field">
          <span>Token</span>
          <input type="password" placeholder="cld_…" bind:value={newToken} autocomplete="off" />
        </label>
        {#if addError}<p class="start-hint hosts-error" role="alert">{addError}</p>{/if}
        <p class="start-hint">
          Requires <code>{location.origin}</code> in <code>[server] web_read_origins</code>.
        </p>
        <div class="start-actions">
          <button class="start-btn" type="button" disabled={adding || !newUrl || !newToken} onclick={add}>
            {adding ? 'Adding…' : 'Add Host'}
          </button>
        </div>
      </div>

      <label class="start-field inline-toggle">
        <span>Show Host badge</span>
        <input type="checkbox" checked={prefs.showHostBadge}
               onchange={e => setShowHostBadge(e.currentTarget.checked)} />
      </label>

      {#if live.hiddenCoordination}
        <label class="start-field inline-toggle">
          <span>Show messages between agents</span>
          <input type="checkbox" checked={coordination.visibility === 'show'}
                 onchange={e => setCoordinationVisibility(e.currentTarget.checked ? 'show' : 'hide')} />
        </label>
        {#if coordination.error}<p class="start-hint">{coordination.error}</p>{/if}
        <!-- Listed whenever the Host hides coordination: a chat's own hide draws these lines
             even while the Host's setting shows them. -->
        <label class="start-field inline-toggle">
          <span>Show hidden agent-message marker</span>
          <input type="checkbox" checked={prefs.showCoordinationLine}
                 onchange={e => setShowCoordinationLine(e.currentTarget.checked)} />
        </label>
      {/if}

      <label class="start-field">
        <span>Tool activity</span>
        <select value={prefs.toolActivity} onchange={e => setToolActivity(e.currentTarget.value)}>
          {#each TOOL_ACTIVITY as mode (mode)}<option value={mode}>{TOOL_ACTIVITY_LABELS[mode]}</option>{/each}
        </select>
      </label>

      <LanguageSetting />

      <div class="start-actions">
        <button class="start-btn primary" onclick={() => open = false}>Done</button>
      </div>
    </div>
  </div>
{/if}

<style>
.hosts-down { border: 0; padding: 0; background: transparent; color: inherit; font: inherit; text-align: left; cursor: pointer; text-decoration: underline dotted; }
  .hosts-list { display: flex; flex-direction: column; border: 1px solid var(--ink-edge); border-radius: 8px; margin: 4px 0 12px; }
  .hosts-row { display: flex; align-items: center; gap: 10px; padding: 8px 10px; border-top: 1px solid var(--ink-edge); font: 400 13px/1.3 var(--font-body); }
  .hosts-row:first-child { border-top: 0; }
  .hosts-name { flex: 1 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--washi); }
  .hosts-state { flex: none; font-size: 11px; color: var(--washi-low); }
  .hosts-remove { flex: none; width: 26px; height: 26px; border: 1px solid var(--ink-edge); border-radius: 6px; background: transparent; color: var(--washi-dim); cursor: pointer; }
  .hosts-remove:hover { color: var(--vermillion); border-color: var(--vermillion); }
  .hosts-error { color: var(--vermillion); }
  .hosts-add code { font-family: var(--font-mono); font-size: 11px; }
</style>
