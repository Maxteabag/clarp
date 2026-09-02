<script>
  // Start / relaunch an agent: working directory (with completion and
  // favourites), backend, model, effort, and fresh/resume/fork.
  import {
    app, flash, setSession,
  } from '../stores/app.svelte.js';
  import { audio } from '../stores/audio.svelte.js';
  import { reset } from '../stores/conversations.svelte.js';
  import {
    normalizeBackend, resumableBackend, supportsForkBackend,
    backendLabel, catalogBackendIds,
  } from '@core/agent-launch.js';
  import { AgentBackend } from '@core/protocol.js';
  import { pathTail } from './render.js';

  let { open = $bindable(), name = '', replaceSid = '', onDone } = $props();

  // The server's live capability catalogue (/agent-model-options), with a
  // static fallback so the dialog still offers sensible choices when it is down.
  const FALLBACK = {
    providers: {
      claude: {
        models: ['sonnet', 'opus', 'haiku'].map(id => ({
          id, label: id, supported_efforts: ['low', 'medium', 'high', 'xhigh', 'max'],
        })),
      },
      codex: {
        models: ['gpt-5.4', 'gpt-5.4-mini', 'gpt-5.2-codex'].map(id => ({
          id, label: id, supported_efforts: ['low', 'medium', 'high'],
        })),
      },
      agy: { models: [], supported_efforts: ['low', 'medium', 'high'] },
      grok: {
        models: ['grok-4.6', 'grok-4.5'].map(id => ({
          id, label: id, supported_efforts: ['low', 'medium', 'high'],
        })),
      },
      opencode: {
        models: ['opencode/gpt-5.4', 'anthropic/claude-sonnet-4-5'].map(id => ({
          id, label: id, supported_efforts: ['low', 'medium', 'high', 'max'],
        })),
      },
    },
  };

  let catalogue = $state(FALLBACK);
  let cwd = $state('~');
  let backend = $state(AgentBackend.CLAUDE);
  let mode = $state('fresh');
  let modelPreset = $state('');
  let modelCustom = $state('');
  let effort = $state('');
  let suggestions = $state([]);
  let favorites = $state([]);
  let resumeItems = $state([]);
  let resumeState = $state('');
  let chosen = $state('');

  let existing = $derived(replaceSid ? (app.agentsBySession[replaceSid] || {}) : {});
  let provider = $derived((catalogue.providers || {})[normalizeBackend(backend)] || {});
  let modelEntries = $derived(
    (Array.isArray(provider.models) ? provider.models : []).filter(m => m && m.id));
  let models = $derived(modelEntries.map(m => m.id));
  // Efforts are provider-wide (agy) or per model; offer the selected model's
  // list, or everything any model supports when no preset is chosen.
  let efforts = $derived.by(() => {
    if (Array.isArray(provider.supported_efforts)) return provider.supported_efforts;
    const chosenEntry = modelEntries.find(m => m.id === modelPreset);
    const source = chosenEntry ? [chosenEntry] : modelEntries;
    return [...new Set(source.flatMap(m => m.supported_efforts || []))];
  });
  let canFork = $derived(supportsForkBackend(backend, catalogue));
  let backendIds = $derived(catalogBackendIds(catalogue));

  // Seed the form when the dialog opens.
  $effect(() => {
    if (!open) return;
    const savedCwd = replaceSid && existing.cwd;
    cwd = savedCwd || localStorage.getItem('lastAgentCwd') || '~';
    mode = 'fresh';
    chosen = '';
    resumeItems = [];
    resumeState = '';
    suggestions = [];
    backend = normalizeBackend(
      (replaceSid && existing.backend)
      || localStorage.getItem('lastAgentBackend') || AgentBackend.CLAUDE);
    loadCatalogue();
    fetchFavorites();
  });

  // Backend switch changes the model list; keep the agent's own model only
  // when the backend still matches.
  $effect(() => {
    const canReuse = replaceSid
      && normalizeBackend(existing.backend) === normalizeBackend(backend);
    const existingModel = canReuse ? (existing.model || '') : '';
    const existingEffort = canReuse ? (existing.effort || '') : '';
    modelPreset = models.includes(existingModel) ? existingModel : '';
    modelCustom = existingModel && !models.includes(existingModel) ? existingModel : '';
    effort = efforts.includes(existingEffort) ? existingEffort : '';
  });

  // Fork is Claude-only; drop back to fresh rather than leaving it selected
  // and disabled.
  $effect(() => {
    if (mode === 'fork' && !canFork) mode = 'fresh';
  });

  let catalogueLoaded = false;
  async function loadCatalogue() {
    if (catalogueLoaded) return;
    try {
      const r = await fetch('/agent-model-options');
      if (!r.ok) throw new Error(String(r.status));
      const d = await r.json();
      if (d && d.providers) catalogue = d;
    } catch (_) {
      catalogue = FALLBACK;
    }
    catalogueLoaded = true;
  }

  let suggestTimer = null;
  function onCwdInput() {
    clearTimeout(suggestTimer);
    suggestTimer = setTimeout(() => {
      fetchSuggestions();
      if (mode === 'resume' || mode === 'fork') fetchResume();
    }, 200);
  }

  async function fetchSuggestions() {
    try {
      const r = await fetch('/dirs?path=' + encodeURIComponent(cwd));
      const d = await r.json();
      suggestions = d.matches || [];
    } catch (_) { suggestions = []; }
  }

  async function fetchFavorites() {
    try {
      const r = await fetch('/favorite-paths?limit=5');
      if (!r.ok) throw new Error(String(r.status));
      const d = await r.json();
      favorites = (Array.isArray(d.paths) ? d.paths : []).map(item => {
        const path = String((item && item.path) || '');
        const count = Number((item && item.use_count) || 0);
        return {
          path,
          label: pathTail(path) || path,
          title: count > 1 ? `${path} (${count} launches)` : path,
        };
      });
    } catch (_) { favorites = []; }
  }

  async function fetchResume() {
    chosen = '';
    resumeState = 'Loading…';
    resumeItems = [];
    try {
      const r = await fetch('/past-sessions?cwd=' + encodeURIComponent(cwd)
                            + '&backend=' + encodeURIComponent(resumableBackend(backend)));
      const d = await r.json();
      const items = d.sessions || [];
      if (!items.length) { resumeState = 'No past sessions for that directory.'; return; }
      resumeState = '';
      resumeItems = items.map(s => ({
        id: s.id,
        preview: s.preview || '(no preview)',
        date: new Date(s.mtime * 1000).toLocaleString(),
      }));
    } catch (_) {
      resumeState = 'Failed to load past sessions.';
    }
  }

  function onModeChange(next) {
    mode = next;
    if (next === 'resume' || next === 'fork') fetchResume();
    else { resumeItems = []; resumeState = ''; }
  }

  function onBackendChange(next) {
    backend = next;
    chosen = '';
    if (mode === 'resume' || mode === 'fork') fetchResume();
  }

  async function submit() {
    const dir = cwd.trim().replace(/\/+$/, '') || '~';
    localStorage.setItem('lastAgentCwd', dir);
    const chosenBackend = resumableBackend(backend);
    localStorage.setItem('lastAgentBackend', chosenBackend);
    const selectedModel = (modelCustom || '').trim() || (modelPreset || '').trim();
    const payload = {
      name,
      session: name.toLowerCase(),
      cwd: dir,
      backend: chosenBackend,
      synthesize_audio: !audio.muted,
    };
    if (selectedModel) payload.model = selectedModel;
    if (effort) payload.effort = effort;
    if (replaceSid) payload.replace_sid = replaceSid;
    if (mode === 'resume' && chosen) payload.resume_session_id = chosen;
    if (mode === 'fork' && chosen) payload.fork_session_id = chosen;
    open = false;
    try {
      const r = await fetch('/agents', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) {
        flash(d.message || ((replaceSid ? 'relaunch' : 'start') + ': ' + (d.error || r.status)), 4500);
        onDone(false);
        return;
      }
      // Land the user on the new agent's pane. A relaunch or fork keeps the
      // session id but starts a new conversation, so drop what was cached.
      const newSession = d.session || (replaceSid || payload.session);
      if (newSession) {
        if (replaceSid) reset(newSession);
        await setSession(newSession);
      }
      onDone(true);
    } catch (e) {
      flash('start failed: ' + e.message, 2500);
      onDone(false);
    }
  }
