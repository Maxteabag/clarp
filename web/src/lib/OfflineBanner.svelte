<script>
  // This device has no network at all (navigator.onLine): one quiet line for the whole app instead
  // of a panel per Host (docs/design/host-not-answering.md).
  import { t } from '../stores/i18n.svelte.js';
  import { reach } from '../stores/reach.svelte.js';
</script>

{#if !reach.deviceOnline}
  <div class="device-offline" role="status" aria-live="polite">
    <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.8"
         stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
      <path d="M7 18h10a4 4 0 0 0 1.6-.3M20.6 15A4 4 0 0 0 17.5 10 6 6 0 0 0 9 5.5M6.3 8.4A4.5 4.5 0 0 0 7 18" />
      <path d="M3 3l18 18" />
    </svg>
    <span>{t('host_down.banner')}</span>
  </div>
{/if}

<style>
.device-offline {
  position: fixed;
  top: calc(env(safe-area-inset-top) + 8px);
  left: 50%;
  transform: translateX(-50%);
  z-index: 55;
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 6px 12px;
  border: 1px solid var(--ink-edge);
  border-radius: 999px;
  background: var(--ink-soft);
  color: var(--washi-dim);
  font: 500 12px/1 var(--font-ui);
  box-shadow: 0 4px 16px rgba(0, 0, 0, .18);
  pointer-events: none;
}
</style>
