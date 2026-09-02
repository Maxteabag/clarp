// B14: target locks at startCapture.
// B15: AWAITING only releases for the addressee.
// B16: explicit transitions, no stale flags.

import { describe, it, expect, beforeEach } from 'vitest';
import { createStateMachine, States } from '../../static/lib/state-machine.js';

describe('createStateMachine', () => {
  let clock = 0;
  let m;

  beforeEach(() => {
    clock = 1_000_000;
    m = createStateMachine({ now: () => clock, awaitDeadlineMs: 60_000 });
  });

  it('starts in idle', () => {
    expect(m.state).toBe(States.IDLE);
  });

  it('locks the capture target at startRecording (B14)', () => {
    m.startRecording('mike');
    expect(m.captureTarget).toBe('mike');
    m.endRecording();
    expect(m.captureTarget).toBe('mike'); // still set until idle
    m.send('mike');
    expect(m.state).toBe(States.AWAITING);
    expect(m.expectingFrom).toBe('mike');
  });

  it('AWAITING expires after the deadline', () => {
    m.startRecording('mike');
    m.endRecording();
    m.send('mike');
    expect(m.awaitExpired()).toBe(false);
    clock += 30_000;
    expect(m.awaitExpired()).toBe(false);
    clock += 31_000;
    expect(m.awaitExpired()).toBe(true);
  });

  it('rejects illegal transitions (B16)', () => {
    // Cannot go straight to AWAITING from IDLE.
    expect(() => m.send('mike')).toThrow(/Illegal transition/);
    // Cannot endRecording from idle.
    expect(() => m.endRecording()).toThrow();
  });

  it('emits a transition event on every state change', () => {
    const events = [];
    m.on(e => events.push(e));
    m.startRecording('mike');
    m.endRecording();
    m.send('mike');
    m.settle();
    expect(events.map(e => e.to)).toEqual([
      States.RECORDING, States.TRANSCRIBING, States.AWAITING, States.IDLE,
    ]);
  });

  it('abort from recording goes back to idle', () => {
    m.startRecording('mike');
    m.abort();
    expect(m.state).toBe(States.IDLE);
    expect(m.captureTarget).toBe('');
  });
});