</script>

{#if open}
  <div class="start-dialog">
    <div class="start-card">
      <h3 id="startTitle">{replaceSid ? `Relaunch ${name}` : `Start ${name}`}</h3>

      <label class="start-field">
        <span>Working directory</span>
        <input type="text" autocomplete="off" placeholder="~ or ~/Projects/example"
               bind:value={cwd} oninput={onCwdInput} />
      </label>
      {#if suggestions.length}
        <ul class="start-suggestions">
          {#each suggestions as p (p)}
            <li>
              <button type="button" onclick={() => { cwd = p + '/'; suggestions = []; }}>{p}</button>
            </li>
          {/each}
        </ul>
      {/if}
      {#if favorites.length}
        <ul class="start-favorites">
          {#each favorites as f (f.path)}
            <li>
              <button type="button" title={f.title} onclick={() => {
                cwd = f.path;
                favorites = [];
                suggestions = [];
                if (mode === 'resume' || mode === 'fork') fetchResume();
              }}>{f.label}</button>
            </li>
          {/each}
        </ul>
      {/if}

      <div class="start-backends">
        {#each backendIds as b (b)}
          <label>
            <input type="radio" value={b} checked={backend === b}
                   onchange={() => onBackendChange(b)} />
            {backendLabel(b, catalogue)}
          </label>
        {/each}
      </div>

      <label class="start-field">
        <span>Model</span>
        <select bind:value={modelPreset}>
          <option value="">Default</option>
          {#each modelEntries as m (m.id)}<option value={m.id}>{m.label || m.id}</option>{/each}
        </select>
      </label>

      <label class="start-field">
        <span>Custom model</span>
        <input type="text" autocomplete="off" placeholder="optional" bind:value={modelCustom} />
      </label>

      {#if efforts.length}
        <label class="start-field">
          <span>Effort</span>
          <select bind:value={effort}>
            <option value="">Default</option>
            {#each efforts as e (e)}<option value={e}>{e}</option>{/each}
          </select>
        </label>
      {/if}

      <div class="start-modes">
        <label>
          <input type="radio" checked={mode === 'fresh'} onchange={() => onModeChange('fresh')} /> Fresh
        </label>
        <label>
          <input type="radio" checked={mode === 'resume'} onchange={() => onModeChange('resume')} /> Resume…
        </label>
        <label>
          <input type="radio" checked={mode === 'fork'} disabled={!canFork}
                 onchange={() => onModeChange('fork')} /> Fork…
        </label>
      </div>

      {#if resumeState || resumeItems.length}
        <ul class="start-resume">
          {#if resumeState}
            <li>{resumeState}</li>
          {:else}
            {#each resumeItems as s (s.id)}
              <li class:selected={chosen === s.id}>
                <button type="button" onclick={() => chosen = s.id}>
                  <div class="preview">{s.preview}</div>
                  <div class="meta">{s.date}</div>
                </button>
              </li>
            {/each}
          {/if}
        </ul>
      {/if}

      <div class="start-actions">
        <button class="start-btn ghost" onclick={() => { open = false; onDone(false); }}>Cancel</button>
        <button class="start-btn primary" onclick={submit}>{replaceSid ? 'Relaunch' : 'Start'}</button>
      </div>
    </div>
  </div>
{/if}
