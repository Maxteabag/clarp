<script>
  // A display cell from /log, drawn like a settled live tool row: one line
  // that opens to the cell's detail lines.
  import { isOpen, toggle } from './expanded.svelte.js';

  let { row } = $props();
  let open = $derived(isOpen(row.id));
</script>

<div class="lv-tool tool-cell {row.status}" data-cell-id={row.id}>
  <button class="lv-head" type="button" disabled={!row.lines.length} aria-expanded={row.lines.length ? open : undefined}
          onclick={() => toggle(row.id)}>
    <span class="lv-dot" aria-hidden="true"></span>
    <span class="lv-verb">{row.verb}</span>
    <span class="lv-label">{row.label}</span>
  </button>
  {#if open}
    <pre class="lv-out lv-full">{#each row.lines as line}{line.label ? `${line.label}: ` : ''}{line.text}
{/each}</pre>
  {/if}
</div>
