// The two wires of a waiting send (feature waited_send_order), each driven
// through its real entry point:
//  - sendText reads `queued: true` from the real /send answer and marks the
//    send waiting, so the 20 s sweep never calls it failed;
//  - the event stream's queue-updated with queue_change 'deleted' drops the
//    waiting send and its bubble.
// Each test fails if its one line is removed (send.svelte.js:
// `if (d && d.queued) noteSendQueued(clientMsgId)`; sse.svelte.js: the
// QUEUE_UPDATED branch).

import { beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../../web/src/lib/net.js', () => ({
  clog() {}, noteSseEvent() {}, withToken: url => url, instanceId: () => 'x',
  trace: { id: '' },
}));
vi.mock('../../web/src/stores/app.svelte.js', () => ({
  app: { session: 'mike' }, flash() {}, setSession: async () => {},
  agentSnapshot: {}, chipLabel() {}, mirrorFocus() {}, refreshAgentSnapshot: async () => {},
  rememberUserNotification() {}, setConn() {}, setVersion() {}, syncStatus() {}, statusFor: () => ({}),
}));
vi.mock('../../web/src/stores/audio.svelte.js', () => ({
  audio: { muted: true }, chime() {}, machine: { settle() {}, send() {} },
  playerAdapter: null, scheduler: null, bumpLastAudioTs() {}, lastAudioTs: 0,
  PLAYER_ADAPTER_VERSION: 'x', unlockAudio() {}, addConditionSource() {},
}));
vi.mock('../../web/src/stores/conversations.svelte.js', () => ({
  addOptimisticTurn() {}, appendActivity() {}, appendThinking() {}, handleSseEvent() {},
  refreshAll() {}, removeLiveThinking() {}, wake() {}, ensureLoaded() {},
  reconcileWithSnapshot() {},
}));
vi.mock('../../web/src/stores/live.svelte.js', () => ({
  handleLiveEvent() {}, liveFor() {}, liveQuery() { return ''; }, liveTurnRunning() { return false; },
  loadExplanationSettings() {}, onSubscriptionsChanged() {}, onTurnSettled() {}, openLive() {},
  setConnectedLive() {}, setServerFeatures() {}, watchedSessions() { return []; },
}));

const streams = [];
class FakeEventSource {
  constructor(url) { this.url = url; this.readyState = 1; streams.push(this); }
  close() { this.readyState = 2; }
}

let sendText;
let connectSSE;
let delivery;
let bindDeliveryWaiting;
const waiting = [];
const dropped = [];

beforeAll(async () => {
  vi.stubGlobal('localStorage', { getItem: () => null, setItem() {} });
  vi.stubGlobal('document', { documentElement: { classList: { contains: () => false } } });
  vi.stubGlobal('EventSource', FakeEventSource);
  ({ sendText } = await import('../../web/src/stores/send.svelte.js'));
  ({ connectSSE } = await import('../../web/src/stores/sse.svelte.js'));
  ({ delivery, bindDeliveryWaiting } = await import('../../web/src/stores/delivery.svelte.js'));
  bindDeliveryWaiting((session, id) => waiting.push(id), (session, id) => dropped.push(id));
});

beforeEach(() => {
  waiting.length = 0;
  dropped.length = 0;
  delivery.entries = [];
});

function answer(body) {
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, status: 200, json: async () => body })));
}

describe('sendText reads queued from the /send answer', () => {
  it('marks a send answered queued:true as waiting', async () => {
    answer({ ok: true, session: 'mike', queued: true, queue_depth: 1, queue_revision: 3 });
    await sendText('second message');
    const [entry] = delivery.entries;
    expect(entry.waiting).toBe(true);
    expect(waiting).toEqual([entry.serverId]);
  });

  it('leaves a send that started at once to the sweep', async () => {
    answer({ ok: true, session: 'mike', queued: false });
    await sendText('first message');
    expect(delivery.entries[0].waiting).toBeUndefined();
    expect(waiting).toEqual([]);
  });
});

describe('queue-updated through the event stream', () => {
  it('drops a waiting send the queue says was deleted', async () => {
    answer({ ok: true, session: 'mike', queued: true, queue_depth: 1, queue_revision: 3 });
    await sendText('to be deleted');
    const [entry] = delivery.entries;
    connectSSE();
    const es = streams[streams.length - 1];
    es.onmessage({ data: JSON.stringify({
      type: 'queue-updated', session: 'mike', agent_id: 'a', queue_depth: 0,
      queue_paused: false, queue_started: false, queue_revision: 4,
      client_msg_id: entry.id, queue_change: 'deleted' }) });
    expect(delivery.entries.find(e => e.id === entry.id)).toBeUndefined();
    expect(dropped).toEqual([entry.serverId]);
  });

  it('keeps it for any other queue change', async () => {
    answer({ ok: true, session: 'mike', queued: true, queue_depth: 1, queue_revision: 3 });
    await sendText('edited only');
    const [entry] = delivery.entries;
    connectSSE();
    const es = streams[streams.length - 1];
    es.onmessage({ data: JSON.stringify({
      type: 'queue-updated', session: 'mike', agent_id: 'a', queue_depth: 1,
      queue_paused: false, queue_started: false, queue_revision: 4,
      client_msg_id: entry.id, queue_change: 'edited', text: 'edited only, better' }) });
    expect(delivery.entries.find(e => e.id === entry.id)).toBeDefined();
    expect(dropped).toEqual([]);
  });
});
