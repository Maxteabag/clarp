// Pure sync reducer — the sync algorithm from docs/protocol.md with no I/O.
//
// State is plain JSON (no Maps, no timers) so fixtures can be written as
// JSON and any client can port the rules by reading this file plus the
// fixture set in contract/fixtures/:
//
//   open chat      → onOpen() → fetch_tail (when nothing cached)
//   snapshot row   → applySnapshot() → fetch_delta when behind
//   /log response  → applyLog() → upsert by id; replace_required or a new
//                    conversation_id means drop_cache + fetch_tail
//   SSE wake-up    → onEvent() → fetch_delta (coalesced while fetching)
//   roster reset   → onEvent() → drop_cache + fetch_tail
//
// Effects are strings the adapter turns into requests: fetch_tail,
// fetch_delta, fetch_older, drop_cache. Unknown event types and unknown
// fields are ignored, never thrown on (additive-only policy).

export const Effects = Object.freeze({
  FETCH_TAIL: 'fetch_tail',
  FETCH_DELTA: 'fetch_delta',
  FETCH_OLDER: 'fetch_older',
  DROP_CACHE: 'drop_cache',
});

export function blankSync(session) {
  return {
    session: session || '',
    conversationId: '',
    cursor: 0,
    turns: {},
    order: [],
    hasMore: false,
    missing: false,
    loaded: false,
    pendingFetch: null,
    wakeWhileFetching: false,
  };
}

function resetSync(state) {
  const next = blankSync(state.session);
  return next;
}

/** Open-chat: load once, otherwise refresh with a delta (handled by caller). */
export function onOpen(state) {
  if (state.loaded) return { state, effects: [] };
  return { state, effects: [Effects.FETCH_TAIL] };
}

/**
 * Snapshot rows tell us which caches are behind or belong to a dead
 * conversation. No cache yet → no opinion; the open-chat path fetches.
 */
export function applySnapshot(state, row) {
  if (!row || typeof row !== 'object') return { state, effects: [] };
  const conversationId = String(row.conversation_id || '');
  const head = Number(row.head_revision) || 0;
  if (state.loaded && conversationId && state.conversationId
      && conversationId !== state.conversationId) {
    return { state: resetSync(state), effects: [Effects.DROP_CACHE, Effects.FETCH_TAIL] };
  }
  if (state.loaded && head > state.cursor) {
    return { state, effects: [Effects.FETCH_DELTA] };
  }
  return { state, effects: [] };
}

/**
 * Merge one /log response. mode is 'tail' | 'delta' | 'older'.
 * Identity is the turn id; a lower revision never overwrites a newer row.
 */
export function applyLog(state, response, mode) {
  const d = (response && typeof response === 'object') ? response : {};
  if (mode === 'tail') {
    const next = blankSync(state.session);
    next.loaded = true;
    next.conversationId = String(d.conversation_id || '');
    next.cursor = Number(d.latest_revision) || 0;
    next.hasMore = !!d.has_more;
    next.missing = !!d.missing;
    for (const t of Array.isArray(d.turns) ? d.turns : []) {
      if (t && t.id) {
        next.turns[t.id] = t;
        next.order.push(t.id);
      }
    }
    return { state: next, effects: [] };
  }
  if (mode === 'older') {
    const known = new Set(state.order);
    const turns = { ...state.turns };
    const older = [];
    for (const t of Array.isArray(d.turns) ? d.turns : []) {
      if (t && t.id && !known.has(t.id)) {
        known.add(t.id);
        older.push(t.id);
        turns[t.id] = t;
      }
    }
    const next = { ...state, turns, order: [...older, ...state.order], hasMore: !!d.has_more };
    return { state: next, effects: [] };
  }
  // mode === 'delta'
  const incomingId = String(d.conversation_id || '');
  if (d.replace_required
      || (state.conversationId && incomingId && incomingId !== state.conversationId)) {
    return { state: resetSync(state), effects: [Effects.DROP_CACHE, Effects.FETCH_TAIL] };
  }
  const next = {
    ...state,
    turns: { ...state.turns },
    order: [...state.order],
  };
  let appended = false;
  for (const t of Array.isArray(d.turns) ? d.turns : []) {
    if (!t || !t.id) continue;
    const prev = next.turns[t.id];
    if (prev === undefined) {
      next.turns[t.id] = t;
      next.order.push(t.id);
      appended = true;
    } else if ((Number(t.revision) || 0) >= (Number(prev.revision) || 0)) {
      next.turns[t.id] = t;
    }
  }
  next.hasMore = !!d.has_more;
  next.cursor = Math.max(next.cursor, Number(d.latest_revision) || 0);
  if (incomingId) next.conversationId = incomingId;
  if (d.latest_ts) next.latestTs = d.latest_ts;
  next.missing = !!d.missing && next.order.length === 0;
  next.loaded = true;
  const effects = d.has_more ? [Effects.FETCH_DELTA] : [];
  return { state: next, effects };
}

/**
 * SSE wake-ups. transcript-updated coalesces while a fetch is in flight;
 * created/relaunched/forked for our session means the conversation is new.
 * Anything else — other sessions, focus, unknown types — is ignored.
 */
export function onEvent(state, event) {
  const ev = (event && typeof event === 'object') ? event : {};
  const type = String(ev.type || '');
  if (type === 'transcript-updated') {
    if (String(ev.session || '') !== state.session) return { state, effects: [] };
    if (state.pendingFetch) {
      return { state: { ...state, wakeWhileFetching: true }, effects: [] };
    }
    return { state, effects: [Effects.FETCH_DELTA] };
  }
  if (type === 'agent-roster') {
    const kind = String(ev.kind || '');
    if (String(ev.session || '') !== state.session) return { state, effects: [] };
    if (kind === 'created' || kind === 'relaunched' || kind === 'forked') {
      return { state: resetSync(state), effects: [Effects.DROP_CACHE, Effects.FETCH_TAIL] };
    }
    if (kind === 'deleted') {
      return { state: resetSync(state), effects: [Effects.DROP_CACHE] };
    }
    return { state, effects: [] };
  }
  return { state, effects: [] };
}

/** One request per session at a time; a wake-up mid-flight runs once more. */
export function beginFetch(state, kind) {
  return {
    state: { ...state, pendingFetch: kind || 'delta', wakeWhileFetching: false },
    effects: [],
  };
}

export function endFetch(state) {
  const woken = state.wakeWhileFetching;
  const next = { ...state, pendingFetch: null, wakeWhileFetching: false };
  return { state: next, effects: woken ? [Effects.FETCH_DELTA] : [] };
}

/** Clip URL precedence: playlist_url beats stream_url beats url. */
export function pickClipSource(audioEvent) {
  const ev = (audioEvent && typeof audioEvent === 'object') ? audioEvent : {};
  if (ev.playlist_url) return 'playlist';
  if (ev.stream_url) return 'stream';
  if (ev.url) return 'file';
  return null;
}

/** A clip already acked must not replay after reconnect. */
export function isClipReplay(clipId, seenIds) {
  if (clipId === undefined || clipId === null || !Array.isArray(seenIds)) return false;
  return seenIds.indexOf(clipId) !== -1;
}
