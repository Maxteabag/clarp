// The SSE connection: one EventSource, every server push, reconnect policy.

import { clog, noteSseEvent, withToken } from '../lib/net.js';
import {
  AgentState, ClientAction, SSEType, Timing,
} from '@core/protocol.js';
import {
  agentSnapshot, app, chipLabel, flash, mirrorFocus, refreshAgentSnapshot,
  rememberUserNotification, setConn, setVersion, syncStatus,
} from './app.svelte.js';
import {
  appendActivity, appendThinking, handleSseEvent, refreshAll, removeLiveThinking, wake,
} from './conversations.svelte.js';
import {
  audio, bumpLastAudioTs, lastAudioTs, PLAYER_ADAPTER_VERSION, scheduler,
  unlockAudio,
} from './audio.svelte.js';

let es = null;
let lastMsgAt = 0;
let staleTimer = null;
let reconnectMs = Timing.SSE_RECONNECT_BASE_MS;

/** Injected by App so this module doesn't import the mic store (which imports
 *  back into send/audio). Set once at startup. */
let hooks = {
  onRecordToggle: () => {},
  stopAgent: () => {},
};
let everOpened = false;

export function setSseHooks(next) {
  hooks = { ...hooks, ...next };
}

export function scheduleReconnect() {
  setConn('dead', Timing.DEAD_OVERLAY_MS);
  if (es) { try { es.close(); } catch (_) {} es = null; }
  if (staleTimer) { clearInterval(staleTimer); staleTimer = null; }
  setTimeout(connectSSE, reconnectMs);
  reconnectMs = Math.min(reconnectMs * 2, Timing.SSE_RECONNECT_MAX_MS);
}

export function connectSSE() {
  setConn('connecting', Timing.DEAD_OVERLAY_MS);
  try { es = new EventSource(withToken('/events')); }
  catch (_) { scheduleReconnect(); return; }
  lastMsgAt = Date.now();

  es.onopen = () => {
    reconnectMs = Timing.SSE_RECONNECT_BASE_MS;
    lastMsgAt = Date.now();
    setConn('live', Timing.DEAD_OVERLAY_MS);
    refreshAgentSnapshot().catch(() => {});
    // Last-Event-ID replays what we missed, but a long gap can outlive the
    // replay window; a delta per loaded chat is cheap and closes it for sure.
    if (everOpened) refreshAll();
    everOpened = true;
  };

  es.onmessage = e => {
    lastMsgAt = Date.now();
    try {
      const ev = JSON.parse(e.data);
      noteSseEvent(ev && ev.type);
      handleEvent(ev);
    } catch (_) {}
  };

  es.onerror = () => scheduleReconnect();

  if (staleTimer) clearInterval(staleTimer);
  staleTimer = setInterval(() => {
    if (Date.now() - lastMsgAt > Timing.SSE_STALE_MS) scheduleReconnect();
  }, Timing.SSE_STALE_CHECK_MS);
}

export function sseIsOpen() {
  return !!es && es.readyState === 1;
}

export function forceReconnect() {
  reconnectMs = Timing.SSE_RECONNECT_BASE_MS;
  scheduleReconnect();
}

function handleEvent(ev) {
  if (ev.type === SSEType.AUDIO && ev.url) {
    const key = ev.name || ev.url;
    const m = String(key).match(/(\d{12,})/);
    const ts = m ? parseInt(m[1], 10) : 0;
    if (ts && ts <= lastAudioTs) { clog('sseSkipOld', key); return; }
    bumpLastAudioTs(ts);
    if (audio.muted) { clog('sseSkipMuted', key); return; }
    const result = scheduler.ingest({
      url: ev.url,
      session: ev.session || '',
      ts,
      clip_id: ev.clip_id || null,
      trace_id: ev.trace_id || '',
      streamable: !!ev.streamable,
      stream_url: ev.stream_url || '',
      // HLS delivery sends playlist_url; the adapter routes it straight to
      // audio.src (iOS plays HLS natively).
      playlist_url: ev.playlist_url || '',
      delivery: ev.delivery || '',
    });
    if (!result.accepted) clog('sseDup', `${key} reason=${result.reason}`);
    else                  clog('sseAudio', `key=${key} ts=${ts}`);

  } else if (ev.type === SSEType.SERVER_VERSION) {
    // Server-pushed reload, faster than waiting for the SW update poll.
    const last = localStorage.getItem('serverVersion');
    localStorage.setItem('serverVersion', ev.version);
    setVersion(ev.version, PLAYER_ADAPTER_VERSION);
    if (last && last !== ev.version) {
      clog('serverVersionChanged', `${last} → ${ev.version}`);
      location.reload();
    }

  } else if (ev.type === SSEType.REMOTE_ACTION) {
    // POST /remote-action from an iOS Shortcut (Action Button). Acts on the
    // running page without a reload.
    clog('remoteAction', ev.action || '');
    if (ev.action === ClientAction.RECORD_TOGGLE || ev.action === ClientAction.RECORD) {
      unlockAudio();
      hooks.onRecordToggle();
    } else if (ev.action === ClientAction.STOP_AGENT) {
      hooks.stopAgent();
    }

  } else if (ev.type === SSEType.AGENT_STATE) {
    agentSnapshot.patchState(ev);
    syncStatus();
    if (ev.kind === AgentState.THINKING) appendThinking(ev.session, chipLabel(ev.session));
    else removeLiveThinking(ev.session);

  } else if (ev.type === SSEType.AGENT_ACTIVITY) {
    agentSnapshot.patchActivity(ev);
    syncStatus();
    appendActivity(ev.session, ev);

  } else if (ev.type === SSEType.TTS_ERROR) {
    // Synthesis failed (e.g. quota) — say so rather than leaving silence
    // with no explanation.
    clog('ttsError', ev.error || ev.message || 'tts failed');
    flash(ev.message || 'Voice synthesis failed', 5000);

  } else if (ev.type === SSEType.AGENT_ROSTER) {
    clog('agentRoster', `${ev.kind}:${ev.session || ''}`);
    if (ev.kind === 'deleted' && ev.session) agentSnapshot.remove(ev.session);
    refreshAgentSnapshot().catch(() => {});
    // Relaunch / fork keeps the session id but the conversation is new; the
    // conversation store's reducer decides what that means for the cache.
    handleSseEvent(ev);
    const isReset = ev.kind === 'relaunched' || ev.kind === 'forked' || ev.kind === 'created';
    if (isReset) hooks.closeOverview?.();

  } else if (ev.type === SSEType.AGENT_FOCUS) {
    mirrorFocus(ev.session || '', ev.agent_id || '');

  } else if (ev.type === SSEType.TRANSCRIPT_UPDATED) {
    handleSseEvent(ev);

  } else if (ev.type === SSEType.USER_NOTIFICATION) {
    rememberUserNotification(ev);
    wake(ev.session);
  }
}
