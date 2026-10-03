// Live items: one conversation's open turn as items that appear, grow and
// settle in place (docs/live-items.md). This is the client reducer; it
// follows server/lib/live_items.py (LiveView) rule for rule, and both are
// checked against the recorded streams in contract/live.
//
// State is immutable. An event that touches one item replaces that item and
// the `items` map, and keeps every other item object as it was, so a view
// keyed by item id re-renders only what changed.

export const OUTPUT_TAIL_LINES = 50;
export const TERMINAL_STATUSES = Object.freeze(['completed', 'failed', 'interrupted']);
export const LiveEffects = Object.freeze({ FETCH_LIVE: 'fetch_live' });

const IDLE = Object.freeze({ state: 'idle' });

export function blankLive() {
  return {
    epoch: null,
    lseq: null,
    activity: IDLE,
    turn: null,
    items: {},
    awaitingSnapshot: false,
    /** Host clock minus local clock, from the newest server_now_ms. */
    skewMs: 0,
    toolExplanations: null,
  };
}

const isObject = v => v !== null && typeof v === 'object' && !Array.isArray(v);

/** Fields absent stay, null clears, objects merge, other values replace. */
export function mergePatch(target, patch) {
  const out = { ...target };
  for (const [key, value] of Object.entries(patch || {})) {
    out[key] = isObject(value) && isObject(out[key]) ? mergePatch(out[key], value) : value;
  }
  return out;
}

function appendOutput(item, lines, totalLines) {
  const tool = item.tool || {};
  const output = isObject(tool.output)
    ? tool.output
    : { tail: [], total_lines: 0, truncated: false, exit_code: null };
  const tail = [...(output.tail || []), ...lines].slice(-OUTPUT_TAIL_LINES);
  return {
    ...item,
    tool: {
      ...tool,
      output: { ...output, tail, total_lines: totalLines, truncated: totalLines > tail.length },
    },
  };
}

function skewOf(payload, now) {
  const at = Number(payload && payload.server_now_ms);
  return Number.isFinite(at) && at > 0 ? at - now : 0;
}

/** Replace the live state with a GET /live snapshot. */
export function applyLiveSnapshot(state, snapshot, now = Date.now()) {
  const items = {};
  for (const item of snapshot.items || []) items[item.id] = item;
  return {
    ...state,
    epoch: snapshot.epoch,
    lseq: Number(snapshot.lseq),
    activity: snapshot.activity || IDLE,
    turn: snapshot.turn || null,
    items,
    awaitingSnapshot: false,
    skewMs: skewOf(snapshot, now),
    toolExplanations: snapshot.tool_explanations || state.toolExplanations || null,
  };
}

/** Ask for GET /live once; ignore events until it lands. */
export function requestLiveSnapshot(state) {
  if (state.awaitingSnapshot) return { state, effects: [] };
  return { state: { ...state, awaitingSnapshot: true }, effects: [LiveEffects.FETCH_LIVE] };
}

/** The snapshot request failed: the next event may ask again. */
export function liveSnapshotFailed(state) {
  return state.awaitingSnapshot ? { ...state, awaitingSnapshot: false } : state;
}

function applyOp(state, op) {
  const name = op.op;
  if (name === 'status') return { ...state, activity: op.activity || IDLE };
  if (name === 'turn') {
    const turn = op.turn || null;
    // A new turn: the last one's items now live in /log.
    const fresh = state.turn && turn && turn.turn_id !== state.turn.turn_id;
    return { ...state, turn, items: fresh ? {} : state.items };
  }
  if (name !== 'upsert' && name !== 'append' && name !== 'done') return state;
  const id = op.id;
  const rev = op.rev;
  let item = state.items[id];
  if (!item) {
    if (name !== 'upsert') return null;
    item = { id, kind: op.kind, rev: 0 };
  } else if (rev !== (item.rev || 0) + 1) {
    return null;
  }
  if (name === 'upsert') {
    item = mergePatch(item, op.item);
  } else if (name === 'append') {
    if (op.field === 'text') {
      item = { ...item, text: String(item.text || '') + String(op.chunk || '') };
    } else if (op.field === 'tool.output') {
      item = appendOutput(item, op.lines || [], Number(op.total_lines) || 0);
    }
  } else {
    item = { ...item, status: op.status };
    if ('started_at_ms' in op) item.started_at_ms = op.started_at_ms;
    if ('ended_at_ms' in op) item.ended_at_ms = op.ended_at_ms;
    item = mergePatch(item, op.item);
  }
  item.rev = Number.isInteger(rev) ? rev : (item.rev || 0) + 1;
  return { ...state, items: { ...state.items, [id]: item } };
}

/**
 * Apply one `live` event. Returns `{state, effects}`; `fetch_live` means the
 * caller must GET /live and hand the answer to applyLiveSnapshot.
 */
export function applyLiveEvent(state, event, now = Date.now()) {
  if (state.awaitingSnapshot) return { state, effects: [] };
  const lseq = Number(event.lseq);
  if (state.lseq === null || event.epoch !== state.epoch) return requestLiveSnapshot(state);
  if (lseq <= state.lseq) return { state, effects: [] };
  if (lseq !== state.lseq + 1) return requestLiveSnapshot(state);
  let next = state;
  for (const op of event.ops || []) {
    next = applyOp(next, op);
    if (!next) return requestLiveSnapshot(state);
  }
  return { state: { ...next, lseq, skewMs: skewOf(event, now) || next.skewMs }, effects: [] };
}

/** The items of the open turn, ordered by ordinal. */
export function liveItems(state) {
  return Object.values(state.items).sort((a, b) => (a.ordinal || 0) - (b.ordinal || 0));
}

export function isTerminal(status) {
  return TERMINAL_STATUSES.includes(status);
}
