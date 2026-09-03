<script>
  // Orchestrator settings: whether an AI resolves delegations that name
  // matching could not, and which model does it.
  import { flash } from '../stores/app.svelte.js';

  let { open = $bindable() } = $props();

  // Routing providers an older Host does not list itself. A Host that sends
  // `providers` on /orchestrator/settings replaces this wholesale.
  const FALLBACK_OPTIONS = [
    { id: 'claude', label: 'Claude', kind: 'backend', catalog_backend: 'claude',
      detail: 'Runs an isolated Claude Code request on this Computer.' },
    { id: 'codex', label: 'Codex', kind: 'backend', catalog_backend: 'codex',
      detail: 'Runs an isolated Codex CLI request on this Computer.' },
    { id: 'agy', label: 'Antigravity', kind: 'backend', catalog_backend: 'agy',
      detail: 'Runs an isolated Antigravity request on this Computer.' },
    { id: 'openai', label: 'OpenAI API', kind: 'api', catalog_backend: 'codex',
      detail: 'Uses the configured OpenAI API key directly.',
      effort_options: ['minimal', 'low', 'medium', 'high'] },
  ];

  let enabled = $state(false);
  let fallbackOnly = $state(true);
  let confidence = $state(0.78);
  let provider = $state('openai');
  let model = $state('');
  let effort = $state('');
  let timeoutMs = $state(30000);
  let last = $state('Loading...');
  let ignored = $state([]);
  let providers = $state({});
  let options = $state(FALLBACK_OPTIONS);

  let option = $derived(
    options.find(o => o.id === provider) || { id: provider, label: provider, kind: 'backend',
                                             catalog_backend: provider, detail: '' });
  // The option names which catalogue row supplies its models (OpenAI reuses
  // Codex's: there is no separate OpenAI capability probe on the server).
  let capability = $derived(providers[option.catalog_backend || provider] || {});
  let models = $derived(capability.models || []);
  let efforts = $derived.by(() => {
    if (Array.isArray(option.effort_options)) return option.effort_options;
    const m = models.find(row => row.id === model);
    if (Array.isArray(m?.supported_efforts)) return m.supported_efforts;
    return Array.isArray(capability.supported_efforts) ? capability.supported_efforts : [];
  });
  let providerHelp = $derived(
    (option.detail || '') + (option.kind === 'api'
      ? ' GPT choices reuse the discovered Codex catalogue; API access is checked when routing runs.'
      : ' Models and effort levels come from this Computer’s live capability catalogue.'));

  $effect(() => {
    if (!open) return;
    load();
  });

  async function load() {
    last = 'Loading...';
    ignored = [];
    try {
      const [r, catalogue] = await Promise.all([
        fetch('/orchestrator/settings'),
        fetch('/agent-model-options').then(x => x.ok ? x.json() : null).catch(() => null),
      ]);
      const d = await r.json();
      const s = d.settings || {};
      providers = catalogue?.providers || {};
      options = Array.isArray(d.providers) && d.providers.length ? d.providers : FALLBACK_OPTIONS;
      enabled = !!s.enabled;
      fallbackOnly = s.fallback_only !== false;
      confidence = s.confidence_threshold || 0.78;
      provider = s.provider || 'openai';
      model = s.model || '';
      effort = s.effort || '';
      timeoutMs = s.timeout_ms || 30000;
      const recent = (d.recent_decisions || [])[0];
      last = recent
        ? `${recent.final_action || recent.decision_kind}: ${recent.target_session || 'none'} (${recent.confidence || 0})`
        : 'No decisions logged yet.';
      ignored = (Array.isArray(d.ignored_decisions) ? d.ignored_decisions : [])
        .slice(0, 10)
        .map(row => ({
          text: row.utterance || '',
          reason: row.reason || 'Ignored by orchestrator',
          conf: Number(row.confidence || 0).toFixed(2),
        }));
    } catch (_) {
      last = 'Failed to load orchestrator settings.';
      ignored = [];
    }
  }

  async function save() {
    try {
      const r = await fetch('/orchestrator/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          enabled: !!enabled,
          fallback_only: !!fallbackOnly,
          confidence_threshold: Number(confidence || 0.78),
          provider: provider || 'openai',
          model,
          effort,
          timeout_ms: Number(timeoutMs || 30000),
        }),
      });
      if (!r.ok) { flash('orchestrator save failed: ' + r.status, 2500); return; }
      flash(enabled ? 'Orchestrator on' : 'Orchestrator off', 1500);
      open = false;
    } catch (e) {
      flash('orchestrator save failed: ' + e.message, 2500);
    }
  }
</script>

{#if open}
  <div class="start-dialog">
    <div class="start-card">
      <h3>Orchestrator</h3>
      <p class="start-hint">
        Name matching runs first. The routing AI is a fallback for delegations
        that do not resolve to one agent.
      </p>

      <label class="start-field inline-toggle">
        <span>Use AI to resolve failed delegations</span>
        <input type="checkbox" bind:checked={enabled} />
      </label>
      <label class="start-field inline-toggle">
        <span>Only when name matching can’t decide</span>
        <input type="checkbox" bind:checked={fallbackOnly} />
      </label>
      <label class="start-field">
        <span>Automatic routing confidence</span>
        <input type="number" min="0.50" max="0.99" step="0.05" bind:value={confidence} />
      </label>
      <label class="start-field">
        <span>Routing AI provider</span>
        <select bind:value={provider} onchange={() => { model = ''; effort = ''; }}>
          {#each options as o (o.id)}
            <option value={o.id}>{o.label}{o.installed === false ? ' (not installed)' : ''}</option>
          {/each}
        </select>
      </label>
      <label class="start-field">
        <span>Routing model</span>
        <select bind:value={model} onchange={() => effort = ''}>
          <option value="">Provider default</option>
          {#each models as m (m.id)}<option value={m.id}>{m.label || m.id}</option>{/each}
        </select>
      </label>
      <label class="start-field">
        <span>Reasoning effort</span>
        <select bind:value={effort}>
          <option value="">Provider default</option>
          {#each efforts as e (e)}
            <option value={e}>{e.charAt(0).toUpperCase() + e.slice(1)}</option>
          {/each}
        </select>
      </label>
      <div class="orchestrator-last">{providerHelp}</div>
      <label class="start-field">
        <span>Timeout ms</span>
        <input type="number" min="250" max="60000" step="250" bind:value={timeoutMs} />
      </label>
      <div class="orchestrator-last">{last}</div>
      <div class="orchestrator-ignored">
        {#if ignored.length}
          <div class="orchestrator-ignored-title">Ignored dictation</div>
          {#each ignored as row, i (i)}
            <div class="orchestrator-ignored-row">
              <div class="orchestrator-ignored-text">{row.text}</div>
              <div class="orchestrator-ignored-meta">{row.reason} · {row.conf}</div>
            </div>
          {/each}
        {:else}
          <div class="orchestrator-ignored-empty">No ignored dictation yet.</div>
        {/if}
      </div>

      <div class="start-actions">
        <button class="start-btn ghost" onclick={() => open = false}>Cancel</button>
        <button class="start-btn primary" onclick={save}>Save</button>
      </div>
    </div>
  </div>
{/if}
