// One conversation per agent, kept in sync with the server exactly the way
// docs/protocol.md describes. The decisions live in the pure reducer
// (@core/conversation-sync.js, the one the golden fixtures in
// contract/fixtures run against); this store is its adapter. It owns the
// Svelte state the panes read, turns effects into /log requests, and adds
// what only a UI needs: optimistic bubbles, live activity rows, scroll
// pinning.
//
//   open  → onOpen → fetch_tail when nothing is cached, else a delta
//   wake  → onEvent(transcript-updated) → fetch_delta, coalesced in flight
//   /log  → applyLog → upsert by id; replace_required or a new
//           conversation_id → drop_cache + fetch_tail
//   optimistic user turn keyed `u-<client_msg_id>` → shown until the server
//   returns a turn with the same id
//
// Every pane reads its own session from here, so two panes never share a
// transcript and switching back to an agent paints from memory.

import { isLoggableActivity } from '@core/activity-view-model.js';
import {
  Effects, applyLog, applySnapshot, beginFetch, blankSync, endFetch, onEvent,
  onOpen, requestDelta, visibleTurns,
} from '@core/conversation-sync.js';
import { AgentState, SSEType } from '@core/protocol.js';
import { registerModule } from '@core/client-health.js';
import { clog, instanceId } from '../lib/net.js';
import { delivery } from './delivery.svelte.js';
import { confirmFromTurns } from '@core/delivery.js';

registerModule(globalThis, 'conversations', instanceId('conversations'));

const PAGE = 100;
/** Activity rows past this are dropped; the server caps a fetch at 100 turns. */
const MAX_ACTIVITY_ROWS = 80;
const WAKE_DEBOUNCE_MS = 100;

export const conversations = $state({
  /** session → ConversationState (the view the panes render) */
  bySession: {},
});

/**
 * Reducer state per session. Plain objects outside $state: the reducer
 * returns fresh ones on every step and only the projection is reactive.
 */
const syncStates = new Map();

function syncOf(session) {
  let s = syncStates.get(session);
  if (!s) {
    s = blankSync(session);
    syncStates.set(session, s);
  }
  return s;
}

function setSync(session, state) {
  syncStates.set(session, state);
}

function blank(session) {
  return {
    session,
    turns: [],
    /** Optimistic user bubbles the server has not filed yet. */
    optimistic: [],
    activity: [],
    conversationId: '',
    latestRevision: 0,
    latestTs: '',
    hasMore: false,
    missing: false,
    cwd: '',
    /** 'empty' (never fetched) | 'loading' | 'ready' | 'error' */
    status: 'empty',
    error: '',
    /** Bumped whenever new turns are appended, for scroll pinning. */
    appendSeq: 0,
  };
}

const EMPTY = Object.freeze(blank(''));

/**
 * Read the state for a session. Safe inside $derived: it never writes, so a
 * session nobody has opened reads as an empty, never-fetched conversation.
 */
export function conversation(session) {
  return (session && conversations.bySession[session]) || EMPTY;
}

/** The writable entry for a session, created on first use. */
function entry(session) {
  if (!conversations.bySession[session]) {
    conversations.bySession[session] = blank(session);
  }
  return conversations.bySession[session];
}

export function placeholderFor(conv) {
  if (!conv) return '';
  if (conv.status === 'loading' && !conv.turns.length) return 'loading…';
  if (conv.status === 'error') return `error: ${conv.error || 'failed to load'}`;
  if (conv.status === 'ready' && !conv.turns.length) {
    return `${conv.missing ? 'no conversation yet in' : 'no messages in'} ${conv.cwd || ''}`.trim();
  }
  return '';
}

// ---- projection -------------------------------------------------------------

/**
 * Paint the reducer state into the pane's view. `bump` says when the scroll
 * pin should follow: 'always' for a fresh tail, 'appended' when a delta added
 * a turn, 'never' for older history prepended above the fold.
 */
