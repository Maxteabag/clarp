<script>
  // What the Host says about a turn it ended (a failure, a restart): a
  // notice in the chat, never styled as the agent's own reply.
  let { message, detail = '' } = $props();
</script>

<div class="turn-notice" role="status">
  <span class="tn-glyph" aria-hidden="true">!</span>
  <div class="tn-body">
    <div class="tn-message">{message}</div>
    {#if detail}<div class="tn-detail">{detail}</div>{/if}
  </div>
</div>
