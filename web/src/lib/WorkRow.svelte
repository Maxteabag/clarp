<script>
  // A settled turn's work behind one row, 'Worked for 11s · 3 tools' (static/lib/history-fold.js);
  // it opens in place to the tool calls it took from the turn's rows. Shares its open state with
  // the live fold of the same turn (key `fold:<turn id>`).
  import { cellRow } from '@core/live-present.js';
  import CellRow from './live/CellRow.svelte';
  import { isOpen, toggle } from './live/expanded.svelte.js';
  import { renderText, renderTool } from './render.js';
  import { workLabel } from '@core/history-fold.js';
  import { t } from '../stores/i18n.svelte.js';
  import { formatDuration } from './i18n/format.js';

  let { fold } = $props();

  let open = $derived(isOpen(fold.key));
  let label = $derived(fold.work ? workLabel(fold.work, { tr: t, duration: formatDuration }) : fold.label);
  let calls = $derived(fold.calls || []);
</script>

<div class="turn work-row" class:running={fold.running}>
  {#if calls.length}
    <button class="lv-fold" type="button" aria-expanded={open} onclick={() => toggle(fold.key)}>
      <span class="lv-caret" class:open aria-hidden="true">▸</span>{label}
    </button>
  {:else}
    <!-- Nothing to open (every call failed and stays in its row): the line alone. -->
    <span class="lv-fold">{label}</span>
  {/if}
  {#if open && calls.length}
    <div class="work-row-calls">
      {#each calls as call (call.rowId)}
        {#if call.text}<div class="work-row-text">{@html renderText(call.text)}</div>{/if}
        {#each call.cells.map(cellRow).filter(Boolean) as cell (cell.id)}<CellRow row={cell} />{/each}
        {@html call.tools.map(renderTool).join('')}
      {/each}
    </div>
  {/if}
</div>
