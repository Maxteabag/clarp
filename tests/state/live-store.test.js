// The web's live channel (docs/live-items.md §4, §5, §7): which chats it
// subscribes to, GET /live on open and after a gap, events applied once per
// frame, and the Host's tool-explanation setting.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

vi.mock('../../web/src/lib/net.js', () => ({ clog: vi.fn() }));

const here = path.dirname(fileURLToPath(import.meta.url));
const turnFull = JSON.parse(fs.readFileSync(
  path.join(here, '..', '..', 'contract', 'live', 'turn-full.json'), 'utf8'));
const snapshotOf = (lseq, extra = {}) => ({ ...turnFull.steps[0].snapshot, lseq, ...extra });
const eventsOf = turnFull.steps.slice(1).map(s => s.event);
const event = lseq => ({ ...eventsOf.find(e => e.lseq === lseq) });

let frames;
let requests;
let routes;

function installGlobals() {
  frames = [];
  requests = [];
  routes = {};
  vi.stubGlobal('requestAnimationFrame', cb => { frames.push(cb); return frames.length; });
  vi.stubGlobal('fetch', vi.fn(async (url, init = {}) => {
    requests.push({ url: String(url), method: init.method || 'GET', body: init.body });
    const route = Object.keys(routes).find(r => String(url).startsWith(r));
    const answer = route ? await routes[route](init) : { status: 404, body: {} };
    return { ok: answer.status === 200, status: answer.status, json: async () => answer.body };
  }));
}

const runFrames = () => { const now = frames; frames = []; now.forEach(cb => cb(0)); };
const settle = () => new Promise(r => setTimeout(r, 0));
const liveGets = () => requests.filter(r => r.url.startsWith('/live?'));

async function freshStore() {
  vi.resetModules();
  return import('../../web/src/stores/live.svelte.js');
}

describe('live store', () => {
  beforeEach(installGlobals);
  afterEach(() => vi.unstubAllGlobals());

  it('stays off the live channel when the Host does not offer live items', async () => {
    const store = await freshStore();
    store.setServerFeatures(['tool_explanations']);
    store.watchSession('rachel');
    expect(store.liveQuery()).toBe('');
    store.handleLiveEvent(event(1));
    runFrames();
    await settle();
    expect(store.liveFor('rachel')).toBe(null);
    expect(liveGets()).toHaveLength(0);
  });

  it('subscribes to the chats on screen and the few opened last', async () => {
    const store = await freshStore();
    const changed = vi.fn();
    store.onSubscriptionsChanged(changed);
    store.setServerFeatures(['live_items']);
    expect(store.liveQuery()).toBe('live=');
    for (const s of ['a', 'b', 'c', 'd', 'e']) store.watchSession(s);
    expect(store.liveQuery()).toBe('live=b,c,d,e');
    store.watchSession('c');
    expect(store.liveQuery()).toBe('live=b,c,d,e');
    expect(changed).toHaveBeenCalledTimes(6);
  });

  it('opens a chat from GET /live and applies the events after it', async () => {
    const store = await freshStore();
    routes['/live?'] = () => ({ status: 200, body: snapshotOf(0) });
    store.setServerFeatures(['live_items']);
    store.watchSession('rachel');
    await store.openLive('rachel');
    for (const lseq of [1, 2, 3, 4]) store.handleLiveEvent(event(lseq));
    expect(store.liveFor('rachel').lseq).toBe(0);
    runFrames();
    const live = store.liveFor('rachel');
    expect(live.lseq).toBe(4);
    expect(live.items['cl:msg_01:0'].title).toBe('Finding the flaky test');
    expect(store.liveTurnRunning('rachel')).toBe(true);
    expect(liveGets()).toHaveLength(1);
  });

  it('keeps events that arrive while the snapshot is on its way', async () => {
    const store = await freshStore();
    let release;
    routes['/live?'] = () => new Promise(r => { release = () => r({ status: 200, body: snapshotOf(1) }); });
    store.setServerFeatures(['live_items']);
    store.watchSession('rachel');
    const opened = store.openLive('rachel');
    for (const lseq of [1, 2, 3]) store.handleLiveEvent(event(lseq));
    runFrames();
    await settle();
    release();
    await opened;
    runFrames();
    expect(store.liveFor('rachel').lseq).toBe(3);
    expect(liveGets()).toHaveLength(1);
  });

  it('asks for one snapshot after a gap and carries on from it', async () => {
    const store = await freshStore();
    let snap = snapshotOf(0);
    routes['/live?'] = () => ({ status: 200, body: snap });
    store.setServerFeatures(['live_items']);
    store.watchSession('rachel');
    await store.openLive('rachel');
    store.handleLiveEvent(event(1));
    store.handleLiveEvent(event(3));
    store.handleLiveEvent(event(4));
    snap = snapshotOf(4, { items: [] });
    runFrames();
    await settle();
    await settle();
    store.handleLiveEvent({ ...event(5), ops: [{ op: 'status', conv: 'conv-1', activity: { state: 'thinking' } }] });
    runFrames();
    expect(liveGets()).toHaveLength(2);
    expect(store.liveFor('rachel').lseq).toBe(5);
  });

  it('tracks status alone for chats it does not subscribe to', async () => {
    const store = await freshStore();
    store.setServerFeatures(['live_items']);
    store.handleLiveEvent({ ...event(13), session: 'mike' });
    runFrames();
    expect(store.liveFor('mike')).toBe(null);
    expect(store.rosterActivity('mike')).toMatchObject({ state: 'tool', headline: 'Running npm test' });
    expect(store.liveTurnRunning('mike')).toBe(true);
    expect(liveGets()).toHaveLength(0);
  });

  it('turns tool explanations off on the Host and says so everywhere', async () => {
    const store = await freshStore();
    routes['/tool-explanations/settings'] = init => ({ status: 200,
      body: { enabled: init.body ? JSON.parse(init.body).enabled : true, detail_level: 2 } });
    store.setServerFeatures(['live_items']);
    await store.loadExplanationSettings();
    expect(store.live.explanations.enabled).toBe(true);
    await store.setExplanationsEnabled(false);
    expect(requests.at(-1)).toMatchObject({ method: 'POST', body: JSON.stringify({ enabled: false }) });
    expect(store.live.explanations.enabled).toBe(false);
  });

  it('puts the switch back when the Host refuses it', async () => {
    const store = await freshStore();
    routes['/tool-explanations/settings'] = init => (init.method === 'POST'
      ? { status: 500, body: {} } : { status: 200, body: { enabled: true, detail_level: 2 } });
    store.setServerFeatures(['live_items']);
    await store.loadExplanationSettings();
    await store.setExplanationsEnabled(false);
    expect(store.live.explanations.enabled).toBe(true);
  });
});

