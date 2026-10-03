// The live channel (docs/live-items.md): a running turn as items pushed over
// SSE instead of "go refetch /log" pings.
//
// Gated on `live_items` in /server-info; without it nothing here subscribes
// and the transcript keeps today's /log path. With it:
//
//   subscribe  /events?live=<chats on screen + the few opened last>
//   open chat  GET /live?session=… once, then apply `live` events above it
//   gap/epoch  the reducer asks for GET /live again (one at a time)
//   other chats  status/turn ops only, kept as each agent's activity
//
// Events are queued and applied once per animation frame, so a burst of
// appends costs one state write per chat per frame. Each chat's state is a
// $state.raw value replaced on change: a pane re-renders only the items the
// reducer replaced.

import {
  applyLiveEvent, applyLiveSnapshot, blankLive, LiveEffects, liveSnapshotFailed,
  requestLiveSnapshot,
} from '@core/live-items.js';
import { SvelteMap, SvelteSet } from 'svelte/reactivity';
import { clog } from '../lib/net.js';

export const LIVE_FEATURE = 'live_items';
export const EXPLANATION_SETTING_FEATURE = 'tool_explanation_setting';
/** Chats kept subscribed besides the ones on screen. */
const MAX_WATCHED = 4;
/** Events held while a snapshot is on its way. */
const MAX_BUFFERED = 1000;

export const live = $state({
  enabled: false,
  /** The Host lets clients switch tool explanations (Host contract 43). */
  explanationSetting: false,
  /** The Host's tool-explanation setting (§6). */
  explanations: { enabled: true, detail_level: 2, loaded: false },
});

class LiveChat {
  state = $state.raw(null);
}

class RosterEntry {
  activity = $state.raw(null);
  turn = $state.raw(null);
}

// Reactive maps: a pane that asked before a chat existed re-reads when it appears.
const chats = new SvelteMap();     // session → LiveChat
// Chats the Host has shown it really streams: a live event for them arrived,
// or GET /live answered with a hub epoch. A Host can list live_items while its
// hub sends nothing; until a chat is proven here it keeps the pre-live
// behaviour (Thinking placeholder, agent-activity status line, /log).
const proven = new SvelteSet();
const roster = new SvelteMap();    // session → RosterEntry
const watched = [];          // most recent last
/** Sessions the open /events connection carries item ops for; null = all watched. */
let connected = null;
const queued = new Map();    // session → events waiting for the next frame
const buffered = new Map();  // session → events that arrived during GET /live
const fetching = new Set();
/** Snapshots in flight that were asked for before the stream carried the chat. */
const staleFetch = new Set();
let frameRequested = false;
let subscriptionsChanged = () => {};
let turnSettled = () => {};

function chatOf(session) {
  let chat = chats.get(session);
  if (!chat) { chat = new LiveChat(); chats.set(session, chat); }
  return chat;
}

function rosterOf(session) {
  let entry = roster.get(session);
  if (!entry) { entry = new RosterEntry(); roster.set(session, entry); }
  return entry;
}

// ---- feature gate and subscriptions ----------------------------------------

/** From /server-info `capabilities.features`. */
export function setServerFeatures(features = []) {
  const list = Array.isArray(features) ? features : [];
  const on = list.includes(LIVE_FEATURE);
  live.explanationSetting = list.includes(EXPLANATION_SETTING_FEATURE);
  if (on === live.enabled) return;
  live.enabled = on;
  if (!on) {
    for (const chat of chats.values()) chat.state = null;
  }
  subscriptionsChanged();
}

/** Called with a session whose running turn just ended. */
export function onTurnSettled(fn) {
  turnSettled = fn || (() => {});
}

export function onSubscriptionsChanged(fn) {
  subscriptionsChanged = fn || (() => {});
}

/** A chat is on screen or was just opened: keep its items coming. */
export function watchSession(session) {
  if (!session) return;
  const at = watched.indexOf(session);
  if (at >= 0) {
    // Already subscribed: only its recency moves, the URL stays.
    watched.splice(at, 1);
    watched.push(session);
    return;
  }
  watched.push(session);
  while (watched.length > MAX_WATCHED) {
    const dropped = watched.shift();
    const chat = chats.get(dropped);
    if (chat) chat.state = null;
  }
  subscriptionsChanged();
}

export function watchedSessions() {
  return [...watched];
}

/**
 * The sessions the open connection subscribed to. Until a connection that
 * asked for a chat is open, the Host sends that chat only status and turn
 * ops, under the same lseq numbers: applying those as full events would
 * skip its item ops.
 */
export function setConnectedLive(sessions) {
  const before = connected || new Set();
  connected = new Set(sessions || []);
  // A snapshot taken before this stream carried the chat may be missing the
  // events that went by meanwhile, and a quiet stream never shows the gap:
  // every chat the stream starts carrying gets a snapshot from now on. One
  // still in flight is asked again when it lands.
  for (const session of connected) {
    if (before.has(session) || !watched.includes(session)) continue;
    if (fetching.has(session)) staleFetch.add(session);
    else openLive(session);
  }
}

const carried = session => (connected ? connected.has(session) : watched.includes(session));

/** The `/events` query for the live channel, '' when it is off. */
export function liveQuery() {
  if (!live.enabled) return '';
  return `live=${[...watched].sort().map(encodeURIComponent).join(',')}`;
}

// ---- reads ----------------------------------------------------------------

/**
 * The live state of a subscribed chat: null before its first snapshot, and
 * null until the Host has proven it streams this chat (see `proven`).
 */
export function liveFor(session) {
  if (!live.enabled || !session || !proven.has(session)) return null;
  return chats.get(session)?.state || null;
}

