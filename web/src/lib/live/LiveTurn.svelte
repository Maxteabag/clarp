<script>
  // The open (or last) turn rendered from live items (docs/live-items.md §7):
  // message text, one-line thinking rows, tool rows updated in place by id,
  // explore groups as one row, a plan card, a diff summary, and once the
  // turn settles its work folded behind "Worked for …".
  import { liveRows, settledFold } from '@core/live-present.js';
  import { renderText } from '../render.js';
  import LiveMessage from './LiveMessage.svelte';
  import LiveToolRow from './LiveToolRow.svelte';
  import { isOpen, toggle } from './expanded.svelte.js';

  let { liveState, items, explanations = true } = $props();

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

  let rows = $derived(liveRows(items, { explanations, now, skewMs: liveState.skewMs || 0 }));
  let fold = $derived(settledFold(liveState.turn, rows));
  let foldId = $derived(`fold:${liveState.turn ? liveState.turn.turn_id : ''}`);
  let foldOpen = $derived(isOpen(foldId));
  let shown = $derived(!fold || foldOpen ? rows : fold.visible);
</script>

<div class="live-turn" class:settled={!!fold}>
  {#if fold && fold.folded.length}
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
          <span class="lv-verb">Changed</span>
          <span class="lv-label">{(row.item.diff.files || []).length} files</span>
          <span class="lv-diff"><span class="add">+{row.item.diff.added || 0}</span> <span class="del">−{row.item.diff.removed || 0}</span></span>
        </div>
      </div>
    {:else if row.type === 'compaction'}
      <div class="lv-divider" data-item-id={row.id}>{row.running ? 'Compacting context…' : 'Context compacted'}</div>
    {/if}
  {/each}
</div>