describe('turn end', () => {
  beforeEach(installGlobals);
  afterEach(() => vi.unstubAllGlobals());

  it('tells the app once when a running turn settles', async () => {
    const store = await freshStore();
    const settled = vi.fn();
    store.onTurnSettled(settled);
    routes['/live?'] = () => ({ status: 200, body: snapshotOf(22) });
    store.setServerFeatures(['live_items']);
    store.watchSession('rachel');
    await store.openLive('rachel');
    store.handleLiveEvent({ ...event(13), lseq: 23, ops: event(13).ops.filter(o => o.op === 'status') });
    runFrames();
    expect(settled).not.toHaveBeenCalled();
    store.handleLiveEvent({ ...event(23), lseq: 24, ops: event(23).ops.filter(o => o.op === 'status' || o.op === 'turn') });
    runFrames();
    expect(settled).toHaveBeenCalledTimes(1);
    expect(settled).toHaveBeenCalledWith('rachel');
  });
});

describe('the connection that carries a chat', () => {
  beforeEach(installGlobals);
  afterEach(() => vi.unstubAllGlobals());

  it('takes a chat’s events as status only until a connection subscribed to it is open', async () => {
    const store = await freshStore();
    routes['/live?'] = () => ({ status: 200, body: snapshotOf(1) });
    store.setServerFeatures(['live_items']);
    store.setConnectedLive([]);
    store.watchSession('rachel');
    await store.openLive('rachel');
    // The old connection only carries the summary of lseq 2: status and turn ops.
    store.handleLiveEvent({ ...event(2), ops: [{ op: 'status', conv: 'conv-1', activity: { state: 'thinking', headline: 'Thinking' } }] });
    runFrames();
    expect(store.liveFor('rachel').lseq).toBe(1);
    expect(store.rosterActivity('rachel')).toMatchObject({ state: 'thinking' });
    store.setConnectedLive(['rachel']);
    await settle(); await settle();            // the fresh snapshot for the carried chat
    store.handleLiveEvent(event(2));
    runFrames();
    expect(store.liveFor('rachel').lseq).toBe(2);
    expect(store.liveFor('rachel').items['cl:msg_01:0']).toBeTruthy();
  });
});

