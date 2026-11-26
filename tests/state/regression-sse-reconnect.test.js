// Hardening sweep — the SSE connection: what a permanently failing stream
// tells the user, and who acts on a broadcast.
//
// Drives the real store (web/src/stores/sse.svelte.js) and the real
// connection state (web/src/stores/app.svelte.js) against a fake EventSource,
// so the reconnect loop, its timers and the overlay all run as shipped.

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { Timing } from '@core/protocol.js';

const clog = vi.fn();
const noteSseEvent = vi.fn();
const onRecordToggle = vi.fn();
const stopAgent = vi.fn();

vi.mock('../../web/src/lib/net.js', () => ({
  clog,
  noteSseEvent,
  withToken: u => u,
  trace: { id: '' },
}));
vi.mock('../../web/src/stores/conversations.svelte.js', () => ({
  appendActivity: () => {}, appendThinking: () => {}, handleSseEvent: () => {},
  refreshAll: () => {}, removeLiveThinking: () => {}, wake: () => {},
  ensureLoaded: async () => {}, reconcileWithSnapshot: () => {},
}));
vi.mock('../../web/src/stores/audio.svelte.js', () => ({
  audio: { muted: false },
  bumpLastAudioTs: () => {},
  lastAudioTs: 0,
  PLAYER_ADAPTER_VERSION: 'test',
  scheduler: { ingest: () => ({ accepted: true }) },
  unlockAudio: () => {},
  addConditionSource: () => {},
}));

const streams = [];

class FakeEventSource {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSED = 2;
  constructor(url) {
    this.url = url;
    this.readyState = 0;
    this.closed = false;
    this.onopen = null;
    this.onmessage = null;
    this.onerror = null;
    streams.push(this);
    // The server refuses the stream: the browser reports an error and never
    // an open. This is exactly what a 401 on GET /events looks like to JS —
    // no status, no body, just onerror.
    setTimeout(() => { if (!this.closed) { this.readyState = 2; this.onerror && this.onerror(); } }, 0);
  }
  close() { this.closed = true; this.readyState = 2; }
}

function stubBrowser() {
  vi.stubGlobal('EventSource', FakeEventSource);
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, status: 200, json: async () => ({}) })));
  vi.stubGlobal('localStorage', {
    getItem: () => null, setItem: () => {}, removeItem: () => {},
  });
  vi.stubGlobal('document', {
    documentElement: { classList: { contains: () => false } },
  });
}

async function newClient() {
  vi.resetModules();
  const sse = await import('../../web/src/stores/sse.svelte.js');
  const appMod = await import('../../web/src/stores/app.svelte.js');
  sse.setSseHooks({ onRecordToggle, stopAgent, closeOverview: () => {} });
  return { sse, app: appMod.app };
}

