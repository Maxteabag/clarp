<script>
  import { app } from '../stores/app.svelte.js';
</script>

<div class="status" class:show={!!app.toast} aria-live="polite">{app.toast}</div>