describe('a snapshot taken before the stream carried the chat', () => {
  beforeEach(installGlobals);
  afterEach(() => vi.unstubAllGlobals());

  it('is asked for again once the stream carries the chat, even on a quiet stream', async () => {
    const store = await freshStore();
    let lseq = 1;
    routes['/live?'] = () => ({ status: 200, body: snapshotOf(lseq) });
    store.setServerFeatures(['live_items']);
    store.setConnectedLive([]);
    store.watchSession('rachel');
    await store.openLive('rachel');            // the chat opened while the stream was reconnecting
    lseq = 4;                                  // events 2..4 went by before the new stream carried it
    store.setConnectedLive(['rachel']);
    await settle(); await settle();
    expect(liveGets()).toHaveLength(2);
    expect(store.liveFor('rachel').lseq).toBe(4);
  });

  it('is asked for again after it lands when the stream starts carrying the chat mid-fetch', async () => {
    const store = await freshStore();
    const answers = [];
    routes['/live?'] = () => new Promise(r => answers.push(r));
    store.setServerFeatures(['live_items']);
    store.setConnectedLive([]);
    store.watchSession('rachel');
    const first = store.openLive('rachel');
    store.setConnectedLive(['rachel']);
    answers[0]({ status: 200, body: snapshotOf(1) });
    await first; await settle(); await settle();
    expect(liveGets()).toHaveLength(2);
    answers[1]({ status: 200, body: snapshotOf(4) });
    await settle(); await settle();
    expect(store.liveFor('rachel').lseq).toBe(4);
  });

  it('is not asked for again when the stream already carried the chat', async () => {
    const store = await freshStore();
    routes['/live?'] = () => ({ status: 200, body: snapshotOf(1) });
    store.setServerFeatures(['live_items']);
    store.setConnectedLive(['rachel']);
    store.watchSession('rachel');
    await store.openLive('rachel');
    store.setConnectedLive(['rachel']);
    await settle(); await settle();
    expect(liveGets()).toHaveLength(1);
  });
});

describe('a Host that lists live_items but sends nothing', () => {
  beforeEach(installGlobals);
  afterEach(() => vi.unstubAllGlobals());

  const noHub = { conv: 'conv-1', session: 'rachel', agent_id: 'agent-1', epoch: '', lseq: 0,
    server_now_ms: 1, activity: { state: 'idle' }, turn: null, items: [] };

  async function openedWithoutHub() {
    const store = await freshStore();
    routes['/live?'] = () => ({ status: 200, body: noHub });
    store.setServerFeatures(['live_items']);
    store.setConnectedLive(['rachel']);
    store.watchSession('rachel');
    await store.openLive('rachel');
    await settle();
    return store;
  }

  it('looks like the pre-live client: no live view, status or turn of its own', async () => {
    const store = await openedWithoutHub();
    expect(store.liveFor('rachel')).toBe(null);
    expect(store.rosterActivity('rachel')).toBe(null);
    expect(store.liveTurnRunning('rachel')).toBe(false);
  });

  it('does not ask for the snapshot again and again while nothing comes', async () => {
    const store = await openedWithoutHub();
    expect(store.liveRequested('rachel')).toBe(true);
    expect(liveGets()).toHaveLength(1);
  });

  it('takes the chat over once a live event for it arrives', async () => {
    const store = await openedWithoutHub();
    // The event's new epoch sends the client back to GET /live.
    routes['/live?'] = () => ({ status: 200,
      body: snapshotOf(1, { turn: { turn_id: 'tr-1', status: 'running', started_at_ms: 1 } }) });
    store.handleLiveEvent(event(1));
    runFrames();
    await settle(); await settle();
    runFrames();
    expect(store.liveFor('rachel')).not.toBe(null);
    expect(store.liveTurnRunning('rachel')).toBe(true);
  });

  it('takes the chat over from a snapshot that names a hub epoch', async () => {
    const store = await freshStore();
    routes['/live?'] = () => ({ status: 200, body: snapshotOf(0) });
    store.setServerFeatures(['live_items']);
    store.setConnectedLive(['rachel']);
    store.watchSession('rachel');
    await store.openLive('rachel');
    expect(store.liveFor('rachel')).not.toBe(null);
  });

  it('offers the explanation switch only when the Host has the setting', async () => {
    const store = await freshStore();
    store.setServerFeatures(['live_items']);
    expect(store.live.explanationSetting).toBe(false);
    store.setServerFeatures(['live_items', 'tool_explanation_setting']);
    expect(store.live.explanationSetting).toBe(true);
  });
});
