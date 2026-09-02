<script>
  // Space opens this: type a name, Enter goes there. A live chat switches the
  // pane; a contact with no session hands off to the start flow.
  //
  // Filtering is buildAgentOverview's, the same function the overview and the
  // rail use, so a query cannot mean one thing here and another there.
  import { buildAgentOverview } from '@core/agent-overview.js';
  import { quickSwitchRows, resolveChoice, clampIndex } from '@core/quick-switch.js';
  import { getLeafPanes } from '@core/pane-tree.js';
  import {
    agentSnapshot, app, avatarUrl, AVATAR_PALETTE, DEFAULT_ROSTER,
    isUserNotificationUnread,
  } from '../stores/app.svelte.js';
  import { panesState } from '../stores/panes.svelte.js';

  let { open = $bindable(), onChoose } = $props();

  let query = $state('');
  let index = $state(0);
  let inputEl = $state(null);

  // Reads at the top level so the tracking sees them; a read inside a helper
  // would leave this stale.
  let rows = $derived.by(() => {
    app.tick;
    const overview = buildAgentOverview({
      agentsBySession: app.agentsBySession,
      personas: agentSnapshot.personas,
      roster: agentSnapshot.roster.length ? agentSnapshot.roster : DEFAULT_ROSTER,
      availableSessions: app.availableSessions,
      currentSession: app.session,
      query,
      isUnread: isUserNotificationUnread,
    });
    return quickSwitchRows(overview, 12, query);
  });

  let selected = $derived(clampIndex(index, rows.length));

  // A fresh query starts at the top: the best match is first, and keeping an
  // old offset would leave the highlight on something unrelated.
  $effect(() => { query; index = 0; });

  $effect(() => {
    if (!open) return;
    query = '';
    index = 0;
    inputEl?.focus();
  });

  function choose(row) {
    const decision = resolveChoice(row, getLeafPanes(panesState.tree.root), panesState.tree.activeId);
    if (!decision) return;
    open = false;
    onChoose?.(decision);
  }

  function keydown(e) {
    if (e.key === 'ArrowDown' || (e.key === 'n' && e.ctrlKey)) {
      e.preventDefault(); index = selected + 1;
    } else if (e.key === 'ArrowUp' || (e.key === 'p' && e.ctrlKey)) {
      e.preventDefault(); index = selected - 1;
    } else if (e.key === 'Enter') {
      e.preventDefault(); choose(rows[selected]);
    } else if (e.key === 'Escape') {
      e.preventDefault(); open = false;
    }
  }
</script>

{#if open}
  <!-- svelte-ignore a11y_click_events_have_key_events -->
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div class="quick-backdrop" onclick={() => (open = false)}>
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <div class="quick" onclick={e => e.stopPropagation()}>
      <input
        class="quick-input"
        placeholder="Go to agent…"
        bind:this={inputEl}
        bind:value={query}
        onkeydown={keydown}
        aria-label="Go to agent"
      />
      <ul class="quick-list">
        {#each rows as row, i (row.key)}
          <li>
            <button
              class="quick-row"
              class:selected={i === selected}
              onmouseenter={() => (index = i)}
              onclick={() => choose(row)}
            >
              <span
                class="quick-avatar"
                style="background-color:{AVATAR_PALETTE[row.name] || 'var(--ochre)'};background-image:url('{avatarUrl(row.name, row.session)}')"
              ></span>
              <span class="quick-name">{row.name}</span>
              <span class="quick-detail" class:new={row.kind === 'contact'}>{row.detail}</span>
            </button>
          </li>
        {:else}
          <li class="quick-empty">No agent matches “{query}”</li>
        {/each}
      </ul>
    </div>
  </div>
{/if}

<style>
  .quick-backdrop {
    position: fixed;
    inset: 0;
    z-index: 60;
    display: flex;
    justify-content: center;
    align-items: flex-start;
    padding-top: 12vh;
    background: rgba(0, 0, 0, 0.45);
  }
  .quick {
    width: min(520px, 92vw);
    background: var(--ink-soft, #1a1b26);
    border: 1px solid rgba(255, 255, 255, 0.12);
    border-radius: 10px;
    box-shadow: 0 18px 48px rgba(0, 0, 0, 0.5);
    overflow: hidden;
  }
  .quick-input {
    width: 100%;
    box-sizing: border-box;
    padding: 12px 14px;
    border: 0;
    border-bottom: 1px solid rgba(255, 255, 255, 0.08);
    background: transparent;
    color: var(--washi, #c0caf5);
    font: 500 15px/1.4 var(--font-mono, monospace);
    outline: none;
  }
  .quick-list {
    margin: 0;
    padding: 4px;
    list-style: none;
    max-height: 46vh;
    overflow-y: auto;
  }
  .quick-row {
    display: flex;
    align-items: center;
    gap: 10px;
    width: 100%;
    padding: 7px 10px;
    border: 0;
    border-radius: 6px;
    background: transparent;
    color: var(--washi, #c0caf5);
    font: 400 13px/1.4 var(--font-ui, sans-serif);
    text-align: left;
    cursor: pointer;
  }
  .quick-row.selected {
    background: color-mix(in srgb, var(--accent-blue, #7aa2f7) 18%, transparent);
  }
  .quick-avatar {
    flex: none;
    width: 22px;
    height: 22px;
    border-radius: 50%;
    background-size: cover;
    background-position: center;
  }
  .quick-name { flex: none; font-weight: 600; }
  .quick-detail {
    margin-left: auto;
    font-size: 11px;
    color: #787c99;
    font-family: var(--font-mono, monospace);
  }
  .quick-detail.new { color: var(--accent-blue, #7aa2f7); }
  .quick-empty {
    padding: 14px;
    color: #787c99;
    font-size: 13px;
    text-align: center;
  }
</style>
