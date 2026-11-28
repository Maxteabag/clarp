// The failed-turn cards of a chat (docs/turn-errors.md): which explained failures collapse into
// the newest of their run ("3×"), and which card offers a retry. Read by the card itself (Turn,
// LiveTurn), so the transcript needs no wiring of its own; only rows that carry an explained
// error ask, so a chat without failures pays nothing.

import { currentTurnItems, explainedError, planTurnErrors, systemNotice } from '@core/live-present.js';
import { conversations } from './conversations.svelte.js';
import { liveFor } from './live.svelte.js';

const EMPTY = { hidden: new Set(), counts: new Map(), actionable: null };
// session -> { turns, state, length, plan }: the plan is worked out again only when the chat's
// rows or its live turn changed.
const plans = new Map();

/** The chat whose rows hold `turn` (the row object the transcript drew). */
export function sessionOfTurn(turn) {
  for (const [session, conv] of Object.entries(conversations.bySession)) {
    if (conv && conv.turns && conv.turns.includes(turn)) return session;
  }
  return '';
}

/** The chat whose live turn `state` is. */
export function sessionOfLive(state) {
  for (const session of Object.keys(conversations.bySession)) {
    if (state && liveFor(session) === state) return session;
  }
  return '';
}

/** The chat's plan: `hidden` older repeats, `counts` on the card drawn, `actionable` card id
 * ('live' for the live failed turn). */
export function errorPlanFor(session) {
  const conv = session && conversations.bySession[session];
  if (!conv || !conv.turns) return EMPTY;
  const state = liveFor(session);
  const cached = plans.get(session);
  if (cached && cached.turns === conv.turns && cached.length === conv.turns.length && cached.state === state) return cached.plan;
  const liveTurn = state && state.turn;
  const liveFailure = liveTurn && liveTurn.status === 'failed' && currentTurnItems(state).length ? explainedError(liveTurn.error) : null;
  // While the live failure shows, its durable notice is not drawn (takenOverTurns): it is the live card.
  const turns = liveFailure
    ? conv.turns.filter(t => !(t.trace_id && t.trace_id === liveTurn.turn_id && systemNotice(t)))
    : conv.turns;
  const plan = planTurnErrors(turns, liveFailure ? { key: 'live', error: liveFailure } : null);
  plans.set(session, { turns: conv.turns, length: conv.turns.length, state, plan });
  return plan;
}
