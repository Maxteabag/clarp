<script>
  import { renderTool, renderTurnBodyCached } from './render.js';
  import LiveMessage from './live/LiveMessage.svelte';
  import { lazyHighlight } from './highlight.js';
  import { blockChrome } from './blocks.js';
  import { cellRow, systemNotice } from '@core/live-present.js';
  import CellRow from './live/CellRow.svelte';
  import TurnNotice from './live/TurnNotice.svelte';

  // `strip`: the tool calls, cells and commentary the turn's work row took
  // (static/lib/history-fold.js).
  let { turn: row, strip = null } = $props();
  let turn = $derived(strip ? {
    ...row,
    text: strip.text ? '' : row.text,
    tools: (row.tools || []).filter(t => !strip.tools.includes(t)),
    display_cells: (row.display_cells || []).filter(c => !strip.cells.includes(c)),
  } : row);
  // A row whose only content went into the work row shows nothing.
  let empty = $derived(!!strip && !String(turn.text || '').trim() && !turn.tools.length
    && !turn.display_cells.length && !turn.failed);

  // Backend-neutral activity rows the Host already rendered (Codex commands,
  // explorations, patches). Keyed on the revision like the body.
  let cells = $derived.by(() => {
    turn.revision;
    return (turn.display_cells || []).map(cellRow).filter(Boolean);
  });

  // Markdown parsing is the expensive part of a turn. Keying it on the
  // revision means a turn that did not change is not re-parsed when its
  // neighbours do — which, in a keyed each, is every turn but one.
  // A reply still streaming into its /log row (Hosts without live items):
  // committed blocks render once and new text is paced, as in the live turn.
  let streaming = $derived(turn.kind === 'live' && !!turn.text);
  // A row the Host wrote about a turn (it failed, a restart cut it short).
  let notice = $derived(systemNotice(turn));
  let html = $derived.by(() => {
    turn.revision;      // tracked: a growing assistant row bumps this
    return streaming ? (turn.tools || []).map(renderTool).join('') : renderTurnBodyCached(turn);
  });
</script>

<!-- .turn carries content-visibility in styles.css, so an off-screen row
     costs no layout or paint. -->
{#if empty}
{:else if notice}
<div class="turn notice"><TurnNotice message={notice.message} detail={notice.detail} /></div>
{:else}
<div
  class="turn {turn.role}"
  class:has-body={!!(turn.text && String(turn.text).trim())}
  class:unsent={!!turn.optimistic && !turn.failed}
  class:failed={!!turn.failed}
  use:lazyHighlight={html}
  use:blockChrome={html}
>
  <!-- The work comes before the words it led to, as in the live turn. -->
  {#each cells as cell (cell.id)}<CellRow row={cell} />{/each}
  {#if streaming}<LiveMessage text={turn.text} streaming />{/if}
  {@html html}
  {#if turn.failed}
    <!-- The whole point of the delivery log: a message that did not arrive
         says so, instead of sitting here looking identical to one that did. -->
    <span class="turn-delivery">Not delivered · ↑ to resend</span>
  {:else if turn.optimistic && turn.waiting}
    <!-- Accepted and waiting in the agent's queue: its row comes when its turn starts. -->
    <span class="turn-delivery">Queued</span>
  {/if}
</div>
{/if}