function project(session, { bump = 'appended', resetActivity = false } = {}) {
  const conv = entry(session);
  const sync = syncOf(session);
  const view = visibleTurns(sync, conv.optimistic);
  const appended = view.turns.length > conv.turns.length;
  conv.turns = view.turns;
  conv.optimistic = view.optimistic;
  conv.conversationId = sync.conversationId;
  conv.latestRevision = sync.cursor;
  if (sync.latestTs) conv.latestTs = sync.latestTs;
  conv.hasMore = sync.hasMore;
  conv.missing = sync.missing;
  conv.status = 'ready';
  conv.error = '';
  if (resetActivity) conv.activity = [];
  if (bump === 'always' || (bump === 'appended' && appended)) {
    removeLiveThinking(session);
    conv.appendSeq++;
  }
  confirmFromTurns(delivery, conv.turns);
}

/** Effects the reducer asked for, outside a running request. */
function runEffects(session, effects) {
  for (const fx of effects) {
    if (fx === Effects.DROP_CACHE) dropCache(session);
    else if (fx === Effects.FETCH_TAIL) reload(session).catch(() => {});
    else if (fx === Effects.FETCH_DELTA) refresh(session).catch(() => {});
    else if (fx === Effects.FETCH_OLDER) loadOlder(session).catch(() => {});
  }
}

function dropCache(session) {
  const cur = syncOf(session);
  const next = blankSync(session);
  // A request already in flight stays in flight; the cache is what resets.
  next.pendingFetch = cur.pendingFetch;
  next.wakeWhileFetching = cur.wakeWhileFetching;
  setSync(session, next);
  conversations.bySession[session] = blank(session);
}

// ---- fetching -------------------------------------------------------------
//
// One request per session at a time. A wake-up that arrives mid-flight is
// remembered by the reducer (requestDelta) and endFetch turns it into one
// follow-up delta, so a burst of transcript-updated events costs at most two
// round trips.

const inflight = new Map();   // session → Promise
const wakeTimers = new Map();

async function fetchLog(session, params) {
  const qs = new URLSearchParams({ session, limit: String(PAGE), include_automated: '0', ...params });
  const r = await fetch('/log?' + qs.toString());
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}

function run(session, kind, job) {
  if (inflight.has(session)) {
    setSync(session, requestDelta(syncOf(session)).state);
    return inflight.get(session);
  }
  setSync(session, beginFetch(syncOf(session), kind).state);
  const p = (async () => {
    try {
      await job();
    } finally {
      inflight.delete(session);
      const r = endFetch(syncOf(session));
      setSync(session, r.state);
      // The follow-up runs for a healthy conversation (a delta) or one whose
      // cache was dropped mid-flight (a tail). A failed load waits for the
      // user to reopen or refresh the chat instead of retrying in a loop.
      if (r.effects.includes(Effects.FETCH_DELTA)) {
        const status = conversations.bySession[session]?.status;
        if (status === 'ready') refresh(session).catch(() => {});
        else if (status === 'empty') reload(session).catch(() => {});
      }
    }
  })();
  inflight.set(session, p);
  return p;
}

/** Fetch and apply a tail snapshot inside a running job. */
async function loadTail(session) {
  const d = await fetchLog(session, {});
  const r = applyLog(syncOf(session), d, 'tail');
  setSync(session, r.state);
  entry(session).cwd = d.cwd || '';
  project(session, { bump: 'always', resetActivity: true });
}

/** Load the newest page, replacing whatever is cached. */
export function reload(session) {
  if (!session) return Promise.resolve();
  return run(session, 'tail', async () => {
    const conv = entry(session);
    conv.status = 'loading';
    conv.error = '';
    const t0 = performance.now();
    try {
      await loadTail(session);
      clog('conversationLoad', `${session} turns=${conv.turns.length} rev=${conv.latestRevision} dur=${Math.round(performance.now() - t0)}ms`);
    } catch (err) {
      conv.status = 'error';
      conv.error = err && err.message ? err.message : 'failed to load';
      clog('conversationLoadError', `${session} ${conv.error}`);
    }
  });
}

