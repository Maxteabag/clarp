// One conversation per agent, kept in sync with the server exactly the way
// docs/protocol.md describes:
//
//   open  → GET /log?session (tail snapshot)
//   wake  → GET /log?session&after_revision=<cursor> (delta, upsert by id)
//   replace_required or a new conversation_id → reload the tail
//   optimistic user turn keyed `u-<client_msg_id>` → confirmed when the server
//   returns a turn with the same id
//
// Every pane reads its own session from here, so two panes never share a
// transcript and switching back to an agent paints from memory.

import { isLoggableActivity } from '@core/activity-view-model.js';
import { AgentState } from '@core/protocol.js';
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
  /** session → ConversationState */
  bySession: {},
});

function blank(session) {
  return {
    session,
    turns: [],
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

// ---- fetching -------------------------------------------------------------
//
// One request per session at a time. A wake-up that arrives mid-flight sets
// `dirty`, and the fetch runs once more when the current one settles, so a
// burst of transcript-updated events costs at most two round trips.

const inflight = new Map();   // session → Promise
const dirty = new Set();
const wakeTimers = new Map();

async function fetchLog(session, params) {
  const qs = new URLSearchParams({ session, limit: String(PAGE), include_automated: '0', ...params });
  const r = await fetch('/log?' + qs.toString());
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}

function run(session, job) {
  if (inflight.has(session)) {
    dirty.add(session);
    return inflight.get(session);
  }
  const p = (async () => {
    try {
      await job();
    } finally {
      inflight.delete(session);
      // A wake-up that arrived mid-flight gets one follow-up delta, but only
      // for a healthy conversation: a failed load waits for the user to reopen
      // or refresh the chat instead of retrying in a loop.
      const again = dirty.delete(session);
      if (again && conversations.bySession[session]?.status === 'ready') {
        refresh(session).catch(() => {});
      }
    }
  })();
  inflight.set(session, p);
  return p;
}

/** Load the newest page, replacing whatever is cached. */
export function reload(session) {
  if (!session) return Promise.resolve();
  return run(session, async () => {
    const conv = entry(session);
    conv.status = 'loading';
    conv.error = '';
    const t0 = performance.now();
    try {
      const d = await fetchLog(session, {});
      applySnapshot(conv, d);
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
  if (conv.status === 'empty' || conv.status === 'error') return reload(session);
  return refresh(session);
}

/** Pull every change after the cursor. */
export function refresh(session) {
  if (!session) return Promise.resolve();
  const conv = entry(session);
  if (conv.status === 'error') return Promise.resolve();
  if (conv.status === 'empty') return reload(session);
  return run(session, async () => {
    try {
      const d = await fetchLog(session, { after_revision: String(conv.latestRevision || 0) });
      if (d.replace_required || (conv.conversationId && d.conversation_id
          && d.conversation_id !== conv.conversationId)) {
        clog('conversationReplace', `${session} ${d.replace_required ? 'replace_required' : 'new conversation'}`);
        applySnapshot(conv, await fetchLog(session, {}));
        return;
      }
      applyDelta(conv, d);
      if (d.has_more) dirty.add(session);
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
  return run(session, async () => {
    try {
      const d = await fetchLog(session, { before: oldest.id });
      const known = new Set(conv.turns.map(t => t.id));
      const older = (d.turns || []).filter(t => t && t.id && !known.has(t.id));
      conv.turns = [...older, ...conv.turns];
      conv.hasMore = !!d.has_more;
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
    if (row.conversation_id && conv.conversationId && row.conversation_id !== conv.conversationId) {
      reset(row.session);
    } else if ((Number(row.head_revision) || 0) > conv.latestRevision) {
      refresh(row.session).catch(() => {});
    }
  }
}

/** A transcript-updated wake-up: coalesce a burst into one delta. */
export function wake(session) {
  if (!session || !conversations.bySession[session]) return;
  if (wakeTimers.has(session)) return;
  wakeTimers.set(session, setTimeout(() => {
    wakeTimers.delete(session);
    refresh(session).catch(() => {});
  }, WAKE_DEBOUNCE_MS));
}

/** Relaunch or fork: the agent keeps its session but the conversation is new. */
export function reset(session) {
  if (!session) return;
  conversations.bySession[session] = blank(session);
  reload(session).catch(() => {});
}

export function forget(session) {
  delete conversations.bySession[session];
}

// ---- merging --------------------------------------------------------------

function applySnapshot(conv, d) {
  const incoming = (d.turns || []).filter(t => t && t.id);
  const ids = new Set(incoming.map(t => t.id));
  // Optimistic bubbles the server has not filed yet stay visible.
  const pending = conv.turns.filter(t => t.optimistic && !ids.has(t.id));
  conv.turns = [...incoming, ...pending];
  conv.conversationId = d.conversation_id || '';
  conv.latestRevision = Number(d.latest_revision) || 0;
  conv.latestTs = d.latest_ts || '';
  conv.hasMore = !!d.has_more;
  conv.missing = !!d.missing;
  conv.cwd = d.cwd || '';
  conv.status = 'ready';
  conv.error = '';
  conv.activity = [];
  conv.appendSeq++;
  confirmFromTurns(delivery, conv.turns);
}

function applyDelta(conv, d) {
  const incoming = (d.turns || []).filter(t => t && t.id);
  if (incoming.length) {
    const byId = new Map(conv.turns.map((t, i) => [t.id, i]));
    const next = [...conv.turns];
    let appended = false;
    for (const t of incoming) {
      const i = byId.get(t.id);
      if (i === undefined) {
        byId.set(t.id, next.length);
        next.push(t);
        appended = true;
      } else {
        next[i] = t;
      }
    }
    conv.turns = next;
    if (appended) {
      removeLiveThinking(conv.session);
      conv.appendSeq++;
    }
    confirmFromTurns(delivery, conv.turns);
  }
  conv.latestRevision = Math.max(conv.latestRevision, Number(d.latest_revision) || 0);
  if (d.latest_ts) conv.latestTs = d.latest_ts;
  if (d.conversation_id) conv.conversationId = d.conversation_id;
  conv.missing = !!d.missing && !conv.turns.length;
  conv.status = 'ready';
}

// ---- local writes -----------------------------------------------------------

/** Paint the user's message before the server has it. `id` is `u-<client_msg_id>`. */
export function addOptimisticTurn(session, id, text) {
  text = String(text || '').trim();
  if (!session || !id || !text) return;
  const conv = entry(session);
  if (conv.turns.some(t => t.id === id)) return;
  conv.turns = [...conv.turns, {
    id, role: 'user', text, timestamp: new Date().toISOString(),
    optimistic: true, tools: [], display_cells: [], revision: 0,
  }];
  conv.appendSeq++;
}

export function markTurnFailed(session, id) {
  if (!session || !id) return;
  const conv = conversations.bySession[session];
  if (!conv) return;
  conv.turns = conv.turns.map(t => (t.id === id ? { ...t, failed: true } : t));
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