beforeEach(() => {
  streams.length = 0;
  clog.mockClear();
  noteSseEvent.mockClear();
  onRecordToggle.mockClear();
  stopAgent.mockClear();
  stubBrowser();
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe('a stream the server will never open', () => {
  // BUG. scheduleReconnect() calls setConn('dead', DEAD_OVERLAY_MS)
  // (sse.svelte.js:38), which arms a DEAD_OVERLAY_MS timer in setConn
  // (app.svelte.js:62-68). It then reconnects after `reconnectMs`, and
  // connectSSE()'s setConn('connecting') (sse.svelte.js:46) takes the *else*
  // branch and clears that timer (app.svelte.js:70-71).
  //
  // reconnectMs is capped at SSE_RECONNECT_MAX_MS = 5000 and the overlay
  // needs DEAD_OVERLAY_MS = 15000 (@core/protocol.js:103,107). The countdown
  // is therefore restarted every 5 seconds at the latest and can never
  // finish: `app.showReconnect` stays false forever and the "Tap to
  // reconnect" button in App.svelte:213-219 is unreachable code for the one
  // failure it exists for. Only a single connection that stays dead longer
  // than the backoff would show it, and the backoff is never that long.
  //
  // FIX: don't let a mere reconnect *attempt* clear the overlay. Clear it on
  // 'live' only, and keep a "last successfully open at" timestamp so the
  // overlay is a function of time since the last open, not of the current
  // attempt.
  it.fails('eventually offers the user the reconnect affordance', async () => {
    const c = await newClient();
    c.sse.connectSSE();
    await vi.advanceTimersByTimeAsync(120_000);
    expect(c.app.showReconnect).toBe(true);
  });

  // BUG. Nothing in the connection lifecycle logs. connectSSE(),
  // scheduleReconnect() and es.onerror (sse.svelte.js:37-77) contain no
  // clog() call at all — the first clog in the file is at :98, inside the
  // audio branch of handleEvent(), which a stream that never opens can never
  // reach. A client whose event stream is permanently refused therefore
  // produces zero client-log rows about it, and POST /clog still succeeds
  // (it goes over fetch, which is authenticated by header), so the server
  // sees a healthy chatty client with a stream it can never open.
  //
  // This is issue #13's "a failure branch that logged nothing", one layer
  // out: the whole failure *loop* logs nothing.
  //
  // FIX: clog once per transition into the failing state, and again with an
  // attempt count when the backoff saturates — e.g.
  // clog('sseReconnect', `attempt=${n} delay=${reconnectMs}`).
  it.fails('says something in the client log', async () => {
    const c = await newClient();
    c.sse.connectSSE();
    await vi.advanceTimersByTimeAsync(120_000);
    expect(clog).toHaveBeenCalled();
  });

  // Axiom: the reconnect loop really does keep running, so this is a live
  // client burning attempts rather than one that gave up.
  it('keeps opening new EventSources forever', async () => {
    const c = await newClient();
    c.sse.connectSSE();
    await vi.advanceTimersByTimeAsync(120_000);
    expect(streams.length).toBeGreaterThan(20);
    expect(c.sse.sseIsOpen()).toBe(false);
  });

  // Axiom, and the reason issue #10's health record cannot help here:
  // noteSseEvent() is called from es.onmessage only (sse.svelte.js:66).
  // Neither the open nor the error path touches health, so from the health
  // record's point of view a permanently refused stream is indistinguishable
  // from a quiet one — and if fetches keep succeeding, assess() says OK.
  it('teaches the health record nothing', async () => {
    const c = await newClient();
    c.sse.connectSSE();
    await vi.advanceTimersByTimeAsync(120_000);
    expect(noteSseEvent).not.toHaveBeenCalled();
  });
});

describe('one remote action, however many clients are open', () => {
  async function twoOpenClients() {
    const a = await newClient();
    a.sse.connectSSE();
    const esA = streams[streams.length - 1];
    const b = await newClient();
    b.sse.connectSSE();
    const esB = streams[streams.length - 1];
    return { esA, esB };
  }

  const deliver = (es, ev) => es.onmessage({ data: JSON.stringify(ev) });

  // BUG. POST /remote-action broadcasts to every subscriber
  // (server/server.py:2575-2586 → ctx.stream.broadcast) and the client
  // handler acts unconditionally (sse.svelte.js:127-136). One press of the
  // iOS Action Button therefore calls toggleRecord() once per open client.
  //
  // That is not a harmless duplicate: toggleRecord() is a *toggle*. With the
  // phone and the desktop both open, one press starts recording on one and
  // stops it on the other, and the second press swaps them — the button
  // stops having a defined meaning. It also multiplies /transcribe uploads
  // and, for stop-agent, sends N stop requests.
  //
  // net.js already mints an instanceId() per module evaluation
  // (web/src/lib/net.js:19-22) and nothing uses it for this.
  //
  // FIX: the server should address one client. Have the client register its
  // instance id (e.g. on the /events query or a POST after open), have the
  // server pick the most recently active one, and stamp the event with it.
  it.fails('is acted on exactly once', async () => {
    const { esA, esB } = await twoOpenClients();
    const ev = { type: 'remote-action', action: 'record-toggle', ts: 1 };
    deliver(esA, ev);
    deliver(esB, ev);
    expect(onRecordToggle).toHaveBeenCalledTimes(1);
  });

  // BUG, same cause, stated as the fix's contract: once the server can name
  // an instance, a client must ignore an event addressed to a different one.
  // Today the handler reads only `ev.action` and acts regardless.
  //
  // FIX: `if (ev.instance && ev.instance !== myInstanceId) return;` at the
  // top of the REMOTE_ACTION branch, with the id coming from net.js.
  it.fails('ignores an action addressed to another instance', async () => {
    const { esA } = await twoOpenClients();
    deliver(esA, {
      type: 'remote-action', action: 'record-toggle',
      instance: 'net-someone-else-9', ts: 1,
    });
    expect(onRecordToggle).not.toHaveBeenCalled();
  });

  // Same for stop-agent, which fans out into N POST /stop.
  it.fails('stops the agent once, not once per open tab', async () => {
    const { esA, esB } = await twoOpenClients();
    const ev = { type: 'remote-action', action: 'stop-agent', ts: 1 };
    deliver(esA, ev);
    deliver(esB, ev);
    expect(stopAgent).toHaveBeenCalledTimes(1);
  });
});

describe('reconnect constants', () => {
  // Axiom that makes the overlay bug arithmetic rather than timing-dependent.
  it('back off faster than the overlay it keeps cancelling', () => {
    expect(Timing.SSE_RECONNECT_MAX_MS).toBeLessThan(Timing.DEAD_OVERLAY_MS);
  });
});
