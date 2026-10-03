<script>
  // One tool call, updated in place from start to finish: verb + label +
  // elapsed on the first line, the explanation (or the raw command) on a
  // reserved second line, and a short output tail with a count of the rest.
  import { isOpen, toggle } from './expanded.svelte.js';

  let { row, explanations = true } = $props();

  let open = $derived(isOpen(row.id));
  let output = $derived(row.item.tool && row.item.tool.output);
  let input = $derived(row.item.tool && row.item.tool.input_preview);
  let canOpen = $derived(!!(row.diffPreview || (output && output.tail && output.tail.length)
    || (input && Object.keys(input).length)));
  // The second line is reserved while an explanation may still arrive, so
  // it never changes the row's height when it lands. A settled row with
  // nothing to say there gives the line back.
  let explain = $derived(row.item.tool && row.item.tool.explain);
  let reserve = $derived(!!row.secondary
    || (explanations && (row.running || (explain && explain.status === 'pending'))));
</script>

<div class="lv-tool {row.status}" data-item-id={row.id}>
  <button class="lv-head" type="button" disabled={!canOpen} aria-expanded={canOpen ? open : undefined}
          onclick={() => toggle(row.id)}>
    <span class="lv-dot" aria-hidden="true"></span>
    <span class="lv-verb">{row.verb}</span>
    <span class="lv-label">{row.label}</span>
    {#if row.diff}<span class="lv-diff">{#each row.diff.split(' ') as part}<span class:add={part.startsWith('+')} class:del={!part.startsWith('+')}>{part}</span>{/each}</span>{/if}
    {#if row.exit}<span class="lv-exit">{row.exit}</span>{/if}
    {#if row.status === 'interrupted'}<span class="lv-exit">interrupted</span>{/if}
    {#if row.elapsed}<span class="lv-time">{row.elapsed}</span>{/if}
  </button>
  {#if reserve}<div class="lv-sub" title={row.secondary}>{row.secondary}</div>{/if}
  {#if open}
    {#if row.diffPreview}
      <pre class="lv-out lv-diffview">{#each row.diffPreview.split('\n') as line}<span class:add={line.startsWith('+')} class:del={line.startsWith('-')} class:hunk={line.startsWith('@@')}>{line}
</span>{/each}</pre>
    {:else if output && output.tail && output.tail.length}
      <pre class="lv-out lv-full">{output.tail.join('\n')}</pre>
      {#if output.truncated}<div class="lv-more">+{output.total_lines - output.tail.length} earlier lines</div>{/if}
    {:else if input}
      <pre class="lv-out">{JSON.stringify(input, null, 2)}</pre>
    {/if}
  {:else if row.tail.length}
    <pre class="lv-out">{row.tail.join('\n')}</pre>
    {#if row.more}<div class="lv-more">+{row.more} lines</div>{/if}
  {/if}
</div>
