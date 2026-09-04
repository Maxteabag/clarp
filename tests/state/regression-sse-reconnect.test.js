// Unresolved SSE ownership regressions: cursor continuity, timer coalescing,
// and the unauthorized reconnect circuit breaker.
import { beforeEach, describe, expect, it, vi } from 'vitest';

const clog = vi.fn();
const noteSseEvent = vi.fn();
const setConn = vi.fn();
const app = { authRejected: false, session: 'mike' };

vi.mock('../../web/src/lib/net.js', () => ({
  clog,
  noteSseEvent,
  withToken: url => url,
}));
vi.mock('../../web/src/stores/app.svelte.js', () => ({
  agentSnapshot: { patchState() {}, patchActivity() {}, remove() {} },
  app,
  chipLabel: value => value,
  flash() {},
  mirrorFocus() {},
  refreshAgentSnapshot: async () => ({}),
  rememberUserNotification() {},
  setConn,
  setVersion() {},
  syncStatus() {},
}));
vi.mock('../../web/src/stores/conversations.svelte.js', () => ({
  appendActivity() {},
  appendThinking() {},
  handleSseEvent() {},
  refreshAll() {},
  removeLiveThinking() {},
  wake() {},
}));
vi.mock('../../web/src/stores/audio.svelte.js', () => ({
  audio: { muted: false },
  bumpLastAudioTs() {},
  lastAudioTs: 0,
  PLAYER_ADAPTER_VERSION: 'test',
  scheduler: { ingest: () => ({ accepted: true }) },
  unlockAudio() {},
  addConditionSource() {},
}));

class FakeEventSource {
  static instances = [];

  constructor(url) {
    this.url = url;
    this.readyState = 0;
    this.closed = false;
    FakeEventSource.instances.push(this);
  }

  close() { this.closed = true; }
}

let timeouts;
let nextTimeoutId;

async function loadStore() {
  vi.resetModules();
  return import('../../web/src/stores/sse.svelte.js');
}

beforeEach(() => {
  FakeEventSource.instances = [];
  timeouts = [];
  nextTimeoutId = 1;
  app.authRejected = false;
  clog.mockClear();
  noteSseEvent.mockClear();
  setConn.mockClear();
  vi.stubGlobal('EventSource', FakeEventSource);
  vi.stubGlobal('setTimeout', fn => {
    const id = nextTimeoutId++;
    timeouts.push({ id, fn });
    return id;
  });
  vi.stubGlobal('clearTimeout', id => {
    timeouts = timeouts.filter(timer => timer.id !== id);
  });
  vi.stubGlobal('setInterval', () => 1);
  vi.stubGlobal('clearInterval', () => {});
  vi.stubGlobal('localStorage', {
    getItem: () => null,
    setItem() {},
  });
  vi.stubGlobal('location', { reload() {} });
});

describe('SSE reconnect ownership', () => {
  it('carries the last durable event id into a replacement EventSource', async () => {
    const store = await loadStore();
    store.connectSSE();
    const first = FakeEventSource.instances[0];
    first.onmessage({
      data: JSON.stringify({ type: 'agent-state', session: 'mike', kind: 'idle' }),
      lastEventId: '417',
    });

    store.scheduleReconnect();
    timeouts.shift().fn();

    expect(FakeEventSource.instances).toHaveLength(2);
    expect(FakeEventSource.instances[1].url).toContain('last_event_id=417');
  });

  it('coalesces repeated reconnect requests into one replacement stream', async () => {
    const store = await loadStore();
    store.connectSSE();

    store.scheduleReconnect();
    store.scheduleReconnect();
    expect(timeouts).toHaveLength(1);
    timeouts.shift().fn();

    expect(FakeEventSource.instances).toHaveLength(2);
  });

  it('stops retrying while the server is known to reject authentication', async () => {
    const store = await loadStore();
    store.connectSSE();
    app.authRejected = true;

    FakeEventSource.instances[0].onerror();

    expect(timeouts).toHaveLength(0);
    expect(setConn).toHaveBeenLastCalledWith('unauthorized', expect.anything());
  });
});
