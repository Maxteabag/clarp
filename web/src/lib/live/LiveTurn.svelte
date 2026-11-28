<script>
  // The open (or last) turn rendered from live items (docs/live-items.md §7):
  // message text, one-line thinking rows, tool rows updated in place by id,
  // explore groups as one row, a plan card, a diff summary, and once the
  // turn settles its work folded behind "Worked for …".
  import { currentTurnItems, liveRows, partFold } from '@core/live-present.js';
  import { renderText } from '../render.js';
  import LiveMessage from './LiveMessage.svelte';
  import LiveToolRow from './LiveToolRow.svelte';
  import TurnNotice from './TurnNotice.svelte';
  import { isOpen, toggle } from './expanded.svelte.js';
  import { t } from '../../stores/i18n.svelte.js';
  import { formatDuration } from '../i18n/format.js';

  // `last`: this part is the one the turn ends in (a turn split at a steer draws its
  // fold button and error once, there); an unsplit turn is its own last part.
  import { errorPlanFor, sessionOfLive } from '../../stores/turn-errors.svelte.js';

  let { liveState, items, explanations = true, last = true } = $props();

  let running = $derived(items.some(i => i.status === 'running' || i.status === 'pending'));
  // Elapsed times and the 600 ms output delay need a clock, but only while
  // something runs; a settled turn never re-renders on its own.
  let now = $state(Date.now());
  $effect(() => {
    if (!running) return;
    now = Date.now();
    const id = setInterval(() => { now = Date.now(); }, 500);
    return () => clearInterval(id);
  });

  let rows = $derived(liveRows(items, { explanations, now, skewMs: liveState.skewMs || 0, tr: t, duration: formatDuration }));
  // The whole turn's rows, read only by the part the turn ends in and only when the turn
  // was split: whether any part's work is folded decides its button.
  let wholeRows = $derived.by(() => {
    // Only a settled turn folds; a running one must not pay for this on every live event.
    if (!last || !(liveState.turn && liveState.turn.ended_at_ms)) return null;
    const all = currentTurnItems(liveState);
    return all.length === items.length ? null : liveRows(all, { explanations, now, skewMs: liveState.skewMs || 0, tr: t, duration: formatDuration });
  });
  let fold = $derived(partFold(liveState.turn, rows, { last, wholeRows, tr: t, duration: formatDuration }));
  let foldId = $derived(`fold:${liveState.turn ? liveState.turn.turn_id : ''}`);
  let foldOpen = $derived(isOpen(foldId));
  let shown = $derived(!fold || foldOpen ? rows : fold.visible);
  // An explained failure (docs/turn-errors.md): the chat's cards, so the live card counts the
  // repeats before it and offers a retry only while it is the newest.
  let session = $derived(fold && fold.error && fold.error.explained ? sessionOfLive(liveState) : '');
  let errorPlan = $derived(session ? errorPlanFor(session) : null);
</script>

<div class="live-turn" class:settled={!!fold}>
  {#if fold && fold.label && fold.opens}
    <button class="lv-fold" type="button" aria-expanded={foldOpen} onclick={() => toggle(foldId)}>
      <span class="lv-caret" class:open={foldOpen} aria-hidden="true">▸</span>{fold.label}
    </button>
  {/if}
  {#each shown as row (row.id)}
    {#if row.type === 'message'}
      <div class="turn assistant has-body live-msg" class:commentary={row.phase === 'commentary'} data-item-id={row.id}>
        <LiveMessage text={row.text} streaming={row.streaming} />
      </div>
    {:else if row.type === 'reasoning'}
      {@const open = isOpen(row.id)}
      <div class="lv-reasoning" class:running={row.running} data-item-id={row.id}>
        <button class="lv-head" type="button" aria-expanded={open} disabled={!row.text}
                onclick={() => toggle(row.id)}>
          <span class="lv-glyph" aria-hidden="true">◌</span><span class="lv-label">{row.label}</span>
        </button>
        {#if open && row.text}<div class="lv-thought">{@html renderText(row.text, row.running)}</div>{/if}
      </div>
    {:else if row.type === 'explore'}
      {@const open = isOpen(row.id)}
      <div class="lv-tool lv-explore {row.status}" data-item-id={row.id}>
        <button class="lv-head" type="button" aria-expanded={open} onclick={() => toggle(row.id)}>
          <span class="lv-dot" aria-hidden="true"></span>
          <span class="lv-verb">{row.label}</span>
          {#if row.running}<span class="lv-label">{row.members[row.members.length - 1].label}</span>{/if}
        </button>
        {#if open}
          <ul class="lv-members">
            {#each row.members as m (m.id)}<li class={m.status}><span class="lv-verb">{m.verb}</span> {m.label}</li>{/each}
          </ul>
        {/if}
      </div>
    {:else if row.type === 'tool'}
      <LiveToolRow {row} {explanations} />
    {:else if row.type === 'plan'}
      <div class="lv-plan" data-item-id={row.id}>
        {#each (row.item.plan && row.item.plan.steps) || [] as step}
          <div class="lv-step {step.status}"><span aria-hidden="true">{step.status === 'completed' ? '✓' : step.status === 'in_progress' ? '●' : '○'}</span> {step.text}</div>
        {/each}
      </div>
    {:else if row.type === 'diff' && row.item.diff}
      <div class="lv-tool completed lv-turn-diff" data-item-id={row.id}>
        <div class="lv-head"><span class="lv-dot" aria-hidden="true"></span>
          <span class="lv-verb">{t('live.changed')}</span>
          <span class="lv-label">{t('live.changed_files', { count: (row.item.diff.files || []).length })}</span>
          <span class="lv-diff"><span class="add">+{row.item.diff.added || 0}</span> <span class="del">−{row.item.diff.removed || 0}</span></span>
        </div>
      </div>
    {:else if row.type === 'compaction'}
      <div class="lv-divider" data-item-id={row.id}>{row.running ? t('live.compacting_context') : t('live.context_compacted')}</div>
    {/if}
  {/each}
  {#if fold && fold.error}
    <TurnNotice message={fold.error.message} detail={fold.error.detail} explained={fold.error.explained || null} {session}
      count={errorPlan ? errorPlan.counts.get('live') || 1 : 1} actionable={!!errorPlan && errorPlan.actionable === 'live'} />
  {/if}
</div>
