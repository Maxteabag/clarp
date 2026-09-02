// Did the message actually arrive?
//
// The client painted its bubble before the request and the server answers 200
// whatever it decides to do with the turn — run it, dedup it as a retry,
// preempt it with the next one. So a delivered message and a lost one looked
// identical, and you found out an hour later when nobody replied.
//
// The server stores the durable user row under `u-` + the client_msg_id the
// client minted, verbatim, and returns it in /log (lib/message_store.py:
// "client and server match by identity — never by fuzzy text/sequence
// reconciliation"). So the client can confirm a send end to end: it is
// delivered when its own id comes back in the transcript, and nothing else
// counts. 200 only means the request was received.

export const DeliveryState = {
  PENDING: 'pending',     // request in flight
  SENT: 'sent',           // server took it, not yet in the transcript
  CONFIRMED: 'confirmed', // its id came back in /log — actually delivered
  FAILED: 'failed',       // rejected, errored, or never showed up
};

// The transcript id the server will file this send under.
export function serverIdFor(clientMsgId) {
  return clientMsgId ? `u-${clientMsgId}` : '';
}

export function createDeliveryLog(limit = 50) {
  return { entries: [], limit };
}

function trim(log) {
  if (log.entries.length > log.limit) {
    log.entries = log.entries.slice(log.entries.length - log.limit);
  }
  return log;
}

export function recordSend(log, { id, session, text, at = Date.now() }) {
  if (!log || !id) return log;
  log.entries = [...log.entries, {
    id,
    serverId: serverIdFor(id),
    session,
    text: String(text || '').slice(0, 200),
    state: DeliveryState.PENDING,
    at,
    settledAt: null,
    detail: '',
  }];
  return trim(log);
}

export function markState(log, id, state, detail = '', now = Date.now()) {
  if (!log) return log;
  log.entries = log.entries.map(e => (e.id === id
    ? {
      ...e,
      state,
      detail: detail || e.detail,
      settledAt: state === DeliveryState.PENDING || state === DeliveryState.SENT
        ? e.settledAt
        : now,
    }
    : e));
  return log;
}

// A turn list from /log confirms every send whose id it contains. Called on
// each transcript refresh, so confirmation costs no extra request.
export function confirmFromTurns(log, turns = [], now = Date.now()) {
  if (!log || !log.entries.length) return log;
  const ids = new Set((turns || []).map(t => t && t.id).filter(Boolean));
  log.entries = log.entries.map(e => (
    e.state !== DeliveryState.CONFIRMED && e.state !== DeliveryState.FAILED && ids.has(e.serverId)
      ? { ...e, state: DeliveryState.CONFIRMED, settledAt: now }
      : e));
  return log;
}

// Anything the server took but never filed is lost — preempted by the next
// message, or dropped as a duplicate. Both used to be silent.
export function staleSends(log, { now = Date.now(), timeoutMs = 20000 } = {}) {
  if (!log) return [];
  return log.entries.filter(e =>
    (e.state === DeliveryState.PENDING || e.state === DeliveryState.SENT)
    && now - e.at >= timeoutMs);
}

export function pendingIds(log) {
  if (!log) return new Set();
  return new Set(log.entries
    .filter(e => e.state === DeliveryState.PENDING || e.state === DeliveryState.SENT)
    .map(e => e.serverId));
}

export function failedEntries(log) {
  return log ? log.entries.filter(e => e.state === DeliveryState.FAILED) : [];
}

export function deliverySummary(log) {
  const counts = { pending: 0, sent: 0, confirmed: 0, failed: 0 };
  for (const e of (log ? log.entries : [])) counts[e.state] = (counts[e.state] || 0) + 1;
  return counts;
}
