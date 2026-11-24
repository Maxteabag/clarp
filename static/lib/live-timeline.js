// The transcript's rows with the live turn among them (docs/live-items.md §7,
// same rules as clarp-ios 3fb7d67):
//
// - only the newest turn_id is drawn from live items (currentTurnItems); when
//   a new turn starts the last turn's live rows go, and its durable /log rows,
//   which were hidden while the live turn stood in for them, show again where
//   they always were
// - the live turn sits before the first row written after its turn started
//   (liveInsertIndex), so a prompt sent once it settled lands below it
// - a message of the user's that the turn read while it ran (a steer) splits
//   the live turn there (withLiveSegments): what the turn did after reading it
//   is drawn below it, never above it as one block under the prompt
//
// Transcript.svelte runs these same steps as separate deriveds, so a live
// event that changes nothing structural rebuilds nothing.

import { mergeTimeline } from './timeline.js';
import { currentTurnItems, liveInsertIndex, takenOverTurns } from './live-present.js';

export const LIVE_ENTRY = Object.freeze({ type: 'live', key: 'live-turn' });

/** `merged` with the live turn's single keyed entry at `at` (-1: none). */
export function withLiveEntry(merged, at) {
  if (at < 0) return merged;
  return [...merged.slice(0, at), LIVE_ENTRY, ...merged.slice(at)];
}

function rowMs(row) {
  const raw = row && (row.timestamp != null ? row.timestamp : row.ts);
  if (raw == null || raw === '') return null;
  const ms = typeof raw === 'number' ? raw : Date.parse(raw);
  return Number.isFinite(ms) ? ms : null;
}

/**
 * `merged` with the live turn at `at`, split at every user row the turn read
 * while it ran: one written after the turn started and, once the turn ended,
 * not after its end. A steered message is such a row; a prompt sent once the
 * turn settled is not. Each part is an entry {type: 'live', key, from, to}
 * that draws the turn's items started in [from, to) (liveSegmentItems). The
 * first part keeps LIVE_ENTRY's key, so a turn nobody steered is drawn as
 * before. Only times decide: a live event that changes no turn time rebuilds
 * nothing here.
 *
 * Before this, the turn was one block under its prompt, and the answer to a
 * steer was drawn above the steer itself (24 Nov 2026, Claude and the Codex
 * lane, every steered turn).
 */
export function withLiveSegments(merged, at, startedMs, endedMs) {
  if (at < 0) return merged;
  const started = Number(startedMs) || 0;
  const ended = Number(endedMs) || Infinity;
  const reads = [];
  for (let i = at; i < merged.length; i++) {
    const entry = merged[i];
    if (entry.type !== 'turn' || !entry.item || entry.item.role !== 'user') continue;
    const t = rowMs(entry.item);
    if (t != null && t > started && t <= ended) reads.push({ index: i, t });
  }
  if (!reads.length) return withLiveEntry(merged, at);
  const part = (k, from, to) => ({ type: 'live', key: k ? `live-turn:${k}` : LIVE_ENTRY.key, from, to });
  const out = [...merged.slice(0, at), part(0, -Infinity, reads[0].t)];
  let k = 0;
  for (let i = at; i < merged.length; i++) {
    out.push(merged[i]);
    if (k < reads.length && reads[k].index === i) {
      k += 1;
      out.push(part(k, reads[k - 1].t, k < reads.length ? reads[k].t : Infinity));
    }
  }
  return out;
}

/** The live items a part of the turn draws: those started in [from, to). */
export function liveSegmentItems(items, entry) {
  if (!entry || entry.from === undefined) return items;
  return items.filter((item) => {
    const t = Number(item.started_at_ms) || 0;
    return (entry.from === -Infinity || t >= entry.from) && t < entry.to;
  });
}

export function composeTimeline({ turns = [], activity = [], liveState = null, previousHidden = null } = {}) {
  const items = liveState ? currentTurnItems(liveState) : [];
  const hidden = takenOverTurns(turns, items, previousHidden, liveState && liveState.turn);
  const kept = hidden.size ? turns.filter(t => !hidden.has(t.id)) : turns;
  const merged = mergeTimeline(kept, items.length ? [] : activity);
  const at = items.length ? liveInsertIndex(merged.map(e => e.item), liveState.turn) : -1;
  const turn = (liveState && liveState.turn) || {};
  return { items, hidden, merged, at, entries: withLiveSegments(merged, at, turn.started_at_ms, turn.ended_at_ms) };
}
