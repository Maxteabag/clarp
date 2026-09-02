// Did the message actually arrive?
//
// A bubble is painted before the request and the server answers 200 even
// when it throws the turn away. The transcript is the only proof: a send is
// delivered when `/log` returns a turn with the id the client minted.

import {
  DeliveryState, createDeliveryLog, markState, recordSend, serverIdFor, staleSends,
} from '@core/delivery.js';

export const delivery = $state(createDeliveryLog(60));

/** Late import breaks the cycle conversations → delivery → conversations. */
let markFailed = () => {};
export function bindDeliveryFailure(fn) { markFailed = fn; }

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
