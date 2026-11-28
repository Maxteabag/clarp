<script>
  // The full Host-down panel over the app, opened from a Host's line in the combined chat list or
  // its settings row. Escape or a click outside closes it; it closes by itself two seconds after
  // the Host answers again (stores/reach.svelte.js).
  import HostDownPanel from './HostDownPanel.svelte';
  import { closePanel, reach } from '../stores/reach.svelte.js';
  import { t } from '../stores/i18n.svelte.js';

  function keydown(e) {
    if (reach.panel && e.key === 'Escape') { closePanel(); e.stopPropagation(); }
  }
</script>

<svelte:window onkeydown={keydown} />

{#if reach.panel && reach.hosts[reach.panel]}
  <div class="hd-overlay" role="presentation" onclick={e => { if (e.target === e.currentTarget) closePanel(); }}>
    <div class="hd-card" role="dialog" aria-modal="true" aria-label={reach.hosts[reach.panel]?.label || ''}>
      <button type="button" class="hd-close" aria-label={t('host_down.close')} title={t('host_down.close')} onclick={closePanel}>
        <svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true">
          <path d="M6 6l12 12M18 6L6 18" stroke="currentColor" stroke-width="2" stroke-linecap="round" />
        </svg>
      </button>
      <HostDownPanel id={reach.panel} full />
    </div>
  </div>
{/if}

<style>
.hd-overlay {
  position: fixed;
  inset: 0;
  z-index: 60;
  display: grid;
  place-items: center;
  padding: 16px;
  background: color-mix(in srgb, var(--ink) 70%, transparent);
}
.hd-card {
  position: relative;
  width: min(480px, 100%);
  border: 1px solid var(--ink-edge);
  border-radius: 14px;
  background: var(--ink-soft);
  box-shadow: 0 12px 40px rgba(0, 0, 0, .25);
}
.hd-close {
  position: absolute;
  top: 8px; right: 8px;
  display: grid; place-items: center;
  width: 28px; height: 28px;
  border: 0; border-radius: 8px;
  background: transparent;
  color: var(--washi-low);
  cursor: pointer;
}
.hd-close:hover { color: var(--washi); }
</style>
