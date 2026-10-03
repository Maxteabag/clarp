<script>
  import { renderTool, renderTurnBodyCached } from './render.js';
  import LiveMessage from './live/LiveMessage.svelte';
  import { lazyHighlight } from './highlight.js';
  import { cellRow } from '@core/live-present.js';
  import CellRow from './live/CellRow.svelte';

  let { turn } = $props();

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
  let html = $derived.by(() => {
    turn.revision;      // tracked: a growing assistant row bumps this
    return streaming ? (turn.tools || []).map(renderTool).join('') : renderTurnBodyCached(turn);
  });
</script>

<!-- .turn carries content-visibility in styles.css, so an off-screen row
     costs no layout or paint. -->
<div
  class="turn {turn.role}"
  class:has-body={!!(turn.text && String(turn.text).trim())}
  class:unsent={!!turn.optimistic && !turn.failed}
  class:failed={!!turn.failed}
  use:lazyHighlight={html}
>
  <!-- The work comes before the words it led to, as in the live turn. -->
  {#each cells as cell (cell.id)}<CellRow row={cell} />{/each}
  {#if streaming}<LiveMessage text={turn.text} streaming />{/if}
  {@html html}
  {#if turn.failed}
    <!-- The whole point of the delivery log: a message that did not arrive
         says so, instead of sitting here looking identical to one that did. -->
    <span class="turn-delivery">not delivered · press ↑ to resend</span>
  {/if}
</div>
