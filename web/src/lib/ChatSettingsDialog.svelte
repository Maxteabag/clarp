<script>
  // Settings > Chats: how chats look on this device. Kept in localStorage, never on the Host.
  import { app } from '../stores/app.svelte.js';
  import { prefs, setShowHostBadge } from '../stores/prefs.svelte.js';

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

      <div class="start-actions">
        <button class="start-btn primary" onclick={() => open = false}>Done</button>
      </div>
    </div>
  </div>
{/if}
