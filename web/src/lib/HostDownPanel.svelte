<script>
  // A Host that does not answer (docs/design/host-not-answering.md): which Host, since when, the
  // most likely reason, what to do, and a quiet status line while it is asked again on its own.
  // `full` is the page shown where nothing of that Host is saved (and in the list's dialog);
  // otherwise the compact panel at the top of one of its chats. Never a raw error: that goes
  // only into the diagnostics text.
  import {
    attemptLabel, diagnosticsText, lastSeenAt, reasonAction, reasonBody, reasonIcon, reasonSeverity,
    reasonTitle, statusLine,
  } from '@core/host-reach.js';
  import {
    PRIMARY, deviceNetworkKind, othersAnswering, reach, retry,
  } from '../stores/reach.svelte.js';
  import { hostsState, knownHosts, updateHost } from '../stores/hosts.svelte.js';
  import { app, flash } from '../stores/app.svelte.js';
  import { APP_VERSION, setAuthToken } from './net.js';
  import { t } from '../stores/i18n.svelte.js';
  import { formatCountdown, formatRelative } from './i18n/format.js';

  let { id, full = false } = $props();

  let rec = $derived(reach.hosts[id] || null);
  let label = $derived(rec?.label || 'This Host');
  let back = $derived(!!rec && !rec.down && rec.backUntil > reach.now);
  let action = $derived(rec ? reasonAction(rec.reason) : '');
  let icon = $derived(back ? 'cloud' : reasonIcon(rec?.reason));
  let severity = $derived(back ? 'info' : reasonSeverity(rec?.reason));
  let title = $derived(reasonTitle(rec?.reason, label, t));
  // Pair again and Edit address need something this device can change: its own token for the
  // serving Host, the saved token and address of a Host added here. A peer's are its Host's.
  let saved = $derived(hostsState.saved.find(h => h.id === id) || null);
  let canPair = $derived(id === PRIMARY || !!saved);
  let canEdit = $derived(!!saved);
  // Switch Host: open another Host's own web app, when one is known and answers.
  let switchTo = $derived(id === PRIMARY
    ? knownHosts().filter(h => h.url && !reach.hosts[h.id]?.down) : []);

  let form = $state('');
  let value = $state('');
  let formError = $state('');
  let fallbackText = $state('');

  function openForm(which) {
    formError = '';
    fallbackText = '';
    value = which === 'edit' ? (saved?.url || '') : '';
    form = form === which ? '' : which;
  }

  async function savePair() {
    formError = '';
    if (id === PRIMARY) {
      if (!setAuthToken(value)) { formError = t('host_down.token_needed'); return; }
    } else {
      const err = await updateHost(id, { token: value });
      if (err) { formError = err; return; }
    }
    form = '';
    value = '';
    retry(id);
  }

  async function saveAddress() {
    formError = '';
    const err = await updateHost(id, { url: value });
    if (err) { formError = err; return; }
    form = '';
    retry(id);
  }

  function checkTailscale() {
    window.open('https://login.tailscale.com/admin/machines', '_blank', 'noopener,noreferrer');
  }

  function switchHost(h) {
    window.open(h.url.replace(/\/$/, '') + '/', '_blank', 'noopener,noreferrer');
  }

  function diagnostics() {
    const others = othersAnswering(id);
    return diagnosticsText({
      app: 'web', version: APP_VERSION, build: app.version || '',
      hostKey: rec?.key || rec?.url || '', url: rec?.url || '', rec: rec || undefined,
      othersOk: others.ok, othersTotal: others.total,
      deviceOnline: reach.deviceOnline, network: deviceNetworkKind(), tunnel: null,
    });
  }

  async function copyDiagnostics() {
    const text = diagnostics();
    fallbackText = '';
    try {
      // Only in a secure context (https, localhost); a Host on http://100.x is not one.
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
        flash(t('host_down.copied'));
        return;
      }
    } catch (_) {}
    const area = document.createElement('textarea');
    area.value = text;
    area.setAttribute('readonly', '');
    area.style.position = 'fixed';
    area.style.opacity = '0';
    document.body.appendChild(area);
    area.select();
    let ok = false;
    try { ok = document.execCommand('copy'); } catch (_) {}
    area.remove();
    if (ok) { flash(t('host_down.copied')); return; }
    // Nothing could copy: show the text, selected, for the user's own copy.
    fallbackText = text;
  }

  function selectAll(node) {
    node.select();
  }
</script>

