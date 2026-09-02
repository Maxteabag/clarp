// Conversation state machine — drives the audio scheduler.
//
// States:
//   idle          → playback queue runs FIFO, addressee-first on ties.
//   recording     → mic open; queue holds until recording ends.
//   transcribing  → audio uploaded, waiting for Whisper.
//   awaiting      → message sent, holding for the addressed agent's reply.
//
// Bugs this module pins (see TESTS.md):
//   B14: target locks at startCapture, not at send time.
//   B15: in `awaiting`, only the addressee's clip can play through.
//   B16: stale flags can't lock the queue — every transition is explicit.

export const States = Object.freeze({
  IDLE:         'idle',
  RECORDING:    'recording',
  TRANSCRIBING: 'transcribing',
  AWAITING:     'awaiting',
});

const ALLOWED = {
  [States.IDLE]:         new Set([States.RECORDING]),
  [States.RECORDING]:    new Set([States.TRANSCRIBING, States.IDLE]),
  [States.TRANSCRIBING]: new Set([States.AWAITING, States.IDLE]),
  [States.AWAITING]:     new Set([States.IDLE, States.RECORDING]),
};

/**
 * Pure conversation state machine.
 *
 * @param {object} [opts]
 * @param {() => number} [opts.now] — clock, injectable for tests.
 * @param {number} [opts.awaitDeadlineMs] — safety cap on `awaiting`.
 */
export function createStateMachine(opts = {}) {
  const now = opts.now || (() => Date.now());
  const awaitDeadlineMs = opts.awaitDeadlineMs ?? 60000;

  let state = States.IDLE;
  let expectingFrom = '';      // session whose reply we're waiting for
  let captureTarget = '';      // session locked at start of recording
  let deadline = 0;
  const listeners = new Set();

  function emit(event) {
    for (const l of listeners) {
      try { l(event); } catch (_) {}
    }
  }

  function transition(next, payload = {}) {
    if (!ALLOWED[state] || !ALLOWED[state].has(next)) {
      throw new Error(`Illegal transition: ${state} → ${next}`);
    }
    const prev = state;
    state = next;
    if (next === States.RECORDING) {
      captureTarget = payload.target || '';
    }
    if (next === States.AWAITING) {
      expectingFrom = payload.target || captureTarget || '';
      deadline = now() + awaitDeadlineMs;
    }
    if (next === States.IDLE) {
      expectingFrom = '';
      deadline = 0;
      captureTarget = '';
    }
    emit({ type: 'transition', from: prev, to: next, payload });
  }

  function startRecording(target) { transition(States.RECORDING, { target }); }
  function endRecording()         { transition(States.TRANSCRIBING); }
  function send(target)           { transition(States.AWAITING, { target }); }
  function abort()                { transition(States.IDLE); }
  function settle()               { transition(States.IDLE); }

  function awaitExpired() {
    return state === States.AWAITING && now() >= deadline;
  }

  return {
    States,
    on: (fn) => { listeners.add(fn); return () => listeners.delete(fn); },
    get state()         { return state; },
    get captureTarget() { return captureTarget; },
    get expectingFrom() { return expectingFrom; },
    get deadline()      { return deadline; },
    startRecording, endRecording, send, abort, settle,
    awaitExpired,
  };
}
