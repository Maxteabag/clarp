// Regression: a second message to the same agent was silently dropped.
//
// Observed 2026-09-01 against session freya-3694: the server logged
//   sendDeduplicated agent=c2b6d6c85d36a5ed client_msg_id=5799ff1768aa54d4
// and answered "POST /send HTTP/1.1" 200 with nothing written — no
// prompt_admission, no queued_turn, no message row. The client had already
// painted the optimistic bubble, so the message looked sent and was gone.
//
// Cause: the server dedups on `client_msg_id or trace_id`, and the client sent
// no client_msg_id while carrying the previous turn's trace id forward (the
// trace id is deliberately long-lived — every client event is stamped with it
// until the server issues a new one). Two turns therefore shared one
// idempotency key and the second was discarded as a retry.

import { describe, it, expect } from 'vitest';
import { buildSendBody, newClientMsgId } from '@core/send-request.js';

// Mirrors lib/turn_dispatch.py: `request_id = spec.client_msg_id or
// spec.trace_id`, and a request_id already seen is dropped with a 200.
function makeServer() {
  const seen = new Set();
  let issued = 0;
  return {
    admitted: [],
    post(body) {
      const requestId = body.client_msg_id || body.trace_id;
      if (requestId && seen.has(requestId)) {
        return { ok: true, deduplicated: true, trace_id: body.trace_id };
      }
      if (requestId) seen.add(requestId);
      this.admitted.push(body.text);
      // The server echoes the caller's trace id, or mints one when absent.
      const traceId = body.trace_id || `srv-trace-${++issued}`;
      return { ok: true, session: body.session, trace_id: traceId };
    },
  };
}

// The client half of the round trip, as sendText() does it: build the body,
// then adopt the trace id the server hands back for subsequent events.
function makeClient(server, initialTraceId = '') {
  let traceId = initialTraceId;
  return {
    send(text) {
      const body = buildSendBody({ text, session: 'freya-3694', traceId });
      const reply = server.post(body);
      if (reply && reply.trace_id) traceId = reply.trace_id;
      return { body, reply };
    },
    get traceId() { return traceId; },
  };
}

describe('newClientMsgId', () => {
  it('is unique across calls within the same millisecond', () => {
    const now = 1788251668458;
    const ids = new Set();
    for (let i = 0; i < 1000; i++) ids.add(newClientMsgId(now));
    expect(ids.size).toBe(1000);
  });

  it('is unique when the random source is degenerate', () => {
    // Some hardened webviews return a constant from a weak Math.random shim,
    // and Clarp is reached over plain http on a NetBird IP, where
    // crypto.randomUUID is unavailable. The counter has to carry uniqueness
    // on its own.
    const ids = new Set();
    for (let i = 0; i < 100; i++) ids.add(newClientMsgId(0, () => 0));
    expect(ids.size).toBe(100);
  });
});

describe('POST /send idempotency key', () => {
  it('sends a distinct client_msg_id per message', () => {
    const a = buildSendBody({ text: 'first', session: 's', traceId: 'trace-1' });
    const b = buildSendBody({ text: 'second', session: 's', traceId: 'trace-1' });
    expect(a.client_msg_id).toBeTruthy();
    expect(b.client_msg_id).toBeTruthy();
    expect(a.client_msg_id).not.toBe(b.client_msg_id);
  });

  it('sends no trace id at all, so the server mints one per turn', () => {
    const body = buildSendBody({ text: 'hi', session: 's' });
    expect(body.trace_id).toBeUndefined();
    expect(body.client_msg_id).toBeTruthy();
  });

  it('admits a follow-up message from a tab holding an old trace id', () => {
    const server = makeServer();
    // A tab mid-conversation already holds a trace id from an earlier turn.
    const client = makeClient(server, 'trace-from-earlier-turn');

    client.send('yeh but i think the point here is that the API is non-user specific');
    const second = client.send('so if we are to do this... i think it would make sense to see if we can use the same API both for the API KEY for all access, and a user-based authentication system');

    expect(second.reply.deduplicated).toBeFalsy();
    expect(server.admitted).toHaveLength(2);
  });

  it('gives consecutive messages distinct traces, so they cannot preempt each other', () => {
    // Regression, 2026-09-01 15:08:19 on domi-5a0e: three messages typed in one
    // second all carried the trace id of an earlier turn, and in-flight
    // bookkeeping is keyed on the trace. Each one killed the last —
    //   turnPreempted agent=be7ab0698feffb34 killed=f2913a09b0102829 new=f2913a09b0102829
    // — and only the third ever ran. The same id had also been used minutes
    // earlier for a different session entirely.
    const server = makeServer();
    const client = makeClient(server, 'trace-from-earlier-turn');

    const traces = ['Theres a bottom bar in the Chats.', 'HELLO?', 'the bottom bar again']
      .map(text => client.send(text).reply.trace_id);

    expect(new Set(traces).size).toBe(3);
    expect(server.admitted).toHaveLength(3);
  });

  it('still drops a genuine retry of the same send', () => {
    const server = makeServer();
    const body = buildSendBody({ text: 'once', session: 's', traceId: '' });

    const first = server.post(body);
    const retry = server.post(body);

    expect(first.deduplicated).toBeFalsy();
    expect(retry.deduplicated).toBe(true);
    expect(server.admitted).toEqual(['once']);
  });
});
