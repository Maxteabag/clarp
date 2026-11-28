<script>
  // What the Host says about a turn it ended (a failure, a restart): a
  // notice in the chat, never styled as the agent's own reply. A Host that
  // explains it (`explained`, docs/turn-errors.md, contract 217) gets one
  // card: its sentence in the reader's language, the buttons that fit, a
  // count for repeats and a closed Details with the provider's own words.
  import { errorActions, isRetry, prefersNorwegian, retryBody, said } from '@core/live-present.js';

  let { message, detail = '', explained = null, count = 1, actionable = false, session = '' } = $props();

  const nb = prefersNorwegian();
  // Action id → 'busy' | 'done' | an error note.
  let states = $state({});
  let note = $state('');
  let noteFailed = $state(false);
  let actions = $derived(explained
    ? errorActions(explained).filter(a => !isRetry(a.id) || (actionable && explained.message_id && session))
    : []);
  let meta = $derived(explained ? [explained.backend, explained.model, explained.trace_id].filter(Boolean).join(' · ') : '');

  async function press(action) {
    if (states[action.id] === 'busy' || states[action.id] === 'done') return;
    if (action.id === 'copy_details') {
      try {
        await navigator.clipboard.writeText(explained.copy_text || [said(explained.text, false), explained.detail, meta].filter(Boolean).join('\n\n'));
        states = { ...states, copy_details: 'done' };
        note = nb ? 'Kopiert.' : 'Copied.'; noteFailed = false;
        setTimeout(() => { states = { ...states, copy_details: undefined }; note = ''; }, 2000);
      } catch (e) {
        note = nb ? 'Kunne ikke kopiere.' : 'Could not copy.'; noteFailed = true;
      }
      return;
    }
    const body = retryBody(explained, session, action);
    if (!body) return;
    states = { ...states, [action.id]: 'busy' };
    try {
      const r = await fetch('/turn-retry', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      const d = await r.json().catch(() => ({}));
      if (!r.ok || d.ok !== true) throw Object.assign(new Error(d.error || `HTTP ${r.status}`), { status: r.status });
      states = { ...states, [action.id]: 'done' };
      noteFailed = false;
      note = {
        scheduled: nb ? 'Planlagt.' : 'Scheduled.',
        compacting: nb ? 'Komprimerer samtalen, så sendes meldingen på nytt.' : 'Compacting the conversation, then sending the message again.',
        refreshing: nb ? 'Starter en ny økt med samme samtale.' : 'Starting a fresh session with the same conversation.',
      }[d.status] || (nb ? 'Sendt på nytt.' : 'Sent again.');
    } catch (e) {
      states = { ...states, [action.id]: undefined };
      noteFailed = true;
      note = e.status === 404 ? (nb ? 'Meldingen finnes ikke lenger i samtalen.' : 'The message is no longer in the conversation.')
        : e.status === 409 ? (nb ? 'Agenten har allerede svart på en nyere melding.' : 'The agent has already answered a newer message.')
        : `${nb ? 'Kunne ikke sende den på nytt' : 'Could not send it again'}: ${e.message}`;
    }
  }
</script>

{#if explained}
<div class="turn-notice turn-error {explained.severity || 'error'}" role="group" aria-label={said(explained.text, nb)}>
  <span class="tn-glyph" aria-hidden="true">{explained.severity === 'info' ? 'i' : '!'}</span>
  <div class="tn-body">
    <div class="tn-message">{said(explained.text, nb)}{#if count > 1}<span class="tn-count">{count}×</span>{/if}</div>
    {#if actions.length}
      <div class="tn-actions">
        {#each actions as action, i (action.id)}
          <button type="button" class:primary={i === 0} disabled={!!states[action.id]} aria-busy={states[action.id] === 'busy'}
            onclick={() => press(action)}>{#if states[action.id] === 'done'}✓ {:else if states[action.id] === 'busy'}… {/if}{said(action.label, nb)}</button>
        {/each}
      </div>
    {/if}
    {#if note}<div class="tn-note" class:failed={noteFailed} role="status">{note}</div>{/if}
    {#if explained.detail || meta}
      <details class="tn-details">
        <summary>{nb ? 'Detaljer' : 'Details'}</summary>
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
