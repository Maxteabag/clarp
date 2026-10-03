// The transcript's rows with the live turn among them (docs/live-items.md §7,
// same rules as clarp-ios 3fb7d67):
//
// - only the newest turn_id is drawn from live items (currentTurnItems); when
//   a new turn starts the last turn's live rows go, and its durable /log rows,
//   which were hidden while the live turn stood in for them, show again where
//   they always were
// - the live turn sits before the first row written after its turn started
//   (liveInsertIndex), so a prompt sent once it settled lands below it
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

export function composeTimeline({ turns = [], activity = [], liveState = null, previousHidden = null } = {}) {
  const items = liveState ? currentTurnItems(liveState) : [];
  const hidden = takenOverTurns(turns, items, previousHidden);
  const kept = hidden.size ? turns.filter(t => !hidden.has(t.id)) : turns;
  const merged = mergeTimeline(kept, items.length ? [] : activity);
  const at = items.length ? liveInsertIndex(merged.map(e => e.item), liveState.turn) : -1;
  return { items, hidden, merged, at, entries: withLiveEntry(merged, at) };
}
