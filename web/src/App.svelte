<script>
  // Desktop and phone are separate shells rather than one tree full of
  // {#if isDesktop}. They differ in layout, in what the dock holds and in
  // whether a conversation rail exists at all; the pieces they do share
  // (Conversation, DockControls, the dialogs) are imported by both.
  import DesktopShell from './lib/desktop/DesktopShell.svelte';
  import MobileShell from './lib/mobile/MobileShell.svelte';

  import AgentSwitcher from './lib/AgentSwitcher.svelte';
  import OrchestratorDialog from './lib/OrchestratorDialog.svelte';
  import QuickSwitch from './lib/QuickSwitch.svelte';
  import Overview from './lib/Overview.svelte';
  import StartDialog from './lib/StartDialog.svelte';
  import StateInspector from './lib/StateInspector.svelte';
  import StatusToast from './lib/StatusToast.svelte';
  import SvgDefs from './lib/SvgDefs.svelte';
  import VoiceDialog from './lib/VoiceDialog.svelte';

  import {
    app, isDesktop, refreshAgentSnapshot, setVersion,
  } from './stores/app.svelte.js';
  import {
    audio, initAudio, PLAYER_ADAPTER_VERSION, unlockAudio,
  } from './stores/audio.svelte.js';
  import { ensureLoaded, markTurnFailed } from './stores/conversations.svelte.js';
  import { bindDeliveryFailure } from './stores/delivery.svelte.js';
  import {
    resumeAudioContext, teardownMic, toggleRecord,
  } from './stores/mic.svelte.js';
  import { stopAgentTurn } from './stores/send.svelte.js';
  import {
    connectSSE, forceReconnect, scheduleReconnect, setSseHooks, sseIsOpen,
  } from './stores/sse.svelte.js';
  import { composerRef } from './stores/composer.svelte.js';
  import {
    applyQuickChoice, handleGlobalKey, initKeyboard, setOverlay,
  } from './stores/input.svelte.js';
  import { clog, flushClog } from './lib/net.js';

  let playerEl = $state(null);

  let overviewOpen = $state(false);
  let switcherOpen = $state(false);
  let startOpen = $state(false);
  let voiceOpen = $state(false);
  let orchestratorOpen = $state(false);
  let quickOpen = $state(false);

  let startName = $state('');
  let startReplaceSid = $state('');
  let voiceSid = $state('');
  let voiceName = $state('');

  bindDeliveryFailure(markTurnFailed);

  // ---- keyboard ----
  //
  // One window listener for the whole app. Which bindings are live comes from
  // the context in input.svelte.js, so a dialog being open is enough to take
  // the keyboard away from the panes — no component needs its own handler.
  initKeyboard({
    onQuickSwitch: () => { quickOpen = true; },
    onSearch: () => { switcherOpen = true; },
    onOverview: () => { overviewOpen = true; },
    onHelp: () => { overviewOpen = true; },
    onCloseOverlay: (name) => {
      if (name === 'switcher') switcherOpen = false;
      else if (name === 'overview') overviewOpen = false;
      else if (name === 'start') startOpen = false;
      else if (name === 'voice') voiceOpen = false;
      else if (name === 'orchestrator') orchestratorOpen = false;
      else if (name === 'quick') quickOpen = false;
    },
  });

  // Read each flag at the top level: a read buried inside a helper would not
  // register as a dependency and the context would go stale.
  $effect(() => {
    const open = { switcher: switcherOpen, overview: overviewOpen, start: startOpen,
                   voice: voiceOpen, orchestrator: orchestratorOpen, quick: quickOpen };
    for (const [name, isOpen] of Object.entries(open)) setOverlay(name, isOpen);
  });

  // ---- boot ----
  $effect(() => {
    if (!playerEl) return;
    initAudio(playerEl);

    setSseHooks({
      onRecordToggle: toggleRecord,
      stopAgent: stopAgentTurn,
      closeOverview: () => { overviewOpen = false; },
    });

    // Bootstrap per docs/protocol.md: snapshot, then the open chat, then the
    // event stream (which itself refetches the snapshot on connect).
    refreshAgentSnapshot();
    ensureLoaded(app.session);
    connectSSE();

    // Paint a version before any SSE event arrives. The SW caches sw.js, so
    // this fetch is essentially free and reflects what it will activate next.
    fetch('/sw.js', { cache: 'no-store' })
      .then(r => r.text())
      .then(txt => {
        const m = txt.match(/VERSION\s*=\s*['"]([^'"]+)['"]/);
        setVersion(m ? m[1] : '', PLAYER_ADAPTER_VERSION);
      })
      .catch(() => setVersion('', PLAYER_ADAPTER_VERSION));

    handleUrlAction('load');
  });

  // Any pointerdown primes audio. iOS Safari won't autoplay without a recent
  // gesture having activated the element; without this, opening the app and
  // typing rather than pressing the mic leaves audio locked.
  function primeAudio() {
    try { unlockAudio(); } catch (_) {}
  }

  // Desktop only: clicking anywhere parks the caret back in the composer, so
  // typing after scrolling or clicking a transcript row just works. On a
  // phone this would pop the soft keyboard on every tap.
  const FOCUS_KEEPERS =
    'input, textarea, select, [contenteditable=""], [contenteditable="true"]';

  function refocusComposer(e) {
    if (!isDesktop) return;
    if (!composerRef.isVisible()) return;
    if (e.target.closest && e.target.closest(FOCUS_KEEPERS)) return;
    if (startOpen || voiceOpen || orchestratorOpen) return;
    // Don't yank focus mid-selection — that would collapse the highlight.
    const sel = window.getSelection();
    if (sel && !sel.isCollapsed) return;
    composerRef.focus();
  }

  // URL-action entry point: an iOS Shortcut on the Action Button opens
  // https://<host>/?action=record to start recording immediately.
  function handleUrlAction(source) {
    let action = '';
    try { action = new URL(location.href).searchParams.get('action') || ''; }
    catch (_) { return; }
    if (!action) return;
    clog('urlAction', `${source}: ${action}`);
    // Strip the query so a refresh or back-button doesn't re-trigger it.
    // window.history explicitly: `history` in this module is the transcript
    // store, which would otherwise shadow it.
    try { window.history.replaceState(null, '', location.pathname + location.hash); } catch (_) {}
    if (action === 'record') toggleRecord();
  }

  function onVisibilityChange() {
    if (document.visibilityState !== 'visible') return;
    resumeAudioContext();
    // iOS Safari pauses SSE for backgrounded tabs — if the connection isn't
    // open when the page comes forward, force a reconnect.
    if (!sseIsOpen()) forceReconnect();
  }

  // iOS often suspends the capture pipeline when the PWA is backgrounded: the
  // JS state survives but MediaRecorder produces zero bytes on resume.
  function onPageHide() {
    clog('pagehide', 'teardownMic');
    try { teardownMic(); } catch (_) {}
    flushClog();
  }

  function openStart(name, replaceSid = '') {
    startName = name;
    startReplaceSid = replaceSid;
    startOpen = true;
  }

  function openVoice(sid, name) {
    voiceSid = sid;
    voiceName = name;
    voiceOpen = true;
  }
