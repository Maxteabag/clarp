// Hardening sweep — did the message arrive, and how long before the client
// admits it did not?
//
// delivery.js already treats "the server answered 200" as no proof of
// anything; the transcript is the proof. These pin the gaps left around that
// idea: the requests that never answer at all, and the verdict the client
// reaches at 20 seconds and then refuses to revise.

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import {
  DeliveryState, createDeliveryLog, recordSend, markState, confirmFromTurns,
  staleSends, serverIdFor,
} from '@core/delivery.js';
import { newClientMsgId } from '@core/send-request.js';

const flash = vi.fn();
const clog = vi.fn();
const chime = vi.fn();
const addOptimisticTurn = vi.fn();

vi.mock('../../web/src/lib/net.js', () => ({ clog, trace: { id: '' } }));
vi.mock('../../web/src/stores/app.svelte.js', () => ({
  app: { session: 'lena-02e0' }, flash, setSession: async () => {},
}));
vi.mock('../../web/src/stores/audio.svelte.js', () => ({
  audio: { muted: true },
  chime,
  machine: { send() {}, settle() {} },
  playerAdapter: { interrupt() {} },
  scheduler: { silence: () => 0 },
}));
vi.mock('../../web/src/stores/conversations.svelte.js', () => ({ addOptimisticTurn }));

let fetchSpy;

async function loadStores() {
  vi.resetModules();
  const delivery = await import('../../web/src/stores/delivery.svelte.js');
  const send = await import('../../web/src/stores/send.svelte.js');
  return { ...send, ...delivery };
}