/** GET /live has been asked for this chat (whether or not it proved live). */
export function liveRequested(session) {
  return !!(live.enabled && session && chats.get(session)?.state);
}

/** The newest status of any agent, subscribed or not; null until proven live. */
export function rosterActivity(session) {
  if (!live.enabled || !proven.has(session)) return null;
  const own = liveFor(session);
  if (own && carried(session)) return own.activity;
  return roster.get(session)?.activity || (own && own.activity) || null;
}

const BUSY = new Set(['thinking', 'responding', 'tool', 'compacting', 'limited']);

/** A turn is open: its turn says running, or its status line is busy. */
export function liveTurnRunning(session) {
  if (!live.enabled || !proven.has(session)) return false;
  const own = liveFor(session);
  const turn = own ? own.turn : roster.get(session)?.turn;
  if (turn && turn.status === 'running') return true;
  const activity = rosterActivity(session);
  return !!activity && BUSY.has(activity.state);
}

// ---- snapshots ------------------------------------------------------------

/** GET /live for a chat, once at a time. Events meanwhile are buffered. */
export async function openLive(session) {
  if (!live.enabled || !session || fetching.has(session)) return;
  const chat = chatOf(session);
  if (!chat.state || !chat.state.awaitingSnapshot) {
    chat.state = requestLiveSnapshot(chat.state || blankLive()).state;
  }
  fetching.add(session);
  try {
    const r = await fetch('/live?session=' + encodeURIComponent(session));
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const snapshot = await r.json();
    let next = applyLiveSnapshot(chat.state || blankLive(), snapshot);
    fetching.delete(session);
    if (snapshot.epoch) proven.add(session);
    if (snapshot.tool_explanations) setExplanationState(snapshot.tool_explanations);
    if (staleFetch.delete(session)) {
      // Taken before the stream carried the chat: show it, keep the events
      // buffered, and take the snapshot again.
      chat.state = requestLiveSnapshot(next).state;
      openLive(session);
      return;
    }
    const held = buffered.get(session) || [];
    buffered.delete(session);
    chat.state = next;
    if (held.length) enqueue(session, held);
  } catch (err) {
    fetching.delete(session);
    if (staleFetch.delete(session)) { openLive(session); return; }
    buffered.delete(session);
    if (chat.state) chat.state = liveSnapshotFailed(chat.state);
    clog('liveSnapshotFail', `${session} ${err && err.message ? err.message : err}`);
  }
}

// ---- events ---------------------------------------------------------------

function enqueue(session, events) {
  const list = queued.get(session) || [];
  list.push(...events);
  queued.set(session, list);
  if (!frameRequested) {
    frameRequested = true;
    const raf = globalThis.requestAnimationFrame || (cb => setTimeout(cb, 16));
    raf(flush);
  }
}

function flush() {
  frameRequested = false;
  const work = [...queued.entries()];
  queued.clear();
  for (const [session, events] of work) applyQueued(session, events);
}

function applyQueued(session, events) {
  const chat = chats.get(session);
  if (!chat || !chat.state) return;
  if (fetching.has(session)) {
    const held = buffered.get(session) || [];
    held.push(...events);
    buffered.set(session, held.slice(-MAX_BUFFERED));
    return;
  }
  const wasRunning = liveTurnRunning(session);
  let state = chat.state;
  let needSnapshot = false;
  for (const ev of events) {
    const r = applyLiveEvent(state, ev);
    state = r.state;
    if (r.effects.includes(LiveEffects.FETCH_LIVE)) { needSnapshot = true; break; }
  }
  if (state !== chat.state) chat.state = state;
  noteRoster(session, state.activity, state.turn, wasRunning);
  if (needSnapshot) openLive(session);
}

function noteRoster(session, activity, turn, wasRunning = liveTurnRunning(session)) {
  const entry = rosterOf(session);
  if (activity && activity !== entry.activity) entry.activity = activity;
  if (turn !== undefined && turn !== entry.turn) entry.turn = turn;
  if (wasRunning && !liveTurnRunning(session)) turnSettled(session);
}

/** A `live` SSE event. */
export function handleLiveEvent(ev) {
  if (!live.enabled || !ev || !ev.session) return;
  const session = ev.session;
  proven.add(session);
  if (watched.includes(session) && carried(session) && chats.get(session)?.state) {
    enqueue(session, [ev]);
    return;
  }
  // Not subscribed: only status and turn ops reach us. Keep the newest.
  for (const op of ev.ops || []) {
    if (op.op === 'status') noteRoster(session, op.activity || null);
    else if (op.op === 'turn') noteRoster(session, rosterOf(session).activity, op.turn || null);
  }
}

// ---- tool explanations (§6) -------------------------------------------------

function setExplanationState(s) {
  if (!s || typeof s !== 'object') return;
  live.explanations = {
    enabled: s.enabled !== false,
    detail_level: Number.isFinite(s.detail_level) ? s.detail_level : live.explanations.detail_level,
    loaded: true,
  };
}

export async function loadExplanationSettings() {
  if (!live.explanationSetting) return;
  try {
    const r = await fetch('/tool-explanations/settings');
    if (r.ok) setExplanationState(await r.json());
  } catch (_) {}
}

/** Flip the Host setting. The switch moves at once and moves back on failure. */
export async function setExplanationsEnabled(enabled) {
  const before = { ...live.explanations };
  live.explanations = { ...before, enabled: !!enabled };
  try {
    const r = await fetch('/tool-explanations/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: !!enabled }),
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    setExplanationState(await r.json());
  } catch (err) {
    live.explanations = before;
    clog('toolExplanationSettingFail', err && err.message ? err.message : String(err));
  }
}