/** Load once; a cached conversation is refreshed with a delta instead. */
export function ensureLoaded(session) {
  if (!session) return Promise.resolve();
  const conv = entry(session);
  if (conv.status === 'error') return reload(session);
  const r = onOpen(syncOf(session));
  return r.effects.includes(Effects.FETCH_TAIL) ? reload(session) : refresh(session);
}

/** Pull every change after the cursor. */
export function refresh(session) {
  if (!session) return Promise.resolve();
  const conv = entry(session);
  if (conv.status === 'error') return Promise.resolve();
  if (conv.status === 'empty') return reload(session);
  return run(session, 'delta', async () => {
    try {
      const cursor = syncOf(session).cursor || 0;
      const d = await fetchLog(session, { after_revision: String(cursor) });
      const r = applyLog(syncOf(session), d, 'delta');
      setSync(session, r.state);
      if (r.effects.includes(Effects.DROP_CACHE)) {
        clog('conversationReplace', `${session} ${d.replace_required ? 'replace_required' : 'new conversation'}`);
        // Optimistic bubbles stay: the server may still be filing them.
        await loadTail(session);
        return;
      }
      project(session);
      if (r.effects.includes(Effects.FETCH_DELTA)) {
        // has_more: the backlog continues after this request settles.
        setSync(session, requestDelta(syncOf(session)).state);
      }
    } catch (_) {
      // Network blips are silent: the next wake-up retries.
    }
  });
}

/** Older history before the first loaded turn. */
export function loadOlder(session) {
  const conv = conversations.bySession[session];
  if (!conv) return Promise.resolve();
  const oldest = conv.turns[0];
  if (!oldest || !conv.hasMore) return Promise.resolve();
  return run(session, 'older', async () => {
    try {
      const d = await fetchLog(session, { before: oldest.id });
      const r = applyLog(syncOf(session), d, 'older');
      setSync(session, r.state);
      project(session, { bump: 'never' });
    } catch (_) {}
  });
}

/** Refresh every conversation that has been loaded (after an SSE reconnect). */
export function refreshAll() {
  for (const [session, conv] of Object.entries(conversations.bySession)) {
    if (conv.status === 'ready') refresh(session).catch(() => {});
  }
}

/** Snapshot rows tell us which caches are behind or belong to a dead conversation. */
export function reconcileWithSnapshot(agents = []) {
  for (const row of agents) {
    const conv = conversations.bySession[row.session];
    if (!conv || conv.status !== 'ready') continue;
    const r = applySnapshot(syncOf(row.session), row);
    setSync(row.session, r.state);
    runEffects(row.session, r.effects);
  }
}

/** A transcript-updated wake-up: coalesce a burst into one delta. */
export function wake(session) {
  if (!session || !conversations.bySession[session]) return;
  if (wakeTimers.has(session)) return;
  wakeTimers.set(session, setTimeout(() => {
    wakeTimers.delete(session);
    const r = onEvent(syncOf(session), { type: SSEType.TRANSCRIPT_UPDATED, session });
    setSync(session, r.state);
    runEffects(session, r.effects);
  }, WAKE_DEBOUNCE_MS));
}

/**
 * Roster and transcript events for chats the user has opened. Everything
 * else is ignored here: SSE traffic for agents nobody opened must not create
 * conversations, or a reconnect would fetch the whole roster.
 */
export function handleSseEvent(ev) {
  const session = ev && ev.session;
  if (!session || !conversations.bySession[session]) return;
  if (ev.type === SSEType.TRANSCRIPT_UPDATED) {
    wake(session);
    return;
  }
  const r = onEvent(syncOf(session), ev);
  setSync(session, r.state);
  runEffects(session, r.effects);
}

/** Relaunch or fork: the agent keeps its session but the conversation is new. */
export function reset(session) {
  if (!session) return;
  dropCache(session);
  reload(session).catch(() => {});
}

export function forget(session) {
  delete conversations.bySession[session];
  syncStates.delete(session);
}

// ---- local writes -----------------------------------------------------------

