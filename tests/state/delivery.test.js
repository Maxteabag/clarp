// Delivery confirmation, pinned against the ways a message was lost silently
// on 2026-09-01: deduped as a retry and never written, preempted by the next
// message a second later, or never leaving a wedged client at all. Every one
// of them answered 200 or looked sent.

import { describe, it, expect } from 'vitest';
import {
  DeliveryState, createDeliveryLog, recordSend, markState, confirmFromTurns,
  staleSends, pendingIds, failedEntries, deliverySummary, serverIdFor,
} from '@core/delivery.js';

const send = (log, id, over = {}) =>
  recordSend(log, { id, session: 'lena-02e0', text: 'hi', at: 1000, ...over });

describe('serverIdFor', () => {
  it('is the id the server files the durable row under', () => {
    // lib/message_store.py stores `u-` + client_msg_id, verbatim.
    expect(serverIdFor('c-mtip8tcx-4-1vmhboh')).toBe('u-c-mtip8tcx-4-1vmhboh');
  });

  it('is empty for a missing id rather than "u-undefined"', () => {
    expect(serverIdFor('')).toBe('');
    expect(serverIdFor(undefined)).toBe('');
  });
});

describe('a send starts pending and only the transcript confirms it', () => {
  it('is pending the moment it is recorded', () => {
    const log = send(createDeliveryLog(), 'a');
    expect(log.entries[0].state).toBe(DeliveryState.PENDING);
  });

  it('is still not delivered after a 200', () => {
    // 200 only means the request was received; the server answers 200 when it
    // dedups the turn and writes nothing.
    let log = send(createDeliveryLog(), 'a');
    log = markState(log, 'a', DeliveryState.SENT);
    expect(log.entries[0].state).toBe(DeliveryState.SENT);
    expect(deliverySummary(log).confirmed).toBe(0);
  });

  it('is delivered when its own id comes back in the transcript', () => {
    let log = send(createDeliveryLog(), 'a');
    log = markState(log, 'a', DeliveryState.SENT);
    log = confirmFromTurns(log, [{ id: 'u-a' }], 5000);
    expect(log.entries[0].state).toBe(DeliveryState.CONFIRMED);
    expect(log.entries[0].settledAt).toBe(5000);
  });

  it('is not confirmed by somebody else\'s turn', () => {
    let log = send(createDeliveryLog(), 'a');
    log = confirmFromTurns(log, [{ id: 'u-b' }, { id: 'assistant-1' }]);
    expect(log.entries[0].state).toBe(DeliveryState.PENDING);
  });

  it('confirms several sends from one transcript refresh', () => {
    let log = send(send(send(createDeliveryLog(), 'a'), 'b'), 'c');
    log = confirmFromTurns(log, [{ id: 'u-a' }, { id: 'u-c' }]);
    expect(log.entries.map(e => e.state)).toEqual([
      DeliveryState.CONFIRMED, DeliveryState.PENDING, DeliveryState.CONFIRMED,
    ]);
  });
});

describe('the silent losses become visible', () => {
  it('flags a send the server took but never filed', () => {
    // The dedup case: 200, no prompt_admission, no message row, nothing.
    let log = send(createDeliveryLog(), 'a', { at: 0 });
    log = markState(log, 'a', DeliveryState.SENT);
    expect(staleSends(log, { now: 20000, timeoutMs: 20000 })).toHaveLength(1);
  });

  it('flags a send that never left the client', () => {
    // The wedged-window case: still pending, no request ever went out.
    const log = send(createDeliveryLog(), 'a', { at: 0 });
    expect(staleSends(log, { now: 30000 })[0].id).toBe('a');
  });

  it('does not flag one that is merely young', () => {
    const log = send(createDeliveryLog(), 'a', { at: 0 });
    expect(staleSends(log, { now: 5000, timeoutMs: 20000 })).toHaveLength(0);
  });

  it('never re-flags a confirmed send however old', () => {
    let log = send(createDeliveryLog(), 'a', { at: 0 });
    log = confirmFromTurns(log, [{ id: 'u-a' }]);
    expect(staleSends(log, { now: 10 ** 9 })).toHaveLength(0);
  });

  it('keeps a failure failed when the transcript later catches up', () => {
    // A retry writes a new entry; the failed one stays failed as a record.
    let log = send(createDeliveryLog(), 'a');
    log = markState(log, 'a', DeliveryState.FAILED, 'timed out');
    log = confirmFromTurns(log, [{ id: 'u-a' }]);
    expect(log.entries[0].state).toBe(DeliveryState.FAILED);
  });

  it('records why it failed', () => {
    let log = send(createDeliveryLog(), 'a');
    log = markState(log, 'a', DeliveryState.FAILED, 'send failed (502)');
    expect(failedEntries(log)[0].detail).toBe('send failed (502)');
  });
});

describe('bookkeeping', () => {
  it('exposes which bubbles are still unconfirmed', () => {
    let log = send(send(createDeliveryLog(), 'a'), 'b');
    log = confirmFromTurns(log, [{ id: 'u-a' }]);
    expect([...pendingIds(log)]).toEqual(['u-b']);
  });

  it('counts each state', () => {
    let log = send(send(send(createDeliveryLog(), 'a'), 'b'), 'c');
    log = confirmFromTurns(log, [{ id: 'u-a' }]);
    log = markState(log, 'b', DeliveryState.FAILED);
    expect(deliverySummary(log)).toMatchObject({ confirmed: 1, failed: 1, pending: 1 });
  });

  it('keeps the log bounded', () => {
    let log = createDeliveryLog(3);
    for (const id of ['a', 'b', 'c', 'd', 'e']) log = send(log, id);
    expect(log.entries.map(e => e.id)).toEqual(['c', 'd', 'e']);
  });

  it('truncates long text rather than holding a whole message', () => {
    const log = send(createDeliveryLog(), 'a', { text: 'x'.repeat(500) });
    expect(log.entries[0].text).toHaveLength(200);
  });

  it('ignores a send with no id, which could never be confirmed', () => {
    const log = recordSend(createDeliveryLog(), { id: '', text: 'hi' });
    expect(log.entries).toHaveLength(0);
  });

  it('is null-safe throughout', () => {
    expect(() => markState(null, 'a', DeliveryState.SENT)).not.toThrow();
    expect(staleSends(null)).toEqual([]);
    expect([...pendingIds(null)]).toEqual([]);
    expect(deliverySummary(null)).toMatchObject({ confirmed: 0 });
  });
});
