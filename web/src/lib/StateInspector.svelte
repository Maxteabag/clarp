<script>
  import { app, isDesktop } from '../stores/app.svelte.js';
  import { conversation } from '../stores/conversations.svelte.js';
  import { delivery } from '../stores/delivery.svelte.js';
  import { prefs } from '../stores/prefs.svelte.js';
  import { health } from './net.js';
  import {
    Health, assess, duplicatedModules, formatAge,
  } from '@core/client-health.js';
  import { deliverySummary } from '@core/delivery.js';
  import { audio } from '../stores/audio.svelte.js';
  import { mic } from '../stores/mic.svelte.js';

  let open = $state(false);
  let lastCopied = $state(false);

  // Ticks only while the panel is open: the ages shown are relative to now, so
  // they have to be recomputed, but not when nobody is looking.
  let nowTick = $state(Date.now());
  $effect(() => {
    if (!open) return;
    const t = setInterval(() => { nowTick = Date.now(); }, 1000);
    return () => clearInterval(t);
  });

  let verdict = $derived.by(() => { nowTick; return assess(health, { now: nowTick }); });
  let dupes = $derived.by(() => { nowTick; return duplicatedModules(globalThis); });
  let sends = $derived.by(() => {
    delivery.entries;
    return [...delivery.entries].reverse().slice(0, 8);
  });
  let counts = $derived.by(() => { delivery.entries; return deliverySummary(delivery); });

  function toggle() {
    open = !open;
  }

  function copyState() {
    const snapshot = {
      app: {
        session: app.session,
        conn: app.conn,
        availableSessions: app.availableSessions,
        toast: app.toast,
        version: app.version,
        tick: app.tick,
      },
      conversation: (() => {
        const c = conversation(app.session);
        return {
          session: c.session, status: c.status, error: c.error,
          turnsCount: c.turns.length, activityCount: c.activity.length,
          latestRevision: c.latestRevision, conversationId: c.conversationId,
          hasMore: c.hasMore, hideTools: prefs.hideTools,
        };
      })(),
      audio: {
        muted: audio.muted,
      },
      mic: {
        recording: mic.recording,
      },
      health: { ...health, verdict: assess(health) },
      duplicatedModules: duplicatedModules(globalThis),
      delivery: { counts: deliverySummary(delivery), recent: delivery.entries.slice(-10) },
    };
    navigator.clipboard.writeText(JSON.stringify(snapshot, null, 2));
    lastCopied = true;
    setTimeout(() => { lastCopied = false; }, 2000);
  }

  function handleKeydown(e) {
    if (e.ctrlKey && e.shiftKey && (e.key === 'D' || e.key === 'd')) {
      e.preventDefault();
      toggle();
    }
  }
</script>

<svelte:window onkeydown={handleKeydown} />

<!-- Floating toggle button -->
<button
  class="state-inspector-toggle"
  class:active={open}
  title="Toggle Svelte State Inspector (Ctrl+Shift+D)"
  aria-label="Toggle Svelte State Inspector"
  onclick={toggle}
>
  <span class="inspector-pulse" class:live={app.conn === 'live'}></span>
  <span class="inspector-label">STATE</span>
</button>

