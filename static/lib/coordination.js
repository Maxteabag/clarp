// Coordination between agents, hidden from the chat (Host feature `hidden_coordination`,
// docs/protocol.md "Hidden coordination" and the client rules under it). Pure.
//
// The Host leaves the rows of a turn another agent started out of /log under an effective
// `hide`, and says where they were: a visible row after a hidden run carries `hidden_before`,
// a tail page or a delta carries `hidden_after` for what follows the newest visible row. Every
// answer to a request with `coordination` echoes the policy it was read under; the client keeps
// its cache with that echo and sends its `visibility` as `coordination_read` on deltas, so a
// setting changed here or on another device answers `replace_required`. A chat's own Show reads
// it again with `coordination=show` for that visit and never caches it.

export const COORDINATION_FEATURE = 'hidden_coordination';
export const VISIBILITIES = ['show', 'hide'];
export const OVERRIDES = ['default', 'show', 'hide'];

/** The policy an answer was read under, {hidden, source, visibility}, or null. */
export function normalizeEcho(value) {
  if (!value || typeof value !== 'object') return null;
  const visibility = String(value.visibility || '');
  if (!visibility) return null;
  return { hidden: !!value.hidden, source: String(value.source || ''), visibility };
}

/** A hidden run's count, {messages, exchanges, agents: [{agent_id, name}], capped}, or null. */
export function normalizeCount(value) {
  if (!value || typeof value !== 'object') return null;
  const messages = Math.max(0, Number(value.messages) || 0);
  const exchanges = Math.max(0, Number(value.exchanges) || 0);
  const capped = !!value.capped;
  if (!messages && !exchanges && !capped) return null;
  const agents = (Array.isArray(value.agents) ? value.agents : [])
    .filter(a => a && typeof a === 'object')
    .map(a => ({ agent_id: String(a.agent_id || ''), name: String(a.name || '').trim() }));
  return { messages, exchanges, agents, capped };
}

function plural(n, word) { return `${n} ${word}${n === 1 ? '' : 's'}`; }

/** '3 messages between agents', '100+ messages between agents' when capped; '' for none. */
export function countLine(value) {
  const count = normalizeCount(value);
  if (!count) return '';
  // Capped with no message counted (the run was cut before one): say it without a number.
  if (count.capped) return count.messages ? `${count.messages}+ messages between agents` : 'Messages between agents';
  if (count.messages) return `${plural(count.messages, 'message')} between agents`;
  return `${plural(count.exchanges, 'exchange')} between agents`;
}

/** The named agents of a run, 'with Mike and Rachel', or ''. */
export function countAgents(value) {
  const count = normalizeCount(value);
  const names = count ? [...new Set(count.agents.map(a => a.name).filter(Boolean))] : [];
  if (!names.length) return '';
  if (names.length === 1) return `with ${names[0]}`;
  return `with ${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`;
}

/**
 * The /log parameters for a chat. Without the feature none (every row, as before). A chat shown
 * for this visit reads `show`; otherwise `auto` (its own setting, else the Host's). A delta also
 * says what the kept copy was read under.
 */
export function logParams({ feature = false, reveal = false, echo = null, delta = false } = {}) {
  if (!feature) return {};
  const params = { coordination: reveal ? 'show' : 'auto' };
  const read = normalizeEcho(echo);
  if (delta && read) params.coordination_read = read.visibility;
  return params;
}

/**
 * Whether a chat read under `echo` must be read again now that its effective visibility is
 * `visibility` (from the snapshot or an agent-roster event). A chat shown for this visit
 * (source `request`) keeps what it shows until the visit ends.
 */
export function needsReload(echo, visibility) {
  const read = normalizeEcho(echo);
  const now = String(visibility || '');
  return !!(read && now && read.source !== 'request' && read.visibility !== now);
}

/** Under an effective hide, a live turn another agent started is not drawn. */
export function hidesLiveTurn(visibility, turn) {
  return visibility === 'hide' && !!turn && turn.audience === 'agents';
}
