<script>
  import { t } from '../stores/i18n.svelte.js';
  // The Host a chat is on: a small neutral capsule (static/lib/host-badge.js decides when).
  let { name = '' } = $props();
</script>

{#if name}<span class="host-badge" title={t('host_badge.on', { host: name })}>{name}</span>{/if}
