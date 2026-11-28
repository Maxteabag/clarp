<script>
  // Voice picker for one agent: preview a voice, or claim it.
  import { t } from '../stores/i18n.svelte.js';
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
    state = 'voice.loading';
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
      state = 'voice.load_failed';
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
      flash(t('voice.rejected', { error: d.error || t('voice.rejected_default') }), 2000);
      return;
    }
    flash(t('voice.updated', { name }), 1500);
    open = false;
    onChanged?.();
  }
</script>

{#if open}
  <div class="start-dialog">
    <div class="start-card">
      <h3 id="voiceTitle" data-kicker={t('voice.kicker')}>{t('voice.title', { name })}</h3>
      {#if bio}<p class="voice-bio">{bio}</p>{/if}
      <ul class="voice-list">
        {#if state}
          <li>{t(state)}</li>
        {:else}
          {#each voices as v (v.id)}
            <li class="voice-row" class:current={v.isCurrent} class:taken={!!v.taken}>
              <span class="voice-label">
                {v.label}{v.taken ? ` · ${t('voice.used_by', { name: v.taken })}` : ''}{v.isCurrent ? ` · ${t('voice.current')}` : ''}
              </span>
              <span class="voice-actions">
                <button class="voice-preview" onclick={() => preview(v.id)}>▶</button>
                {#if !v.taken && !v.isCurrent}
                  <button class="voice-pick" onclick={() => pick(v.id)}>{t('voice.use')}</button>
                {/if}
              </span>
            </li>
          {/each}
        {/if}
      </ul>
      <div class="start-actions">
        <button class="start-btn ghost" onclick={() => open = false}>{t('voice.done')}</button>
      </div>
    </div>
  </div>
{/if}