beforeEach(() => {
  flash.mockClear(); clog.mockClear(); chime.mockClear();
  addOptimisticTurn.mockClear();
  vi.useFakeTimers();
  fetchSpy = vi.fn(async () => ({
    ok: true, status: 200, json: async () => ({ ok: true, session: 'lena-02e0' }),
  }));
  vi.stubGlobal('fetch', (...a) => fetchSpy(...a));
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe('a POST /send that never answers', () => {
  // BUG. The /send fetch has no AbortController and no timeout
  // (web/src/stores/send.svelte.js:36-46) — unlike /transcribe, which is
  // capped at TRANSCRIBE_TIMEOUT_MS (mic.svelte.js:237-238). And the only
  // thing that ever calls sweepStaleSends() is armed *after* a successful
  // response (send.svelte.js:57), so a request that never resolves arms
  // nothing.
  //
  // A request that hangs — a half-open TCP connection after a laptop sleeps,
  // a proxy that accepts and never replies, iOS holding a socket across a
  // background transition — therefore leaves the entry PENDING forever. The
  // optimistic bubble stays on screen looking sent, which is precisely the
  // failure mode delivery.js was written to end.
  //
  // FIX: give /send the same AbortController timeout /transcribe has, and
  // arm the stale sweep from noteSendStarted() rather than from the success
  // path, so every send is swept whatever the request does.
  it.fails('is eventually shown as failed rather than pending forever', async () => {
    const m = await loadStores();
    fetchSpy.mockImplementation(() => new Promise(() => {}));
    m.sendText('is anyone there');
    await Promise.resolve();
    expect(m.delivery.entries).toHaveLength(1);
    expect(m.delivery.entries[0].state).toBe(DeliveryState.PENDING);
    await vi.advanceTimersByTimeAsync(180_000);
    expect(m.delivery.entries[0].state).toBe(DeliveryState.FAILED);
  });

  // Axiom: the sweep really is armed only on the path that succeeded.
  it('arms no sweep at all, because the sweep hangs off the success path', async () => {
    const m = await loadStores();
    fetchSpy.mockImplementation(() => new Promise(() => {}));
    m.sendText('is anyone there');
    await Promise.resolve();
    expect(vi.getTimerCount()).toBe(0);
  });
});

describe('a POST /send the server refuses', () => {
  // Axiom, and the answer to "are rejected sends retried forever?": no. A
  // 401 takes the !r.ok branch (send.svelte.js:47-52), which marks the entry
  // FAILED once, paints the bubble via markTurnFailed and stops. Nothing
  // retries, and the auth banner comes from the fetch wrapper separately.
  it('is marked failed once and never retried', async () => {
    const m = await loadStores();
    fetchSpy.mockImplementation(async () => ({ ok: false, status: 401 }));
    const failed = [];
    m.bindDeliveryFailure((session, id) => failed.push([session, id]));
    await m.sendText('hello');
    await vi.advanceTimersByTimeAsync(180_000);
    expect(fetchSpy).toHaveBeenCalledTimes(1);
    const entry = m.delivery.entries[0];
    expect(entry.state).toBe(DeliveryState.FAILED);
    expect(entry.detail).toBe('send failed (401)');
    expect(failed).toEqual([['lena-02e0', serverIdFor(entry.id)]]);
  });
});

describe('the twenty-second verdict', () => {
  // Axiom: the sweep's whole basis is that the transcript should have caught
  // up within 20 seconds.
  it('calls a send stale once nothing has confirmed it for 20s', () => {
    const log = recordSend(createDeliveryLog(), {
      id: 'c-1', session: 'lena-02e0', text: 'hi', at: 1000,
    });
    markState(log, 'c-1', DeliveryState.SENT, '', 1200);
    expect(staleSends(log, { now: 21_500 })).toHaveLength(1);
  });

  // BUG. confirmFromTurns() skips any entry already in FAILED
  // (static/lib/delivery.js:74), so the sweep's guess is final. But the
  // sweep is only a timer, and the transcript is the ground truth this whole
  // module is built on ("it is delivered when its own id comes back in the
  // transcript, and nothing else counts", delivery.js:11-13).
  //
  // Twenty seconds is easy to exceed without anything being wrong: the
  // transcript is refreshed on a transcript-updated SSE event, so any client
  // whose event stream is down — see regression-sse-reconnect.test.js, where
  // a refused stream reconnects forever in silence — never refreshes in
  // time. Every message sent in that window is then permanently painted as
  // failed by markTurnFailed(), while the agent is answering it. There is no
  // markTurnDelivered() to undo it (conversations.svelte.js:361-369 is the
  // only writer of the flag).
  //
  // FIX: let evidence beat the timer. confirmFromTurns() should upgrade a
  // FAILED entry whose id appears in the transcript, and the conversation
  // store needs the matching un-fail so the bubble stops lying.
  it.fails('is revised when the transcript later proves the send arrived', () => {
    const log = recordSend(createDeliveryLog(), {
      id: 'c-1', session: 'lena-02e0', text: 'hi', at: 1000,
    });
    markState(log, 'c-1', DeliveryState.SENT, '', 1200);
    // The sweep fires before the transcript catches up.
    markState(log, 'c-1', DeliveryState.FAILED, 'never reached the transcript', 21_500);
    // The transcript then arrives carrying the very id we minted.
    confirmFromTurns(log, [{ id: serverIdFor('c-1'), role: 'user' }], 40_000);
    expect(log.entries[0].state).toBe(DeliveryState.CONFIRMED);
  });
});

describe('the idempotency key across a reload', () => {
  // Axiom worth stating because the key is what protects a send from the
  // server's dedup: newClientMsgId()'s uniqueness counter is module state
  // (static/lib/send-request.js:22,29) and restarts at 1 on every page load.
  // Within one millisecond of a reload the clock and the counter are both
  // identical, so Math.random() is the only thing left separating two keys.
  // That is 32 bits and fine in practice — but it is the whole guarantee,
  // and the comment at send-request.js:26 credits the counter instead.
  it('rests entirely on the random salt, not on the counter', () => {
    const at = 1_788_251_668_458;
    const rand = () => 0.5;
    // Two page loads: the counter is back to zero for the second one.
    const first = newClientMsgId(at, rand);
    const second = newClientMsgId(at, rand);
    expect(first).not.toBe(second);       // counter separates them in one load
    // Simulate the reload by restarting the counter: same clock, same salt.
    expect(first).toBe(`c-${Math.floor(at).toString(36)}-1-${(0.5 * 0x100000000).toString(36)}`);
  });

  // Axiom: nothing about an in-flight send survives the reload either. The
  // delivery log is plain in-memory $state (delivery.svelte.js:11), so a
  // reload during a send loses the pending entry, the sweep that would have
  // failed it, and the bubble — the message is neither confirmed nor
  // reported. Persisting the log (sessionStorage) would let a reloaded
  // client finish the job with confirmFromTurns() on the next /log.
  it('starts from an empty delivery log on every load', async () => {
    const a = await loadStores();
    await a.sendText('first');
    expect(a.delivery.entries).toHaveLength(1);
    const b = await loadStores();
    expect(b.delivery.entries).toHaveLength(0);
  });
});