{#if rec && (rec.down || back)}
  <section class="host-down {severity}" class:full class:back role="status" aria-live="polite"
           aria-label={back ? t('host_down.status.back_online') : title}>
    <span class="hd-icon" aria-hidden="true">
      <svg viewBox="0 0 24 24" width={full ? 40 : 22} height={full ? 40 : 22} fill="none"
           stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
        {#if icon === 'moon'}
          <path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z" />
        {:else if icon === 'plug'}
          <path d="M9 3v5M15 3v5M6 8h12v3a6 6 0 0 1-12 0zM12 17v4" />
        {:else if icon === 'key'}
          <circle cx="8" cy="15" r="4" /><path d="M11 12l9-9M17 6l3 3M15 8l2 2" />
        {:else if icon === 'signpost'}
          <path d="M12 3v18M12 5h7l2 2.5-2 2.5h-7M12 12H5l-2 2.5L5 17h7" />
        {:else if icon === 'network'}
          <circle cx="12" cy="5" r="2" /><circle cx="5" cy="19" r="2" /><circle cx="19" cy="19" r="2" />
          <path d="M12 7v5M12 12l-6 5.5M12 12l6 5.5" />
        {:else if icon === 'cloud'}
          <path d="M7 18h10a4 4 0 0 0 .5-8A6 6 0 0 0 6 9a4.5 4.5 0 0 0 1 9z" /><path d="M9.5 13.5l2 2 3.5-4" />
        {:else}
          <path d="M7 18h10a4 4 0 0 0 1.6-.3M20.6 15A4 4 0 0 0 17.5 10 6 6 0 0 0 9 5.5M6.3 8.4A4.5 4.5 0 0 0 7 18" />
          <path d="M3 3l18 18" />
        {/if}
      </svg>
    </span>
    <div class="hd-body">
      {#if back}
        <p class="hd-status-line"><span class="hd-dot ok"></span>{t('host_down.status.back_online')}</p>
      {:else}
        <h2 class="hd-title">{title}</h2>
        <p class="hd-reason">{reasonBody(rec.reason, label, t)}</p>
        <p class="hd-meta">
          {#if rec.lastOk}<span>{t('host_down.last_seen', { when: formatRelative(rec.lastOk, reach.now) })}</span><span class="hd-sep">·</span>{/if}
          <span class="hd-status-line"><span class="hd-dot" class:pulse={rec.inFlight}></span>{statusLine(rec, reach.now, t, formatCountdown)}</span>
        </p>
        <div class="hd-actions">
          <button type="button" class="hd-btn primary" onclick={() => retry(id)}>{t('host_down.action.retry')}</button>
          {#if action === 'pair' && canPair}
            <button type="button" class="hd-btn" aria-expanded={form === 'pair'} onclick={() => openForm('pair')}>{t('host_down.action.pair_again')}</button>
          {:else if action === 'edit' && canEdit}
            <button type="button" class="hd-btn" aria-expanded={form === 'edit'} onclick={() => openForm('edit')}>{t('host_down.action.edit_address')}</button>
          {:else if action === 'tailscale'}
            <button type="button" class="hd-btn" onclick={checkTailscale}>{t('host_down.action.check_tailscale')}</button>
          {/if}
          {#if switchTo.length}
            <button type="button" class="hd-btn" aria-expanded={form === 'switch'}
                    onclick={() => switchTo.length === 1 ? switchHost(switchTo[0]) : openForm('switch')}>{t('host_down.action.switch_host')}</button>
          {/if}
          <button type="button" class="hd-btn" onclick={copyDiagnostics}>{t('host_down.copy_diagnostics')}</button>
        </div>
        {#if form === 'pair'}
          <form class="hd-form" onsubmit={e => { e.preventDefault(); savePair(); }}>
            <input type="password" autocomplete="off" bind:value aria-label={t('host_down.action.pair_again')}
                   placeholder={id === PRIMARY ? t('host_down.token_or_link') : t('host_down.token')} />
            <button type="submit" class="hd-btn" disabled={!value.trim()}>{t('host_down.action.pair_again')}</button>
          </form>
        {:else if form === 'edit'}
          <form class="hd-form" onsubmit={e => { e.preventDefault(); saveAddress(); }}>
            <input type="url" autocomplete="off" bind:value aria-label={t('host_down.action.edit_address')} />
            <button type="submit" class="hd-btn" disabled={!value.trim()}>{t('host_down.action.edit_address')}</button>
          </form>
        {:else if form === 'switch'}
          <div class="hd-switch">
            {#each switchTo as h (h.id)}
              <button type="button" class="hd-btn" onclick={() => switchHost(h)}>{h.name || h.url}</button>
            {/each}
          </div>
        {/if}
        {#if formError}<p class="hd-error" role="alert">{formError}</p>{/if}
        {#if rec.attempt}
          <!-- The raw detail of the last attempt, closed by default; never the message itself. -->
          <details class="hd-details">
            <summary>{t('host_down.details')}</summary>
            <code>{attemptLabel(rec.attempt)}</code>
          </details>
        {/if}
        {#if fallbackText}
          <textarea class="hd-diag" readonly rows="11" use:selectAll>{fallbackText}</textarea>
        {/if}
      {/if}
    </div>
  </section>
{/if}

<style>
/* Calm, not alarming: the app's own neutral surfaces and text, no red, no big spinner. */
.host-down {
  display: flex;
  gap: 12px;
  align-items: flex-start;
  margin: 8px 12px 4px;
  padding: 12px 14px;
  border: 1px solid var(--ink-edge);
  border-radius: 12px;
  background: var(--ink-soft);
  color: var(--washi-dim);
  font: 400 13px/1.4 var(--font-ui);
}
.host-down.back { align-items: center; padding-block: 8px; }
.host-down.full {
  flex-direction: column;
  align-items: center;
  text-align: center;
  gap: 14px;
  margin: auto;
  max-width: 440px;
  padding: 28px 24px;
  border: 0;
  background: transparent;
}
.hd-icon { flex: 0 0 auto; color: var(--washi-low); display: grid; place-items: center; }
/* Shared with turn errors: info grey for most reasons, warning yellow where the user must act
   (auth_expired, wrong_address). Never error red. */
.host-down { --hd-warn: var(--current-ring); }
.host-down.warning:not(.full) { border-color: color-mix(in srgb, var(--hd-warn) 55%, var(--ink-edge)); }
.host-down.warning .hd-icon { color: var(--hd-warn); }
.host-down.warning .hd-btn.primary { border-color: color-mix(in srgb, var(--hd-warn) 60%, var(--ink-edge)); }
.hd-body { min-width: 0; display: flex; flex-direction: column; gap: 4px; }
.full .hd-body { align-items: center; gap: 8px; }
.hd-title { margin: 0; font: 600 14px/1.3 var(--font-ui); color: var(--washi); }
.full .hd-title { font-size: 18px; }
.hd-reason { margin: 0; color: var(--washi-dim); }
.full .hd-reason { font-size: 14px; }
.hd-meta { margin: 0; display: flex; flex-wrap: wrap; gap: 6px; align-items: center; color: var(--washi-low); font-size: 12px; }
.full .hd-meta { justify-content: center; }
.hd-sep { opacity: .6; }
.hd-status-line { margin: 0; display: inline-flex; gap: 6px; align-items: center; color: var(--washi-low); font-size: 12px; }
.hd-dot { width: 6px; height: 6px; border-radius: 50%; background: var(--washi-low); flex: 0 0 auto; }
.hd-dot.ok { background: var(--moss); }
.hd-dot.pulse { animation: hd-pulse 1.2s ease-in-out infinite; }
@keyframes hd-pulse { 50% { opacity: .25; } }
@media (prefers-reduced-motion: reduce) { .hd-dot.pulse { animation: none; } }
.hd-actions { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 6px; }
.full .hd-actions { justify-content: center; margin-top: 10px; }
.hd-btn {
  padding: 6px 10px;
  border: 1px solid var(--ink-edge);
  border-radius: 8px;
  background: transparent;
  color: var(--washi-dim);
  font: 500 12px/1 var(--font-ui);
  cursor: pointer;
  transition: color var(--t-snap) var(--ease), border-color var(--t-snap) var(--ease);
}
.hd-btn:hover:not(:disabled) { color: var(--washi); border-color: var(--ink-edge-hi, var(--washi-low)); }
.hd-btn:disabled { opacity: .45; cursor: default; }
.hd-btn.primary { color: var(--washi); border-color: var(--washi-low); }
.hd-form, .hd-switch { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 6px; }
.full .hd-form, .full .hd-switch { justify-content: center; }
.hd-form input {
  flex: 1 1 200px;
  min-width: 0;
  padding: 6px 8px;
  border: 1px solid var(--ink-edge);
  border-radius: 8px;
  background: var(--ink);
  color: var(--washi);
  font: 400 12px/1.2 var(--font-ui);
}
.hd-error { margin: 4px 0 0; color: var(--washi-dim); font-size: 12px; }
.hd-details { margin-top: 6px; color: var(--washi-low); font-size: 11px; }
.full .hd-details { align-self: center; }
.hd-details summary { cursor: pointer; }
.hd-details code { display: block; margin-top: 4px; font: 400 11px/1.35 var(--font-mono); color: var(--washi-dim); user-select: text; }
.hd-diag {
  width: 100%;
  margin-top: 6px;
  padding: 8px;
  border: 1px solid var(--ink-edge);
  border-radius: 8px;
  background: var(--ink);
  color: var(--washi-dim);
  font: 400 11px/1.35 var(--font-ui);
  resize: none;
}
</style>
