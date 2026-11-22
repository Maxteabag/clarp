<script>
  // Settings > Chats: how chats look. Kept in localStorage on this device, except 'Show
  // messages between agents', which is the Host's (static/lib/coordination.js).
  import { app } from '../stores/app.svelte.js';
  import {
    prefs, setShowCoordinationLine, setShowHostBadge, setToolActivity,
  } from '../stores/prefs.svelte.js';
  import { TOOL_ACTIVITY, TOOL_ACTIVITY_LABELS } from '@core/history-fold.js';
  import { coordination, setCoordinationVisibility } from '../stores/coordination.svelte.js';
  import { live } from '../stores/live.svelte.js';

  let { open = $bindable() } = $props();

  let hostCount = $derived(app.hosts.length);
</script>

{#if open}
  <div class="start-dialog" onclick={e => { if (e.target === e.currentTarget) open = false; }}
       role="presentation">
    <div class="start-card" role="dialog" aria-label="Chat settings">
      <h3>Chats</h3>

      <label class="start-field inline-toggle">
        <span>Show the Host on each chat</span>
        <input type="checkbox" checked={prefs.showHostBadge}
               onchange={e => setShowHostBadge(e.currentTarget.checked)} />
      </label>
      <p class="start-hint">
        {#if hostCount > 1}
          A small label names the Host on every chat row and in the chat header.
        {:else}
          Shown once this app reads chats from more than one Host; with one Host there is
          nothing to tell apart.
        {/if}
      </p>

      {#if live.hiddenCoordination}
        <label class="start-field inline-toggle">
          <span>Show messages between agents</span>
          <input type="checkbox" checked={coordination.visibility === 'show'}
                 onchange={e => setCoordinationVisibility(e.currentTarget.checked ? 'show' : 'hide')} />
        </label>
        <p class="start-hint">
          {#if coordination.error}{coordination.error}{:else}
            Messages agents send each other, and the work they do for them, in every chat that has no
            setting of its own (⇄ in a chat). The Host keeps this, so your other devices follow.
            Notes, cards and files an agent leaves for you always show.
          {/if}
        </p>
        <!-- Listed whenever the Host hides coordination: a chat's own hide draws these lines
             even while the Host's setting shows them. -->
        <label class="start-field inline-toggle">
          <span>Show a line where agent messages were</span>
          <input type="checkbox" checked={prefs.showCoordinationLine}
                 onchange={e => setShowCoordinationLine(e.currentTarget.checked)} />
        </label>
        <p class="start-hint">Applies wherever messages between agents are hidden, in every chat.</p>
      {/if}

      <label class="start-field">
        <span>Tool activity</span>
        <select value={prefs.toolActivity} onchange={e => setToolActivity(e.currentTarget.value)}>
          {#each TOOL_ACTIVITY as mode (mode)}<option value={mode}>{TOOL_ACTIVITY_LABELS[mode]}</option>{/each}
        </select>
      </label>
      <p class="start-hint">
        {#if prefs.toolActivity === 'grouped'}
          Each finished turn's tool calls sit behind one line, 'Worked for 11s · 3 tools', which opens in place.
        {:else if prefs.toolActivity === 'always'}
          Every tool call shows where it was made.
        {:else if prefs.toolActivity === 'group_old'}
          The newest turn shows its tool calls; older turns are grouped.
        {:else}
          A turn shows its work while it runs; once it has finished, only its messages stay.
        {/if}
        Your messages, replies, cards, errors and notices are never hidden.
      </p>

      <div class="start-actions">
        <button class="start-btn primary" onclick={() => open = false}>Done</button>
      </div>
    </div>
  </div>
{/if}
