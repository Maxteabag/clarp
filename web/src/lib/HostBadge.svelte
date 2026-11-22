<script>
  // The Host a chat is on: a small neutral capsule (static/lib/host-badge.js decides when).
  let { name = '' } = $props();
</script>

{#if name}<span class="host-badge" title={`On ${name}`}>{name}</span>{/if}
