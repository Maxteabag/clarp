<script>
  import { t } from '../stores/i18n.svelte.js';
  import { app } from '../stores/app.svelte.js';
</script>

<span id="conn" class="conn-dot {app.conn}" title={app.conn} aria-label={t('conn.label')}></span>
