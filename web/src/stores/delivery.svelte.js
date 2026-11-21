// Did the message actually arrive?
//
// A bubble is painted before the request and the server answers 200 even
// when it throws the turn away. The transcript is the only proof: a send is
// delivered when `/log` returns a turn with the id the client minted.

import {
  DeliveryState, createDeliveryLog, dropSend, markState, markWaiting, recordSend,
  serverIdFor, staleSends,
} from '@core/delivery.js';

export const delivery = $state(createDeliveryLog(60));

/** Late import breaks the cycle conversations → delivery → conversations. */
let markFailed = () => {};
export function bindDeliveryFailure(fn) { markFailed = fn; }
let markWaitingTurn = () => {};
let dropTurn = () => {};
/** The bubble side of a waiting send: draw it as waiting; remove it once deleted. */
export function bindDeliveryWaiting(waiting, dropped) {
  markWaitingTurn = waiting;
  dropTurn = dropped;
}

/** /send answered `queued: true`: the send waits for the running turn as a
 *  queue item and gets its row only when its turn starts. Never swept. */
export function noteSendQueued(clientMsgId) {
  markWaiting(delivery, clientMsgId);
  const entry = delivery.entries.find(e => e.id === clientMsgId);
  if (entry && entry.waiting) markWaitingTurn(entry.session, entry.serverId);
}

/** queue-updated said this waiting send was deleted from the queue. */
export function noteQueuedSendDeleted(clientMsgId) {
  const entry = delivery.entries.find(e => e.id === clientMsgId);
  if (!entry) return;
  dropSend(delivery, clientMsgId);
  dropTurn(entry.session, entry.serverId);
}

export function noteSendStarted(clientMsgId, session, text) {
  recordSend(delivery, { id: clientMsgId, session, text });
}

export function noteSendResult(clientMsgId, ok, detail = '') {
  markState(delivery, clientMsgId, ok ? DeliveryState.SENT : DeliveryState.FAILED, detail);
  if (!ok) {
    const entry = delivery.entries.find(e => e.id === clientMsgId);
    markFailed(entry && entry.session, serverIdFor(clientMsgId));
  }
}

// Anything the server accepted but never filed in the transcript is lost:
// deduplicated as a retry, or preempted by the next message.
export function sweepStaleSends(timeoutMs = 20000) {
  for (const entry of staleSends(delivery, { timeoutMs })) {
    markState(delivery, entry.id, DeliveryState.FAILED, 'never reached the transcript');
    markFailed(entry.session, entry.serverId);
  }
}
