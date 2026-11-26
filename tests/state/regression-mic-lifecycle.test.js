// Hardening sweep — order of operations around the microphone.
//
// Issue #13 was one instance of a general shape in this store: a function
// owns several flags (`alwaysOn`, `singleShot`, `mic.recording`,
// `mic.capturing`) and its failure paths return without putting them back.
// The button reads `mic.recording`, so every one of those paths leaves a
// green mic over a dead pipeline. These drive the real store
// (web/src/stores/mic.svelte.js) through its real entry points.

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { Timing } from '@core/protocol.js';

const flash = vi.fn();
const clog = vi.fn();
const sendText = vi.fn();

const machine = {
  startRecording: vi.fn(),
  endRecording: vi.fn(),
  settle: vi.fn(),
};

vi.mock('../../web/src/lib/net.js', () => ({ clog, trace: { id: '' } }));
vi.mock('../../web/src/stores/app.svelte.js', () => ({
  app: { session: 'lena-02e0' }, flash,
}));
vi.mock('../../web/src/stores/audio.svelte.js', () => ({
  machine,
  playerAdapter: { interrupt() {} },
  scheduler: { flushOlderThan() {} },
  tick() {},
  unlockAudio() {},
  addConditionSource() {},
}));
vi.mock('../../web/src/stores/send.svelte.js', () => ({
  send: { captureTarget: '', pendingHandsFree: false }, sendText,
}));

const recorders = [];
let recorderThrows = false;

class FakeMediaRecorder {
  static isTypeSupported() { return true; }
  constructor(stream, opts = {}) {
    if (recorderThrows) throw new DOMException('device busy', 'NotSupportedError');
    this.stream = stream;
    this.mimeType = opts.mimeType || 'audio/webm';
    this.state = 'inactive';
    this.starts = 0;
    this.stops = 0;
    this.onstop = null;
    this.ondataavailable = null;
    recorders.push(this);
  }
  start() { this.starts += 1; this.state = 'recording'; }
  stop() { this.stops += 1; this.state = 'inactive'; }
  async finish(bytes = 4096) {
    const data = new Blob([new Uint8Array(bytes)], { type: this.mimeType });
    this.ondataavailable && this.ondataavailable({ data });
    if (this.onstop) await this.onstop();
  }
}

// The VAD reads a spectrum; `energy` drives what readEnergy() computes.
// Bins 3..37 are the 300-3400 Hz voice band at 48 kHz / 256 bins, and one
// tall bin keeps peak/avg above the 1.25 "this is speech" ratio.
let energy = 0;
let rafCb = null;

class FakeAudioContext {
  constructor() { this.state = 'running'; this.sampleRate = 48000; }
  resume() { return Promise.resolve(); }
  close() {}
  createMediaStreamSource() { return { connect() {} }; }
  createAnalyser() {
    return {
      fftSize: 0, smoothingTimeConstant: 0, frequencyBinCount: 256,
      getByteFrequencyData(buf) {
        buf.fill(0);
        if (!energy) return;
        for (let i = 3; i <= 37; i++) buf[i] = energy;
        buf[20] = Math.min(255, energy * 3);
      },
    };
  }
}

function fakeStream() {
  const track = { readyState: 'live', enabled: true, stop() { this.readyState = 'ended'; } };
  return { getTracks: () => [track], getAudioTracks: () => [track] };
}

let clock;
let fetchSpy;

function installBrowser() {
  fetchSpy = vi.fn(async () => ({
    ok: true, status: 200,
    json: async () => ({ text: 'turn the lamp on', trace_id: 't-9' }),
    text: async () => '',
  }));
  const globals = {
    window: globalThis,
    location: { origin: 'https://clarp.test' },
    isSecureContext: true,
    MediaRecorder: FakeMediaRecorder,
    AudioContext: FakeAudioContext,
    navigator: { mediaDevices: { getUserMedia: async () => fakeStream() } },
    requestAnimationFrame: cb => { rafCb = cb; return 1; },
    cancelAnimationFrame: () => { rafCb = null; },
    fetch: (...a) => fetchSpy(...a),
  };
  for (const [name, value] of Object.entries(globals)) vi.stubGlobal(name, value);
}

async function loadStore() {
  vi.resetModules();
  return import('../../web/src/stores/mic.svelte.js');
}