{#if open}
  <aside class="state-inspector-panel" aria-label="Live Svelte State Inspector">
    <header class="inspector-header">
      <div class="inspector-title">
        <svg viewBox="0 0 24 24" width="14" height="14" fill="currentColor">
          <path d="M12 2a10 10 0 1 0 10 10A10 10 0 0 0 12 2zm1 14.5h-2v-2h2zm0-4h-2v-6h2z"/>
        </svg>
        <span>Svelte 5 Reactive States</span>
      </div>
      <div class="inspector-actions">
        <button class="inspector-btn" onclick={copyState} title="Copy JSON snapshot">
          {lastCopied ? 'Copied!' : 'Copy JSON'}
        </button>
        <button class="inspector-btn close-btn" onclick={toggle} aria-label="Close">✕</button>
      </div>
    </header>

    <div class="inspector-body">
      <section class="state-group">
        <div class="state-group-title">Session & Navigation</div>
        <div class="state-row">
          <span class="state-key">app.session:</span>
          <span class="state-val highlight">{app.session}</span>
        </div>
        <div class="state-row">
          <span class="state-key">loadedForSession:</span>
          <span class="state-val" class:match={history.loadedForSession === app.session}>
            {history.loadedForSession || '(none)'}
          </span>
        </div>
        <div class="state-row">
          <span class="state-key">app.conn (SSE):</span>
          <span class="state-val status-{app.conn}">{app.conn}</span>
        </div>
        <div class="state-row">
          <span class="state-key">availableSessions:</span>
          <span class="state-val">{app.availableSessions.length} active</span>
        </div>
      </section>

      <section class="state-group">
        <div class="state-group-title">Transcript ($state history)</div>
        <div class="state-row">
          <span class="state-key">turns.length:</span>
          <span class="state-val number">{history.turns.length}</span>
        </div>
        <div class="state-row">
          <span class="state-key">placeholder:</span>
          <span class="state-val" class:warn={history.placeholder.includes('loading')} class:error={history.placeholder.includes('error')}>
            {history.placeholder ? `"${history.placeholder}"` : '"" (clean)'}
          </span>
        </div>
        <div class="state-row">
          <span class="state-key">latestRevision:</span>
          <span class="state-val number">{history.latestRevision}</span>
        </div>
        <div class="state-row">
          <span class="state-key">liveActivity:</span>
          <span class="state-val">{history.activity.length} rows</span>
        </div>
      </section>

      <section class="state-group">
        <div class="state-group-title">Audio & Media</div>
        <div class="state-row">
          <span class="state-key">audio.muted:</span>
          <span class="state-val">{audio.muted ? 'true' : 'false'}</span>
        </div>
        <div class="state-row">
          <span class="state-key">mic.recording:</span>
          <span class="state-val">{mic.recording ? 'true' : 'false'}</span>
        </div>
      </section>

      <section class="state-group">
        <div class="state-group-title">Client health</div>
        <div class="state-row">
          <span class="state-key">server contact:</span>
          <span class="state-val" class:match={verdict.state === Health.OK}
                class:warn={verdict.state === Health.STALE}
                class:error={verdict.state === Health.WEDGED || verdict.state === Health.UNAUTHORIZED}>
            {verdict.state}{verdict.reason ? ` · ${verdict.reason}` : ''}
          </span>
        </div>
        <div class="state-row">
          <span class="state-key">last response:</span>
          <span class="state-val">
            {health.lastFetchAt ? `${formatAge(nowTick - health.lastFetchAt)} ago · ${health.lastFetchPath}` : 'never'}
          </span>
        </div>
        <div class="state-row">
          <span class="state-key">last SSE event:</span>
          <span class="state-val">
            {health.lastSseAt ? `${formatAge(nowTick - health.lastSseAt)} ago · ${health.lastSseType}` : 'never'}
          </span>
        </div>
        <div class="state-row">
          <span class="state-key">requests:</span>
          <span class="state-val number">{health.fetches} ok / {health.fetchErrors} failed · {health.sseEvents} events</span>
        </div>
        {#if dupes.length}
          <div class="state-row">
            <span class="state-key">hot-reload:</span>
            <span class="state-val error">
              {dupes.map(d => `${d.name}×${d.instances}`).join(', ')} — reload, this window is split
            </span>
          </div>
        {/if}
      </section>

      <section class="state-group">
        <div class="state-group-title">
          Delivery ({counts.confirmed} confirmed · {counts.pending + counts.sent} in flight · {counts.failed} failed)
        </div>
        {#each sends as entry (entry.id)}
          <div class="state-row">
            <span class="state-key delivery-state" data-state={entry.state}>{entry.state}</span>
            <span class="state-val">
              {formatAge(nowTick - entry.at)} ago · {entry.session} · “{entry.text.slice(0, 40)}”
              {entry.detail ? ` · ${entry.detail}` : ''}
            </span>
          </div>
        {:else}
          <div class="state-row"><span class="state-val">nothing sent from this window yet</span></div>
        {/each}
      </section>
    </div>
  </aside>
{/if}

<style>
  .state-inspector-toggle {
    position: fixed;
    top: 10px;
    right: 12px;
    z-index: 9999;
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 3px 8px;
    background: rgba(22, 22, 30, 0.85);
    border: 1px solid rgba(255, 255, 255, 0.15);
    border-radius: 4px;
    color: #a9b1d6;
    font-family: monospace;
    font-size: 11px;
    font-weight: 600;
    cursor: pointer;
    backdrop-filter: blur(8px);
    transition: all 0.15s ease;
  }
  .state-inspector-toggle:hover, .state-inspector-toggle.active {
    background: #24283b;
    border-color: #7aa2f7;
    color: #c0caf5;
  }
  .inspector-pulse {
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: #f7768e;
  }
  .inspector-pulse.live {
    background: #9ece6a;
    box-shadow: 0 0 6px rgba(158, 206, 106, 0.6);
  }
  .state-inspector-panel {
    position: fixed;
    top: 38px;
    right: 12px;
    width: 320px;
    max-height: calc(100vh - 50px);
    background: #1a1b26;
    border: 1px solid rgba(122, 162, 247, 0.3);
    border-radius: 6px;
    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.5);
    z-index: 9999;
    font-family: monospace;
    font-size: 11px;
    color: #c0caf5;
    overflow-y: auto;
  }
  .inspector-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 8px 10px;
    background: #24283b;
    border-bottom: 1px solid rgba(255, 255, 255, 0.08);
  }
  .inspector-title {
    display: flex;
    align-items: center;
    gap: 6px;
    font-weight: 700;
    color: #7aa2f7;
  }
  .inspector-actions {
    display: flex;
    gap: 6px;
  }
  .inspector-btn {
    padding: 2px 6px;
    background: rgba(255, 255, 255, 0.08);
    border: 1px solid rgba(255, 255, 255, 0.15);
    border-radius: 3px;
    color: #c0caf5;
    font-size: 10px;
    cursor: pointer;
  }
  .inspector-btn:hover {
    background: rgba(255, 255, 255, 0.15);
  }
  .close-btn {
    padding: 2px 5px;
  }
  .inspector-body {
    padding: 10px;
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .state-group {
    background: rgba(0, 0, 0, 0.2);
    border-radius: 4px;
    padding: 8px;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .state-group-title {
    font-size: 10px;
    font-weight: 700;
    text-transform: uppercase;
    color: #565f89;
    margin-bottom: 2px;
  }
  .state-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 8px;
  }
  .state-key {
    color: #7aa2f7;
  }
  .state-val {
    color: #e0af68;
    word-break: break-all;
    text-align: right;
  }
  .state-val.highlight {
    color: #7dcfff;
    font-weight: 700;
  }
  .state-val.match {
    color: #9ece6a;
  }
  .state-val.number {
    color: #bb9af7;
  }
  .state-val.warn {
    color: #ff9e64;
  }
  .state-val.error {
    color: #f7768e;
  }
  .state-val.status-live {
    color: #9ece6a;
    font-weight: 700;
  }
  .state-val.status-connecting {
    color: #e0af68;
  }
  .state-val.status-dead {
    color: #f7768e;
  }
  .delivery-state {
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }
  .delivery-state[data-state='confirmed'] { color: #9ece6a; }
  .delivery-state[data-state='pending'],
  .delivery-state[data-state='sent'] { color: #e0af68; }
  .delivery-state[data-state='failed'] { color: #f7768e; }
</style>
