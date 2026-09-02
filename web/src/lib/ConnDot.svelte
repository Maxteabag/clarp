<script>
  import { app } from '../stores/app.svelte.js';
</script>

<span id="conn" class="conn-dot {app.conn}" title={app.conn} aria-label="Connection"></span>
