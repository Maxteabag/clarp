// The SSE connection: one EventSource, every server push, reconnect policy.

import { clog, noteSseEvent, withToken } from '../lib/net.js';
import {
  AgentState, ClientAction, SSEType, Timing,
} from '@core/protocol.js';
import { createCoalescedRefresh } from '@core/snapshot-refresh.js';
import {
  agentSnapshot, app, chipLabel, flash, mirrorFocus, refreshAgentSnapshot,
  rememberUserNotification, setConn, setVersion, syncStatus,
} from './app.svelte.js';
import {
  appendActivity, appendThinking, handleSseEvent, refreshAll, removeLiveThinking, wake,
} from './conversations.svelte.js';
import {
  handleLiveEvent, liveFor, liveQuery, liveTurnRunning, loadExplanationSettings,
  onSubscriptionsChanged, onTurnSettled, openLive, setConnectedLive, setServerFeatures,
  watchedSessions,
} from './live.svelte.js';
import {
  audio, bumpLastAudioTs, lastAudioTs, PLAYER_ADAPTER_VERSION, scheduler,
  unlockAudio,
  addConditionSource,
} from './audio.svelte.js';

let es = null;
let lastMsgAt = 0;
/** The newest SSE id seen, so a reconnect resumes instead of replaying a window. */
let lastEventId = '';
/** Set while we reopen on purpose (the live subscription changed). */
let resubscribing = false;
/** Agents whose transcript changed mid-turn; their snapshot refresh waits for the turn to end. */
const snapshotOwed = new Set();
let staleTimer = null;
let reconnectMs = Timing.SSE_RECONNECT_BASE_MS;
const transcriptSnapshotRefresh = createCoalescedRefresh(
  () => refreshAgentSnapshot(),
);

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

function eventsUrl() {
  const params = [];
  const live = liveQuery();
  if (live) params.push(live);
  if (lastEventId) params.push('last_event_id=' + encodeURIComponent(lastEventId));
  return '/events' + (params.length ? '?' + params.join('&') : '');
}

/**
 * /server-info decides whether the live channel exists (docs/live-items.md
 * §7). Asked at boot and after every reconnect, since a Host restart can
 * bring the feature with it.
 */
export async function refreshServerInfo() {
  try {
    const r = await fetch('/server-info');
    if (!r.ok) return;
    const info = await r.json();
    const features = (info && info.capabilities && info.capabilities.features) || [];
    setServerFeatures(features);
    if (features.includes('live_items')) loadExplanationSettings();
  } catch (_) {}
}

// A turn ended: the snapshot refresh its transcript pings held back is due.
onTurnSettled(session => {
  if (snapshotOwed.delete(session)) transcriptSnapshotRefresh.schedule();
});

// The live subscription is part of the URL, so a changed set of chats means
// reopening the stream. Other events resume from the last id, and live
// events are never replayed: each watched chat takes a fresh GET /live.
// Changes in one burst (the feature arriving, then the open chat) share one
// reconnect.
let resubscribeTimer = null;
onSubscriptionsChanged(() => {
  if (resubscribeTimer) return;
  resubscribeTimer = setTimeout(() => {
    resubscribeTimer = null;
    if (!es) return;
    resubscribing = true;
    try { es.close(); } catch (_) {}
    es = null;
    connectSSE();
  }, 50);
});

export function connectSSE() {
  // Replace, never add: every open stream holds one of the browser's six
  // HTTP/1.1 connections to the Host and handles every event again.
  if (es) { try { es.close(); } catch (_) {} es = null; }
  if (!resubscribing) setConn('connecting', Timing.DEAD_OVERLAY_MS);
  // Until this connection is open, the chats it adds are carried by none.
  setConnectedLive([]);
  const carries = liveQuery() ? watchedSessions() : [];
  try { es = new EventSource(withToken(eventsUrl())); }
  catch (_) { scheduleReconnect(); return; }
  lastMsgAt = Date.now();

  es.onopen = () => {
    reconnectMs = Timing.SSE_RECONNECT_BASE_MS;
    lastMsgAt = Date.now();
    setConn('live', Timing.DEAD_OVERLAY_MS);
    const planned = resubscribing;
    resubscribing = false;
    // The store fetches GET /live again for every chat this stream now
    // carries, including one fetched while the stream was reconnecting.
    setConnectedLive(carries);
    if (planned) return;
    refreshServerInfo();
    refreshAgentSnapshot().catch(() => {});
    // Last-Event-ID replays what we missed, but a long gap can outlive the
    // replay window; a delta per loaded chat is cheap and closes it for sure.
    if (everOpened) refreshAll();
    everOpened = true;
  };

  es.onmessage = e => {
    lastMsgAt = Date.now();
    if (e.lastEventId) lastEventId = e.lastEventId;
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

addConditionSource(() => ({
  sse_open: sseIsOpen(),
  sse_ready_state: es ? es.readyState : null,
}));

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

  } else if (ev.type === SSEType.LIVE) {
    handleLiveEvent(ev);

  } else if (ev.type === SSEType.AGENT_STATE) {
    agentSnapshot.patchState(ev);
    syncStatus();
    // Live items already show thinking and tools in place for this chat.
    if (liveFor(ev.session)) removeLiveThinking(ev.session);
    else if (ev.kind === AgentState.THINKING) appendThinking(ev.session, chipLabel(ev.session));
    else removeLiveThinking(ev.session);
    if (!AgentState.BUSY.has(ev.kind) && snapshotOwed.delete(ev.session)) {
      transcriptSnapshotRefresh.schedule();
    }

  } else if (ev.type === SSEType.AGENT_ACTIVITY) {
    agentSnapshot.patchActivity(ev);
    syncStatus();
    if (!liveFor(ev.session)) appendActivity(ev.session, ev);

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
    // Transcript wakeups cover both user-directed replies and noisy tool or
    // automation imports. Re-read the canonical snapshot so chat ordering is
    // driven by the server's provenance-aware message clock; agent-state and
    // activity events never mutate last_activity locally.
    // With the live channel a streaming turn pings this many times a
    // second; the agent list learns what it needs from status ops, so the
    // refresh waits until the turn ends.
    if (liveTurnRunning(ev.session)) snapshotOwed.add(ev.session);
    else transcriptSnapshotRefresh.schedule();
    handleSseEvent(ev);

  } else if (ev.type === SSEType.USER_NOTIFICATION) {
    rememberUserNotification(ev);
    wake(ev.session);
  }
}
