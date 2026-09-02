<script>
  import { renderTurnBody } from './render.js';
  import { lazyHighlight } from './highlight.js';

  let { turn } = $props();

  // Markdown parsing is the expensive part of a turn. Keying it on the
  // revision means a turn that did not change is not re-parsed when its
  // neighbours do — which, in a keyed each, is every turn but one.
  let html = $derived.by(() => {
    turn.revision;      // tracked: a growing assistant row bumps this
    return renderTurnBody(turn);
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
  {@html html}
  {#if turn.failed}
    <!-- The whole point of the delivery log: a message that did not arrive
         says so, instead of sitting here looking identical to one that did. -->
    <span class="turn-delivery">not delivered · press ↑ to resend</span>
  {/if}
</div>