</script>

<svelte:window
  onkeydown={handleGlobalKey}
  onpointerdowncapture={primeAudio}
  onclick={refocusComposer}
  onpagehide={onPageHide}
  onpopstate={() => handleUrlAction('popstate')}
  onpageshow={e => { if (e.persisted) handleUrlAction('pageshow'); }}
/>
<svelte:document onvisibilitychange={onVisibilityChange} />

<SvgDefs />

{#if isDesktop}
  <DesktopShell
    onTapAgent={() => switcherOpen = !switcherOpen}
    onHoldAgent={() => { switcherOpen = false; overviewOpen = true; }}
    onOpenOverview={() => overviewOpen = true}
  />
{:else}
  <MobileShell
    onTapAgent={() => switcherOpen = !switcherOpen}
    onHoldAgent={() => { switcherOpen = false; overviewOpen = true; }}
  />
{/if}

{#if app.authRejected}
  <!-- The server is reachable and refusing every request: the saved token is
       wrong. Waiting or reconnecting cannot fix that, only a fresh link can. -->
  <div class="reconnect auth-rejected" role="alert">
    <span>Server rejected the saved token. Run <code>clarp-admin url</code> on the server and open the link it prints.</span>
  </div>
{:else if app.showReconnect}
  <button class="reconnect" aria-label="Reconnect" onclick={() => {
    app.showReconnect = false;
    scheduleReconnect();
  }}>
    <span>Tap to reconnect</span>
  </button>
{/if}

<StatusToast />

<Overview
  bind:open={overviewOpen}
  onStart={name => openStart(name)}
  onRelaunch={(name, sid) => openStart(name, sid)}
  onVoice={openVoice}
  onOrchestrator={() => orchestratorOpen = true}
/>

<AgentSwitcher bind:open={switcherOpen} />

<StartDialog
  bind:open={startOpen}
  name={startName}
  replaceSid={startReplaceSid}
  onDone={ok => { if (ok) overviewOpen = false; else overviewOpen = true; }}
/>

<VoiceDialog
  bind:open={voiceOpen}
  sid={voiceSid}
  name={voiceName}
  onChanged={() => overviewOpen = true}
/>

<OrchestratorDialog bind:open={orchestratorOpen} />

<QuickSwitch
  bind:open={quickOpen}
  onChoose={decision => applyQuickChoice(decision, name => openStart(name))}
/>

<StateInspector />


<!-- svelte-ignore component_name_lowercase -->
<audio bind:this={playerEl} preload="auto"></audio>
