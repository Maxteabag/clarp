// Outbound text: /send, /stop, and the trace id every subsequent client event
// is stamped with.

import { buildSendBody, newClientMsgId } from '@core/send-request.js';
import { serverIdFor } from '@core/delivery.js';
import { clog, trace } from '../lib/net.js';
import { app, flash, setSession } from './app.svelte.js';
import {
  audio, chime, machine, playerAdapter, scheduler,
} from './audio.svelte.js';
import { addOptimisticTurn } from './conversations.svelte.js';
import { noteSendResult, noteSendStarted, sweepStaleSends } from './delivery.svelte.js';

export const send = $state({
  /** Which agent a capture was started for; a switch mid-capture must not
   *  redirect the utterance to whoever is focused when it finishes. */
  captureTarget: '',
  pendingHandsFree: false,
});

export async function sendText(text, opts = {}) {
  text = (text || '').trim();
  if (!text) return;
  const target = send.captureTarget || app.session;
  send.captureTarget = '';

  // One id for the whole life of this message: the request's idempotency key,
  // the bubble's id, and the id the server files the durable row under. That
  // is what lets the client tell delivered from merely accepted.
  const clientMsgId = newClientMsgId();
  addOptimisticTurn(target, serverIdFor(clientMsgId), text);
  noteSendStarted(clientMsgId, target, text);
  let routed = target;
  let dispatched = true;
  try {
    const r = await fetch('/send', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(buildSendBody({
        text,
        session: target,
        clientMsgId,
        synthesizeAudio: !audio.muted,
        handsFree: !!opts.handsFree,
      })),
    });
    if (!r.ok) {
      chime(false);
      noteSendResult(clientMsgId, false, `send failed (${r.status})`);
      flash(`send failed (${r.status})`, 4000);
      return;
    }
    chime(true);
    // Accepted, not delivered. Only the transcript coming back with this id
    // proves it ran; the sweep marks it failed if it never does.
    noteSendResult(clientMsgId, true);
    setTimeout(() => sweepStaleSends(), 20000);
    try {
      const d = await r.json();
      // Hands-free routing may have picked a different agent; follow it.
      if (d && d.session && d.session !== app.session) {
        await setSession(d.session);
      }
      if (d && d.session) routed = d.session;
      if (d && d.orchestrator) {
        const action = d.orchestrator.action || '';
        dispatched = action === 'route' || action === 'fallback';
      }
      if (d && d.trace_id) {
        trace.id = d.trace_id;
        clog('traceStart', trace.id);
      }
    } catch (_) {}
  } catch (err) {
    chime(false);
    noteSendResult(clientMsgId, false, `send failed: ${err.message}`);
    flash(`send failed: ${err.message}`, 4000);
    return;
  }
  if (!dispatched) {
    try { machine.settle(); } catch (_) {}
    return;
  }
  try { machine.send(routed); } catch (_) {}
  // A new turn is committed, so the user has moved on from whatever was
  // playing. Drop the queue and release busy, or the reply queues behind a
  // two-minute previous answer.
  try {
    playerAdapter?.interrupt();
    const dropped = scheduler?.silence();
    if (dropped) clog('autoSilenceOnSend', `dropped=${dropped}`);
  } catch (_) {}
}

export function stopAgentTurn() {
  // No busy-guard: the snapshot can be stale or focused elsewhere, and an
  // Escape into an idle pane is a harmless no-op. Always send.
  clog('stopPressed', app.session);
  fetch('/stop', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session: app.session }),
  }).then(r => {
    if (!r.ok) flash('Stop failed: ' + r.status, 2000);
  }).catch(e => flash('Stop network error: ' + (e && e.message), 2000));
  flash('Stopped', 1000);
}
