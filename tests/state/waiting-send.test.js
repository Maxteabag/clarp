// A send the Host answers `queued: true` waits as a queue item and gets its
// transcript row only when its turn starts (Host contract 109, feature
// waited_send_order). Before this, the 20 s sweep called it "not delivered"
// while the agent was still busy, and pressing ↑ made a second message.

import { describe, it, expect } from 'vitest';
import {
  DeliveryState, confirmFromTurns, createDeliveryLog, dropSend, markState, markWaiting,
  recordSend, staleSends,
} from '@core/delivery.js';
import {
  bindDeliveryFailure, bindDeliveryWaiting, delivery, noteQueuedSendDeleted, noteSendQueued,
  noteSendResult, noteSendStarted, sweepStaleSends,
} from '../../web/src/stores/delivery.svelte.js';

const sent = (id, at = 1000) => {
  const log = recordSend(createDeliveryLog(), { id, session: 's', text: 'hi', at });
  return markState(log, id, DeliveryState.SENT);
};

describe('a send that waits in the queue', () => {
  it('is never swept by the 20 s timer', () => {
    const log = markWaiting(sent('w'), 'w');
    expect(staleSends(log, { now: 1000 + 600_000 })).toEqual([]);
    expect(log.entries[0].state).toBe(DeliveryState.SENT);
  });

  it('is still swept when the answer did not say queued', () => {
    const log = sent('p');
    expect(staleSends(log, { now: 1000 + 20_000 }).map(e => e.id)).toEqual(['p']);
  });

  it('is confirmed by its row when its turn starts', () => {
    const log = confirmFromTurns(markWaiting(sent('w'), 'w'), [{ id: 'u-w' }], 5000);
    expect(log.entries[0].state).toBe(DeliveryState.CONFIRMED);
  });

  it('is forgotten, not failed, when it is deleted from the queue', () => {
    const log = dropSend(markWaiting(sent('w'), 'w'), 'w');
    expect(log.entries).toEqual([]);
  });
});

describe('the store, as /send and queue-updated drive it', () => {
  it('keeps a queued send waiting past the sweep, then drops it on deletion', () => {
    const failed = [];
    const waiting = [];
    const dropped = [];
    bindDeliveryFailure((session, id) => failed.push(id));
    bindDeliveryWaiting((session, id) => waiting.push(id), (session, id) => dropped.push(id));
    noteSendStarted('q1', 's', 'second message');
    noteSendResult('q1', true);
    noteSendQueued('q1');
    const entry = delivery.entries.find(e => e.id === 'q1');
    entry.at -= 60_000;                     // the running turn takes a minute
    sweepStaleSends(20_000);
    expect(failed).toEqual([]);
    expect(waiting).toEqual(['u-q1']);
    expect(delivery.entries.find(e => e.id === 'q1').state).toBe(DeliveryState.SENT);
    noteQueuedSendDeleted('q1');
    expect(dropped).toEqual(['u-q1']);
    expect(delivery.entries.find(e => e.id === 'q1')).toBeUndefined();
  });
});
