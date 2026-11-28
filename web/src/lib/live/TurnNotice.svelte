<script>
  // What the Host says about a turn it ended (a failure, a restart): a
  // notice in the chat, never styled as the agent's own reply. A Host that
  // explains it (`explained`, docs/turn-errors.md, contract 217) gets one
  // card: the catalog's sentence for its key in the app's language, the
  // buttons that fit, a count for repeats and a closed Details with the
  // provider's own words.
  import { errorActions, isRetry, retryBody } from '@core/live-present.js';
  import { actionLabel, appLanguage, errorSentence, hostWords, repeatCount, say, tr } from '@core/turn-error-text.js';

  let { message, detail = '', explained = null, count = 1, actionable = false, session = '' } = $props();

  const lang = appLanguage();
  // Action id -> 'busy' | 'done'.
  let states = $state({});
  let note = $state('');
  let noteFailed = $state(false);
  // A scheduled retry's time passing turns it into a plain Try again.
  let now = $state(Date.now());
  $effect(() => {
    const next = explained && (explained.actions || []).filter(a => a && a.id === 'retry_at')
      .map(a => a.at_ms || explained.resets_at_ms).filter(t => t > now).sort((a, b) => a - b)[0];
    if (!next) return;
    const id = setTimeout(() => { now = Date.now(); }, Math.min(next - now + 50, 2 ** 31 - 1));
    return () => clearTimeout(id);
  });
  let actions = $derived(explained
    ? errorActions(explained, now).filter(a => !isRetry(a.id) || (actionable && explained.message_id && session))
    : []);
  let sentence = $derived(explained ? errorSentence(explained, lang, now) : '');
  let meta = $derived(explained ? [explained.backend, explained.model, explained.trace_id].filter(Boolean).join(' · ') : '');

  async function press(action) {
    if (states[action.id] === 'busy' || states[action.id] === 'done') return;
    if (action.id === 'copy_details') {
      try {
        await navigator.clipboard.writeText(explained.copy_text || [hostWords(explained.text, 'en'), explained.detail, meta].filter(Boolean).join('\n\n'));
        states = { ...states, copy_details: 'done' };
        note = tr('error.copied', lang); noteFailed = false;
        setTimeout(() => { states = { ...states, copy_details: undefined }; note = ''; }, 2000);
      } catch (e) {
        note = tr('error.action_failed', lang); noteFailed = true;
      }
      return;
    }
    const body = retryBody(explained, session, action);
    if (!body) return;
    states = { ...states, [action.id]: 'busy' };
    try {
      const r = await fetch('/turn-retry', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      const d = await r.json().catch(() => ({}));
      if (!r.ok || d.ok !== true) throw new Error(d.error || `HTTP ${r.status}`);
      states = { ...states, [action.id]: 'done' };
      noteFailed = false;
      note = (d.status === 'scheduled' && say('error.retry_scheduled', { time_ms: body.at_ms }, lang)) || tr('error.retry_sent', lang);
    } catch (e) {
      console.warn('turn-retry refused:', e.message);
      states = { ...states, [action.id]: undefined };
      noteFailed = true;
      note = tr('error.action_failed', lang);
    }
  }
</script>

{#if explained}
<div class="turn-notice turn-error {explained.severity || 'error'}" role="group" aria-label={sentence}>
  <span class="tn-glyph" aria-hidden="true">{explained.severity === 'info' ? 'i' : '!'}</span>
  <div class="tn-body">
    <div class="tn-message">{sentence}{#if count > 1}<span class="tn-count">{repeatCount(count, lang)}</span>{/if}</div>
    {#if actions.length}
      <div class="tn-actions">
        {#each actions as action, i (action.id)}
          <button type="button" class:primary={i === 0} disabled={!!states[action.id]} aria-busy={states[action.id] === 'busy'}
            onclick={() => press(action)}>{#if states[action.id] === 'done'}✓ {:else if states[action.id] === 'busy'}… {/if}{actionLabel(action, lang, now)}</button>
        {/each}
      </div>
    {/if}
    {#if note}<div class="tn-note" class:failed={noteFailed} role="status">{note}</div>{/if}
    {#if explained.detail || meta}
      <details class="tn-details">
        <summary>{tr('error.details', lang)}</summary>
        {#if explained.detail}<pre>{explained.detail}</pre>{/if}
        {#if meta}<pre class="tn-meta">{meta}</pre>{/if}
      </details>
    {/if}
  </div>
</div>
{:else}
<div class="turn-notice" role="status">
  <span class="tn-glyph" aria-hidden="true">!</span>
  <div class="tn-body">
    <div class="tn-message">{message}</div>
    {#if detail}<div class="tn-detail">{detail}</div>{/if}
  </div>
</div>
{/if}
