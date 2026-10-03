<script>
  // A streaming message: committed blocks render once and keep their DOM
  // (selection, highlighted code); only the open tail is re-rendered, and
  // new text is revealed at a pace that drains a burst in about 200 ms.
  import { untrack } from "svelte";
  import { createBlockCache, revealStep, splitCommitted } from '@core/markdown-stream.js';
  import { stripVoiceMarkup } from '@core/voice-markup.js';
  import { renderText } from '../render.js';
  import { lazyHighlight } from '../highlight.js';

  let { text = '', streaming = false } = $props();

  const blocks = createBlockCache(block => renderText(block, false));
  // Text already there when the row mounts (a chat opened mid-turn) shows
  // at once; only growth from here on is paced.
  let shown = $state(untrack(() => text.length));
  let frame = 0;
  let last = 0;

  function tickReveal(at) {
    const dt = last ? Math.min(64, at - last) : 16;
    last = at;
    shown = revealStep(text, shown, dt);
    frame = shown < text.length ? requestAnimationFrame(tickReveal) : 0;
    if (!frame) last = 0;
  }

  $effect(() => {
    const len = text.length;
    if (!streaming || len < shown || document.hidden) { shown = len; return; }
    if (len > shown && !frame) frame = requestAnimationFrame(tickReveal);
  });

  $effect(() => () => { if (frame) cancelAnimationFrame(frame); });

  let parts = $derived(splitCommitted(
    stripVoiceMarkup(text.slice(0, shown), { streaming: streaming || shown < text.length })));
  let tailHtml = $derived(parts.tail.trim() ? renderText(parts.tail, true) : '');
</script>

<div class="body live-body" class:streaming use:lazyHighlight={parts.committed.length}>
  {#each parts.committed as block, i (i)}
    <div class="lv-block">{@html blocks.render(block)}</div>
  {/each}
  {#if tailHtml}<div class="lv-block lv-tail-block">{@html tailHtml}</div>{/if}
</div>

<style>
  .live-body.streaming .lv-tail-block :global(> :last-child)::after {
    content: '▍';
    margin-left: 1px;
    color: var(--washi-low);
    animation: lv-caret 1s steps(2) infinite;
  }
  @keyframes lv-caret { 50% { opacity: 0; } }
  @media (prefers-reduced-motion: reduce) {
    .live-body.streaming .lv-tail-block :global(> :last-child)::after { animation: none; }
  }
</style>
