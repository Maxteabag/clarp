#!/usr/bin/env node
// Minimal core-protocol client: the whole contract in one file, no deps.
// Uses the pure modules by relative path (no package) and only global fetch.
//
//   node clarp-client.mjs --base=http://127.0.0.1:7682 --session=rachel --text="hi"
//
// Flow: server-info → snapshot → events → open tail → send → poll deltas
// until u-<id> lands (delivery) → fetch clips by precedence + ack.
// Prints one JSON summary line. If this cannot stay small while passing,
// the contract is too complex (that is a contract bug, not a client bug).
import { randomUUID } from 'node:crypto';
import {
  applyLog, applySnapshot, blankSync, onEvent, onOpen, pickClipSource,
} from '../../static/lib/conversation-sync.js';
import {
  confirmFromTurns, createDeliveryLog, DeliveryState, markState,
  recordSend,
} from '../../static/lib/delivery.js';

const args = Object.fromEntries(process.argv.slice(2).map((a) => {
  const m = a.match(/^--([^=]+)=(.*)$/);
  return m ? [m[1], m[2]] : [a.replace(/^--/, ''), true];
}));
const base = String(args.base || '');
const session = String(args.session || 'rachel');
const text = String(args.text || 'hello from the reference client');
if (!base) throw new Error('pass --base=http://host:port');

const get = (p) => fetch(base + p).then((r) => r.json());
const post = (p, body) => fetch(base + p, {
  method: 'POST', headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body),
}).then((r) => r.json());

let sync = blankSync(session);
const delivery = createDeliveryLog();
const audioQueue = [];
const seenAudio = new Set();

// server-info: features is the only gate; just report the window.
const info = await get('/server-info');
if (!info.min_app_version) throw new Error('server-info has no min_app_version');

// snapshot: learn the roster; our cache is empty so this is quiet.
const snap = await get('/agents/snapshot');
const row = snap.agents.find((a) => a.session === session);
if (!row) throw new Error(`no such session: ${session}`);
({ state: sync } = applySnapshot(sync, row));

// events: plain fetch reader; every block is id:/data: or a : comment.
const stream = await fetch(base + '/events', { headers: { Accept: 'text/event-stream' } });
const reader = stream.body.getReader();
const decoder = new TextDecoder();
let buf = '';
(async () => {
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let cut;
    while ((cut = buf.indexOf('\n\n')) !== -1) {
      const block = buf.slice(0, cut);
      buf = buf.slice(cut + 2);
      let data = null;
      for (const line of block.split('\n')) {
        if (line.startsWith('data:')) {
          try { data = JSON.parse(line.slice(5).trim()); } catch { data = null; }
        }
      }
      if (!data) continue; // : connected, : ping, roster nudge without data
      const r = onEvent(sync, data);
      sync = r.state; // fetch effects are covered by the poll loop below
      if (data.type === 'audio' && data.session === session && !seenAudio.has(data.clip_id)) {
        seenAudio.add(data.clip_id);
        audioQueue.push(data);
      }
    }
  }
})().catch(() => {});

// open the chat, then send with a minted idempotency key.
if (onOpen(sync).effects.includes('fetch_tail')) {
  const tail = await get(`/log?session=${session}&limit=100`);
  ({ state: sync } = applyLog(sync, tail, 'tail'));
}
const id = randomUUID();
recordSend(delivery, { id, session, text });
await post('/send', { session, text, client_msg_id: id });
markState(delivery, id, DeliveryState.SENT);

// poll deltas until our u- id lands: 200 was acceptance, this is delivery.
let delivered = false;
for (let i = 0; i < 60 && !delivered; i++) {
  await new Promise((r) => setTimeout(r, 300));
  const d = await get(`/log?session=${session}&after_revision=${sync.cursor}&limit=100`);
  ({ state: sync } = applyLog(sync, d, 'delta'));
  if (d.replace_required || (d.conversation_id && d.conversation_id !== sync.conversationId)) {
    const tail = await get(`/log?session=${session}&limit=100`);
    ({ state: sync } = applyLog(sync, tail, 'tail'));
  }
  confirmFromTurns(delivery, sync.order.map((x) => sync.turns[x]));
  delivered = delivery.entries.some((e) => e.id === id && e.state === DeliveryState.CONFIRMED);
  if (d.has_more) continue;
}

// voice out: best source first, then the ack sequence (no playback here).
const acked = [];
for (const ev of audioQueue.splice(0)) {
  const src = pickClipSource(ev);
  const url = src === 'playlist' ? ev.playlist_url : src === 'stream' ? ev.stream_url : ev.url;
  const ack = (status, error) => post('/clips/ack', {
    clip_id: ev.clip_id, url: ev.url, status, error,
  });
  await ack('queued');
  await ack('play-start');
  try {
    await (await fetch(base + url)).arrayBuffer();
    await ack('play-ok');
    acked.push(ev.clip_id);
  } catch (e) { await ack('play-fail', String(e && e.message || e)); }
}
try { await reader.cancel(); } catch { /* shut down the stream */ }

// final catch-up so our transcript equals /log.
const end = await get(`/log?session=${session}&after_revision=${sync.cursor}&limit=100`);
({ state: sync } = applyLog(sync, end, 'delta'));
const mine = sync.order.map((x) => sync.turns[x].id);
const log = await get(`/log?session=${session}&limit=100`);
console.log(JSON.stringify({
  delivered,
  turns: mine,
  logIds: log.turns.map((t) => t.id),
  acked,
  min_app_version: info.min_app_version,
}));