/** Paint the user's message before the server has it. `id` is `u-<client_msg_id>`. */
export function addOptimisticTurn(session, id, text) {
  text = String(text || '').trim();
  if (!session || !id || !text) return;
  const conv = entry(session);
  if (conv.turns.some(t => t.id === id)) return;
  const turn = {
    id, role: 'user', text, timestamp: new Date().toISOString(),
    optimistic: true, tools: [], display_cells: [], revision: 0,
  };
  conv.optimistic = [...conv.optimistic, turn];
  conv.turns = [...conv.turns, turn];
  conv.appendSeq++;
}

export function markTurnFailed(session, id) {
  if (!session || !id) return;
  const conv = conversations.bySession[session];
  if (!conv) return;
  const fail = t => (t.id === id ? { ...t, failed: true } : t);
  conv.optimistic = conv.optimistic.map(fail);
  conv.turns = conv.turns.map(fail);
}

// ---- live activity rows -----------------------------------------------------
//
// Tool calls and phase changes arrive over SSE before the transcript catches
// up. They are transient: the next delta that appends a turn clears the live
// thinking row, and rows are trimmed so a long turn cannot grow unbounded.
// Only chats the user has opened keep them; SSE traffic for every other agent
// must not create conversations, or a reconnect would fetch the whole roster.

function activityKey(item) {
  return [item.ts || '', item.kind || '', item.phase || '', item.tool || '',
          item.action || '', item.summary || ''].join('|');
}
function activityMatchKey(item) {
  return [item.kind || '', item.tool || '', item.action || '',
          item.summary || '', item.file_path || ''].join('|');
}
function activityLooseKey(item) {
  return [item.kind || '', item.tool || '', item.action || ''].join('|');
}

export function appendThinking(session, label) {
  const conv = session && conversations.bySession[session];
  if (!conv) return;
  const existing = conv.activity.find(r => r.thinkingLive);
  if (existing) {
    conv.activity = [...conv.activity.filter(r => r !== existing), existing];
    return;
  }
  conv.activity = [...conv.activity, {
    key: 'thinking-live', thinkingLive: true, cls: 'running',
    label: AgentState.THINKING, summary: `${label || session} is working`,
  }];
  conv.appendSeq++;
}

export function removeLiveThinking(session) {
  const conv = conversations.bySession[session];
  if (!conv) return;
  const next = conv.activity.filter(r => !r.thinkingLive);
  if (next.length !== conv.activity.length) conv.activity = next;
}

export function appendActivity(session, item) {
  if (!session || !isLoggableActivity(item, session)) return;
  const key = activityKey(item);
  if (!key) return;
  const conv = conversations.bySession[session];
  if (!conv) return;
  const matchKey = activityMatchKey(item);
  const looseKey = activityLooseKey(item);
  const cls = item.status === 'error' ? 'error'
    : item.status === 'running' ? 'running'
      : item.status === 'ok' ? 'ok' : 'recorded';

  const rows = conv.activity.filter(r => !r.thinkingLive);
  const matching = rows.filter(r => r.matchKey === matchKey);
  const loose = rows.filter(r => r.looseKey === looseKey);

  // A finished tool supersedes its own running row rather than stacking.
  const runningMatch = [...matching, ...loose].reverse().find(r => r.cls === 'running');
  if (runningMatch && cls !== 'running') {
    conv.activity = rows.map(r => (r === runningMatch ? { ...r, cls, key, matchKey, looseKey } : r));
    return;
  }
  if (rows.some(r => r.key === key)) return;
  if (cls === 'running' && matching.length) return;
  if (cls !== 'running' && (matching.length || loose.length)) return;

  let next = [...rows, {
    key, matchKey, looseKey, cls,
    ts: Number(item.ts) || Date.now(),
    label: item.action || item.phase || item.tool || item.kind || 'activity',
    summary: item.summary || item.tool || '',
  }];
  if (next.length > MAX_ACTIVITY_ROWS) next = next.slice(next.length - MAX_ACTIVITY_ROWS);
  conv.activity = next;
  conv.appendSeq++;
}
