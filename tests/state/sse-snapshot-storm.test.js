// A cold page's event stream replays the last minutes of events. Each
// agent-roster event used to fetch /agents/snapshot on its own, so a replay
// with ~25 helper-state roster events fired ~25 snapshot requests at once
// (about 140 ms of Host time each) while the page was trying to load its
// chat. A burst of roster events must cost one coalesced refresh.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const refreshAgentSnapshot = vi.fn(() => Promise.resolve({}));
vi.mock('../../web/src/lib/net.js', () => ({ clog: vi.fn(), noteSseEvent: vi.fn(), withToken: u => u }));
vi.mock('../../web/src/stores/app.svelte.js', () => ({
  agentSnapshot: { remove: vi.fn(), patchState: vi.fn(), patchActivity: vi.fn() },
  app: { session: 'rachel' }, chipLabel: s => s, flash: vi.fn(), mirrorFocus: vi.fn(),
  refreshAgentSnapshot, rememberUserNotification: vi.fn(), setConn: vi.fn(),
  setVersion: vi.fn(), syncStatus: vi.fn(),
}));
vi.mock('../../web/src/stores/conversations.svelte.js', () => ({
  appendActivity: vi.fn(), appendThinking: vi.fn(), handleSseEvent: vi.fn(),
  refreshAll: vi.fn(), removeLiveThinking: vi.fn(), wake: vi.fn(),
}));
vi.mock('../../web/src/stores/audio.svelte.js', () => ({
  audio: {}, bumpLastAudioTs: vi.fn(), lastAudioTs: 0, PLAYER_ADAPTER_VERSION: 't',
  scheduler: {}, unlockAudio: vi.fn(), addConditionSource: vi.fn(),
}));

let sources;
class FakeEventSource {
  constructor(url) { this.url = url; this.readyState = 0; sources.push(this); }
  close() { this.readyState = 2; }
}

beforeEach(() => {
  vi.useFakeTimers();
  sources = [];
  refreshAgentSnapshot.mockClear();
  vi.stubGlobal('EventSource', FakeEventSource);
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 404, json: async () => ({}) })));
  vi.stubGlobal('localStorage', { getItem: () => null, setItem: () => {} });
});
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

describe('agent-roster events', () => {
  it('a replayed burst costs one snapshot refresh, not one per event', async () => {
    vi.resetModules();
    const { connectSSE } = await import('../../web/src/stores/sse.svelte.js');
    connectSSE();
    const es = sources[0];
    for (let i = 0; i < 25; i++) {
      es.onmessage({ data: JSON.stringify({ type: 'agent-roster', kind: 'helper-state', session: `h-${i}` }), lastEventId: String(i) });
    }
    await vi.advanceTimersByTimeAsync(1000);
    expect(refreshAgentSnapshot).toHaveBeenCalledTimes(1);
  });
});
