<script>
  // Where messages between agents were left out of the chat (docs/protocol.md, Hidden
  // coordination): '3 messages between agents', '100+ ...' when capped, and the chat's Show,
  // which reads the chat with them for this visit.
  import { countAgents, countLine } from '@core/coordination.js';
  import { t } from '../stores/i18n.svelte.js';
  import { formatList } from './i18n/format.js';

  let { count, onShow } = $props();

  let line = $derived(countLine(count, t));
  let agents = $derived(countAgents(count, t, formatList));
</script>

{#if line}
  <div class="turn hidden-run" role="note">
    <span class="hidden-run-text">{line}{#if agents}<span class="hidden-run-agents">{' ' + agents}</span>{/if}</span>
    <button class="hidden-run-show" type="button" onclick={onShow}>{t('hidden_run.show')}</button>
  </div>
{/if}