beforeEach(() => {
  recorders.length = 0;
  recorderThrows = false;
  energy = 0;
  rafCb = null;
  flash.mockClear(); clog.mockClear(); sendText.mockClear();
  machine.startRecording.mockClear();
  machine.endRecording.mockClear();
  machine.settle.mockClear();
  machine.startRecording.mockImplementation(() => {});
  machine.endRecording.mockImplementation(() => {});
  machine.settle.mockImplementation(() => {});
  // Fake timers first: vitest's fake clock installs its own
  // requestAnimationFrame, which would otherwise replace the stub below and
  // leave the VAD loop undrivable.
  vi.useFakeTimers();
  installBrowser();
  clock = 1_000_000;
  vi.spyOn(Date, 'now').mockImplementation(() => clock);
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

/** One VAD frame at the current `energy`, `ms` after the previous one. */
function vadFrame(ms = 100) {
  clock += ms;
  const cb = rafCb;
  rafCb = null;
  cb && cb();
}

/** Bring always-on listening up to a live VAD-triggered capture. */
async function alwaysOnCapturing(m) {
  await m.startAlwaysOn();            // runs one vadTick
  energy = 70;                        // above VAD_ENERGY_ON (50)
  vadFrame(50);                       // arms energyAboveSince
  vadFrame(Timing.VAD_ENERGY_ON_MS + 50);  // sustained -> startCapture()
  expect(m.mic.capturing).toBe(true);
  return recorders[recorders.length - 1];
}

describe('a recorder that never fires onstop', () => {
  // Axiom: the watchdog exists and does fire (mic.svelte.js:192-200).
  it('is caught by captureStopWatchdog', async () => {
    const m = await loadStore();
    await alwaysOnCapturing(m);
    energy = 0;
    vadFrame(100);                                   // silenceStartedAt
    vadFrame(Timing.SILENCE_END_MS + 100);           // -> stopCapture()
    await vi.advanceTimersByTimeAsync(Timing.CAPTURE_STOP_WATCHDOG_MS + 10);
    expect(clog).toHaveBeenCalledWith('captureStopWatchdog', expect.anything());
    expect(flash).toHaveBeenCalledWith('Recorder stalled — tap again', 2500);
  });

  // BUG. The watchdog calls teardownMic() (mic.svelte.js:197), which drops
  // the stream, the AudioContext and `analyser` and clears `mic.capturing`
  // — but it does not touch `alwaysOn`, `singleShot` or `mic.recording`.
  //
  // Two things follow. vadTick() bails at its `!analyser` guard
  // (mic.svelte.js:291) so the rAF loop stops: always-on listening is over
  // and will never restart. And `mic.recording` stays true, so the button
  // still shows a live green mic. The toast says "tap again"; the next tap
  // goes to micTap() :347, sees `alwaysOn` still true, and routes to
  // stopAlwaysOn() — the user is told to tap, taps, and is told "Listening
  // off". It takes two more taps to get recording back, and nothing on
  // screen explains why.
  //
  // FIX: have the watchdog return the store to a truthful idle — alwaysOn =
  // false, singleShot = false, mic.recording = false — or, better, restart
  // listening itself, since the user asked for always-on and never asked to
  // stop.
  it.fails('leaves the mic honest about not listening any more', async () => {
    const m = await loadStore();
    await alwaysOnCapturing(m);
    energy = 0;
    vadFrame(100);
    vadFrame(Timing.SILENCE_END_MS + 100);
    await vi.advanceTimersByTimeAsync(Timing.CAPTURE_STOP_WATCHDOG_MS + 10);
    expect(m.mic.capturing).toBe(false);
    expect(m.mic.recording).toBe(false);
  });

  // BUG, same cause seen from the user's side: after the stall the very next
  // tap should start recording, because that is what the toast asked for.
  // Today it turns listening off instead and starts nothing.
  //
  // FIX: as above — clearing `alwaysOn` in the watchdog makes this pass.
  it.fails('lets the next tap start recording, as the toast promises', async () => {
    const m = await loadStore();
    await alwaysOnCapturing(m);
    energy = 0;
    vadFrame(100);
    vadFrame(Timing.SILENCE_END_MS + 100);
    await vi.advanceTimersByTimeAsync(Timing.CAPTURE_STOP_WATCHDOG_MS + 10);
    const before = recorders.length;
    await m.micTap();
    expect(recorders.length).toBe(before + 1);
  });
});

describe('a MediaRecorder that refuses to be constructed', () => {
  // BUG. startCapture() logs and flashes, then returns (mic.svelte.js:
  // 159-163 and 170-175). But micTap() has already set `alwaysOn = true`,
  // `singleShot = true` and `mic.recording = true` (mic.svelte.js:350-353)
  // and calls vadTick() afterwards regardless. The mic button is green, the
  // VAD loop is running, and no clip will ever be recorded — which is
  // exactly the invisible state issue #13 described, reached by a different
  // route. The next tap says "Listening off".
  //
  // FIX: startCapture() should report failure to its callers, and micTap()
  // /startAlwaysOn() should unwind their own flags when it does.
  it.fails('does not leave the mic button green over a dead recorder', async () => {
    const m = await loadStore();
    recorderThrows = true;
    await m.micTap();
    expect(clog).toHaveBeenCalledWith('recorderFail', expect.stringContaining('MediaRecorder'));
    expect(m.mic.recording).toBe(false);
  });
});

describe('hands-free is a property of the capture, not of the moment it ends', () => {
  // BUG. onCaptureEnd() computes `isHandsFree = alwaysOn && !singleShot`
  // (mic.svelte.js:240) at upload time, several seconds after the clip was
  // recorded. If the user taps the mic to stop listening while a VAD capture
  // is in flight, stopAlwaysOn() clears `alwaysOn` (mic.svelte.js:334)
  // before onstop fires, so the utterance is uploaded with
  // `X-Hands-Free: 0` and the server routes it as a deliberate typed-style
  // send rather than a hands-free one.
  //
  // This is the same shape as issue #13 — a flag cleared before its reader —
  // and the store already knows the right answer: `send.captureTarget` is
  // latched at startCapture() (mic.svelte.js:179) precisely so a mid-capture
  // change cannot redirect the utterance (state-machine.js:10, "B14: target
  // locks at startCapture, not at send time").
  //
  // FIX: latch `capturedHandsFree` next to `send.captureTarget` in
  // startCapture() and read that in onCaptureEnd().
  it.fails('keeps the hands-free flag a tap-to-stop cannot revoke', async () => {
    const m = await loadStore();
    const recorder = await alwaysOnCapturing(m);
    clock += 5_000;
    await m.micTap();               // second tap: stop listening
    expect(recorder.stops).toBe(1);
    await recorder.finish();
    const [, init] = fetchSpy.mock.calls[0];
    expect(init.headers['X-Hands-Free']).toBe('1');
  });
});

describe('a transcription that fails', () => {
  // BUG. Both failure branches of the /transcribe round trip only call
  // flash(): the non-ok reply at mic.svelte.js:258-261 and the catch (which
  // also covers the 45s abort) at :262-263. Neither writes a clog row.
  //
  // The toast is gone in 3.5 seconds and never leaves the device, so a user
  // reporting "it stopped hearing me" produces a client log with a capture
  // that starts and then simply nothing — the same blind spot issue #13
  // called out for startCapture()'s silent exits, on the path that carries
  // every server-side STT failure (model missing, OOM, timeout).
  //
  // FIX: clog('sttFail', `status=${r.status} ${body.slice(0,120)}`) and
  // clog('sttError', err.name + ': ' + err.message) alongside the flashes.
  it.fails('leaves a client-log row behind, not just a toast', async () => {
    const m = await loadStore();
    fetchSpy.mockImplementation(async () => ({
      ok: false, status: 503, text: async () => 'no stt model installed',
      json: async () => ({}),
    }));
    const recorder = await alwaysOnCapturing(m);
    clock += 5_000;
    energy = 0;
    vadFrame(100);
    vadFrame(Timing.SILENCE_END_MS + 100);
    await recorder.finish();
    expect(flash).toHaveBeenCalledWith(expect.stringContaining('stt 503'), expect.anything());
    expect(clog).toHaveBeenCalled();
  });
});

describe('an illegal conversation-state transition', () => {
  // BUG. createStateMachine() throws on a transition it does not allow
  // (static/lib/state-machine.js:52-54) — deliberately, so a desync is loud.
  // Every caller then swallows it: mic.svelte.js:186, :198, :208, :226, :286
  // and send.svelte.js:81, :84 are all `try { machine.x() } catch (_) {}`.
  //
  // So the one signal that the audio queue's model of the conversation has
  // drifted from reality is discarded at every call site. A machine stuck in
  // `awaiting` holds the playback queue (the agent replies and nothing is
  // heard) and there is no client-side evidence of how it got there.
  //
  // FIX: a shared helper — `function step(fn, what) { try { fn(); } catch (e)
  // { clog('machineIllegal', `${what}: ${e.message}`); } }` — used by both
  // stores, so the throw stays fatal to the transition and visible in the log.
  it.fails('is recorded rather than swallowed', async () => {
    const m = await loadStore();
    machine.endRecording.mockImplementation(() => {
      throw new Error('Illegal transition: idle → transcribing');
    });
    const recorder = await alwaysOnCapturing(m);
    clock += 5_000;
    energy = 0;
    vadFrame(100);
    vadFrame(Timing.SILENCE_END_MS + 100);
    await recorder.finish();
    const events = clog.mock.calls.map(c => String(c[0]));
    expect(events.some(e => /machine|transition|illegal/i.test(e))).toBe(true);
  });
});
