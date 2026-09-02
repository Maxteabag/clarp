// One ordered transcript out of two sources.
//
// Durable turns come from /log with ISO timestamps; live activity rows arrive
// over SSE with an epoch-ms `ts`. They used to render as two blocks — every
// turn, then every activity row — which pinned tool calls below the last
// message however long ago they ran. A reply that landed after a tool call
// appeared above it.

const TURN = 0;
const ACTIVITY = 1;

function turnTime(turn) {
  const raw = turn && turn.timestamp;
  if (!raw) return null;
  const ms = typeof raw === 'number' ? raw : Date.parse(raw);
  return Number.isFinite(ms) ? ms : null;
}

function activityTime(row) {
  const raw = row && row.ts;
  if (raw == null) return null;
  const ms = typeof raw === 'number' ? raw : Date.parse(raw);
  return Number.isFinite(ms) ? ms : null;
}

// A row with no usable time inherits the last one we saw, so it stays next to
// the rows it arrived with instead of being flung to one end.
function stamp(items, readTime, kind) {
  let carried = null;
  return items.map((item, index) => {
    const own = readTime(item);
    if (own !== null) carried = own;
    return { item, kind, index, at: own !== null ? own : carried };
  });
}

export function mergeTimeline(turns = [], activity = []) {
  const stamped = [
    ...stamp(turns, turnTime, TURN),
    ...stamp(activity, activityTime, ACTIVITY),
  ];

  // Anything still without a time (nothing before it had one either) sorts
  // first, keeping the order it came in.
  const floor = stamped.reduce(
    (min, e) => (e.at !== null && e.at < min ? e.at : min), Infinity);
  const base = Number.isFinite(floor) ? floor - 1 : 0;

  return stamped
    .map(e => ({
      ...e,
      // The live thinking row is by definition happening now.
      at: e.item && e.item.thinkingLive ? Infinity : (e.at === null ? base : e.at),
    }))
    .sort((a, b) => a.at - b.at || a.kind - b.kind || a.index - b.index)
    .map(e => ({
      type: e.kind === TURN ? 'turn' : 'activity',
      key: e.kind === TURN ? `t:${e.item.id}` : `a:${e.item.key}`,
      item: e.item,
    }));
}
