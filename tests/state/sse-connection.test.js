import { beforeAll, describe, expect, it, vi } from 'vitest';

// One page holds one event stream. A second connect (the boot effect re-running
// when the open chat changes) must replace the stream, not add another: on
// HTTP/1.1 every extra stream takes one of the browser's six connections to
// the Host, and after a handful of chat switches /log requests stall behind them.
vi.mock('../../web/src/lib/net.js', () => ({
  clog() {}, noteSseEvent() {}, withToken: url => url, instanceId: () => 'x',
}));
vi.mock('../../web/src/stores/conversations.svelte.js', () => ({
  appendActivity() {}, appendThinking() {}, handleSseEvent() {}, refreshAll() {},
  removeLiveThinking() {}, wake() {}, ensureLoaded() {}, reconcileWithSnapshot() {},
}));
vi.mock('../../web/src/stores/audio.svelte.js', () => ({
  audio: {}, bumpLastAudioTs() {}, lastAudioTs: 0, PLAYER_ADAPTER_VERSION: 'x',
  scheduler: {}, unlockAudio() {}, addConditionSource() {},
}));

const streams = [];
class FakeEventSource {
  constructor(url) { this.url = url; this.readyState = 0; this.closed = false; streams.push(this); }
  close() { this.closed = true; this.readyState = 2; }
}

let connectSSE;
beforeAll(async () => {
  vi.stubGlobal('localStorage', { getItem: () => null, setItem() {} });
  vi.stubGlobal('document', { documentElement: { classList: { contains: () => false } } });
  vi.stubGlobal('EventSource', FakeEventSource);
  ({ connectSSE } = await import('../../web/src/stores/sse.svelte.js'));
});

describe('event stream connection', () => {
  it('keeps one open stream however often it is connected', () => {
    connectSSE();
    connectSSE();
    connectSSE();
    expect(streams.filter(s => !s.closed)).toHaveLength(1);
  });
});
