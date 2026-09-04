// Issue #13: tap-to-record never uploaded. stopAlwaysOn() cleared
// mic.capturing one line before calling stopCapture(), whose guard reads that
// flag, so recorder.stop() was never reached, onstop never fired and the blob
// was never POSTed. These drive the store through the same entry points the
// button and the remote action use, against a fake MediaRecorder.

import { describe, it, expect, beforeEach, vi } from 'vitest';

const flash = vi.fn();
const clog = vi.fn();
const sendText = vi.fn();
const unlockAudio = vi.fn();

vi.mock('../../web/src/lib/net.js', () => ({ clog, trace: { id: '' } }));
vi.mock('../../web/src/stores/app.svelte.js', () => ({
  app: { session: 'omar' },
  flash,
}));
vi.mock('../../web/src/stores/audio.svelte.js', () => ({
  machine: { startRecording() {}, endRecording() {}, settle() {} },
  playerAdapter: { interrupt() {} },
  scheduler: { flushOlderThan() {} },
  tick() {},
  unlockAudio,
  addConditionSource() {},
}));
vi.mock('../../web/src/stores/send.svelte.js', () => ({
  send: { captureTarget: '', pendingHandsFree: false },
  sendText,
}));

const recorders = [];

class FakeMediaRecorder {
  static isTypeSupported() { return true; }
  constructor(stream, opts = {}) {
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
  // Hand the store a chunk and fire onstop, as the browser does after stop().
  async finish(bytes = 4096) {
    const data = new Blob([new Uint8Array(bytes)], { type: this.mimeType });
    this.ondataavailable && this.ondataavailable({ data });
    if (this.onstop) await this.onstop();
  }
}

function fakeStream() {
  const track = { readyState: 'live', enabled: true, stop() { this.readyState = 'ended'; } };
  return { getTracks: () => [track], getAudioTracks: () => [track] };
}

class FakeAudioContext {
  constructor() { this.state = 'running'; this.sampleRate = 48000; }
  resume() { return Promise.resolve(); }
  close() {}
  createMediaStreamSource() { return { connect() {} }; }
  createAnalyser() {
    return {
      fftSize: 0, smoothingTimeConstant: 0, frequencyBinCount: 256,
      getByteFrequencyData(buf) { buf.fill(0); },
    };
  }
}

function installBrowser() {
  const fetchSpy = vi.fn(async () => ({
    ok: true,
    json: async () => ({ text: 'hello there', trace_id: 't-1' }),
    text: async () => '',
  }));
  const globals = {
    window: globalThis,
    location: { origin: 'https://clarp.test' },
    isSecureContext: true,
    MediaRecorder: FakeMediaRecorder,
    AudioContext: FakeAudioContext,
    navigator: { mediaDevices: { getUserMedia: async () => fakeStream() } },
    requestAnimationFrame: () => 1,
    cancelAnimationFrame: () => {},
    fetch: fetchSpy,
  };
  for (const [name, value] of Object.entries(globals)) vi.stubGlobal(name, value);
  return fetchSpy;
}

async function loadStore() {
  vi.resetModules();
  return import('../../web/src/stores/mic.svelte.js');
}

let fetchSpy;
let clock;

beforeEach(() => {
  recorders.length = 0;
  flash.mockClear();
  clog.mockClear();
  sendText.mockClear();
  fetchSpy = installBrowser();
  clock = 1_000_000;
  vi.spyOn(Date, 'now').mockImplementation(() => clock);
});

async function tapAndSpeak(start) {
  await start();
  expect(recorders).toHaveLength(1);
  const recorder = recorders[0];
  expect(recorder.starts).toBe(1);
  // Speak for long enough to clear MIN_UTTER_MS.
  clock += 5_000;
  return recorder;
}

describe('tap-to-record', () => {
  it('second tap stops the recorder', async () => {
    const m = await loadStore();
    const recorder = await tapAndSpeak(() => m.micTap());
    await m.micTap();
    expect(recorder.stops).toBe(1);
  });

  it('uploads the clip to /transcribe once the recorder stops', async () => {
    const m = await loadStore();
    const recorder = await tapAndSpeak(() => m.micTap());
    await m.micTap();
    await recorder.finish();
    expect(fetchSpy).toHaveBeenCalledTimes(1);
    const [url, init] = fetchSpy.mock.calls[0];
    expect(url).toBe('/transcribe');
    expect(init.method).toBe('POST');
    expect(init.body.size).toBeGreaterThan(1024);
    expect(sendText).toHaveBeenCalledWith('hello there', expect.anything());
    expect(m.mic.capturing).toBe(false);
    expect(m.mic.recording).toBe(false);
  });

  it('remote-action toggle stops and uploads too', async () => {
    const m = await loadStore();
    m.toggleRecord();
    // toggleRecord calls micTap() without awaiting; let ensureMic settle.
    await new Promise(r => setTimeout(r, 0));
    const recorder = recorders[0];
    expect(recorder.starts).toBe(1);
    clock += 5_000;
    m.toggleRecord();
    expect(recorder.stops).toBe(1);
    await recorder.finish();
    expect(fetchSpy).toHaveBeenCalledTimes(1);
    expect(fetchSpy.mock.calls[0][0]).toBe('/transcribe');
  });
});
