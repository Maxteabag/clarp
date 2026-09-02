<script>
  // Voice picker for one agent: preview a voice, or claim it.
  import { app, flash } from '../stores/app.svelte.js';

  let { open = $bindable(), sid = '', name = '', onChanged } = $props();

  let bio = $state('');
  let voices = $state([]);
  let state = $state('');

  $effect(() => {
    if (!open) return;
    load();
  });

  async function load() {
    state = 'Loading…';
    voices = [];
    bio = '';
    try {
      const r = await fetch('/voices?for=' + encodeURIComponent(sid));
      const d = await r.json();
      if (d.bio) bio = d.bio;
      const currentVoice = (app.agentsBySession[sid] || {}).voice_id;
      voices = (d.voices || []).map(v => ({
        id: v.id,
        label: v.label,
        isCurrent: v.id === currentVoice,
        // A voice another agent already owns can be previewed but not taken.
        taken: v.taken_by && v.id !== currentVoice ? v.taken_by : '',
      }));
      state = '';
    } catch (_) {
      state = 'Failed to load voices.';
    }
  }

  async function preview(id) {
    await fetch('/preview', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ voice_id: id, session: sid, text: `Hi, I'm ${name}.` }),
    }).catch(() => {});
  }

  async function pick(id) {
    const r = await fetch('/agent-voice', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session: sid, voice_id: id }),
    });
    if (!r.ok) {
      const d = await r.json().catch(() => ({}));
      flash('voice ' + (d.error || 'rejected'), 2000);
      return;
    }
    flash(`${name} voice updated`, 1500);
    open = false;
    onChanged?.();
  }
</script>

{#if open}
  <div class="start-dialog">
    <div class="start-card">
      <h3 id="voiceTitle">{name}'s voice</h3>
      {#if bio}<p class="voice-bio">{bio}</p>{/if}
      <ul class="voice-list">
        {#if state}
          <li>{state}</li>
        {:else}
          {#each voices as v (v.id)}
            <li class="voice-row" class:current={v.isCurrent} class:taken={!!v.taken}>
              <span class="voice-label">
                {v.label}{v.taken ? ` · used by ${v.taken}` : ''}{v.isCurrent ? ' · current' : ''}
              </span>
              <span class="voice-actions">
                <button class="voice-preview" onclick={() => preview(v.id)}>▶</button>
                {#if !v.taken && !v.isCurrent}
                  <button class="voice-pick" onclick={() => pick(v.id)}>use</button>
                {/if}
              </span>
            </li>
          {/each}
        {/if}
      </ul>
      <div class="start-actions">
        <button class="start-btn ghost" onclick={() => open = false}>Done</button>
      </div>
    </div>
  </div>
{/if}
