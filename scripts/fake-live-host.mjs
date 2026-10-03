#!/usr/bin/env node
// A fake Host that speaks the live item contract (docs/live-items.md) for
// looking at a client without a real Host, agent or database.
//
//   node scripts/fake-live-host.mjs [--port 7799] [--build <vite outDir>] [--speed 1]
//
// It serves the PWA (index.html and the bundle from --build, everything else
// from static/), one agent `rachel`, and two turns on a loop-free timeline:
//
//   A  contract/live/turn-full.json replayed at its recorded pace (thinking,
//      commentary, an explore group, a long failing command, an edit, an
//      interrupt), retimed to start now
//   B  a completed turn: thinking, a command with a growing output tail, an
//      edit with a diff, then a markdown answer streamed in small chunks,
//      after which its durable /log row lands under the same id
//
// GET /live is answered from the same reducer the client runs
// (static/lib/live-items.js), so a snapshot always matches the events.
// `?at=<ms>` on the page URL is not needed: GET /fake/clock tells how far the
// timeline is, for screenshot scripts.

import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { applyLiveEvent, applyLiveSnapshot, blankLive, liveItems } from '../static/lib/live-items.js';

const repo = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const arg = (name, fallback) => {
  const i = process.argv.indexOf(`--${name}`);
  return i > 0 ? process.argv[i + 1] : fallback;
};
const PORT = Number(arg('port', 7799));
const BUILD = path.resolve(arg('build', path.join(repo, 'static')));
const SPEED = Number(arg('speed', 1));
const EXPLAIN_START = arg('explanations', 'on') !== 'off';
// --no-live: a Host from before live items. The same turns arrive as today:
// a growing `live` /log row with transcript-updated pings, and agent-state /
// agent-activity events for the status line.
const NO_LIVE = process.argv.includes('--no-live');

const T0 = 1759480000000;
const fixture = JSON.parse(fs.readFileSync(path.join(repo, 'contract/live/turn-full.json'), 'utf8'));
const start = Date.now() + 1500;   // a moment to load the page first
const shift = start - T0;

function retime(value) {
  if (Array.isArray(value)) return value.map(retime);
  if (value && typeof value === 'object') {
    const out = {};
    for (const [k, v] of Object.entries(value)) {
      out[k] = k.endsWith('_ms') && typeof v === 'number' && k !== 'worked_ms' ? v + shift : retime(v);
    }
    return out;
  }
  return value;
}

// ---- timeline ---------------------------------------------------------------

const timeline = [];   // {at: wall ms, event?: live event, log?: turn, sse?: plain event}
let lseq = 0;
const snapshot0 = retime(fixture.steps[0].snapshot);
for (const step of fixture.steps.slice(1)) {
  const ev = retime(step.event);
  timeline.push({ at: start + (step.event.server_now_ms - T0) / SPEED, event: ev });
  lseq = Math.max(lseq, step.event.lseq);
}

// Turn B, built here: 4 s after turn A settles.
const bStart = start + 16000 / SPEED;
let bClock = bStart;
let bRev = {};
const conv = 'conv-1';
// `ops` is a function so the times inside it are read after the clock moves.
const pushB = (dt, makeOps) => {
  bClock += dt / SPEED;
  const ops = makeOps();
  lseq += 1;
  timeline.push({ at: bClock, event: { type: 'live', agent_id: 'agent-1', session: 'rachel', conv,
    epoch: 'boot-a', lseq, server_now_ms: Math.round(bClock), ops } });
};
const rev = id => (bRev[id] = (bRev[id] || 0) + 1);
const now = () => Math.round(bClock);
const status = (state, extra = {}) => ({ op: 'status', conv, activity: {
  state, tool: null, running_tools: 0, headline: null, item_id: null, since_ms: now(),
  turn_id: 'tr-2', turn_started_ms: Math.round(bStart), ...extra } });
const upsert = (id, kind, item) => ({ op: 'upsert', conv, id, kind, rev: rev(id), item });
const done = (id, kind, st, startedAt, item) => ({ op: 'done', conv, id, kind, rev: rev(id), status: st,
  started_at_ms: startedAt, ended_at_ms: now(), ...(item ? { item } : {}) });
const append = (id, kind, extra) => ({ op: 'append', conv, id, kind, rev: rev(id), ...extra });

timeline.push({ at: bStart - 50, log: { id: 'u-b', role: 'user', text: 'Now fix it and tell me what changed.',
  timestamp: new Date(bStart - 50).toISOString(), revision: 3 } });
pushB(0, () => [{ op: 'turn', conv, turn: { turn_id: 'tr-2', status: 'running', started_at_ms: now(),
  ended_at_ms: null, worked_ms: null, tool_count: 0 } }, status('thinking', { headline: 'Thinking' })]);
const r0 = now() + 200;
pushB(200, () => [upsert('cl:msg_b1:0', 'reasoning', { id: 'cl:msg_b1:0', conv, turn_id: 'tr-2', kind: 'reasoning',
  status: 'running', ordinal: 11, started_at_ms: now(), ended_at_ms: null, text: '', title: null })]);
pushB(300, () => [append('cl:msg_b1:0', 'reasoning', { field: 'text',
  chunk: '**Bounding the tokenizer loop**\n\nThe loop reads one past the end when the input ends in a newline.' }),
  upsert('cl:msg_b1:0', 'reasoning', { title: 'Bounding the tokenizer loop' }),
  status('thinking', { headline: 'Thinking: Bounding the tokenizer loop', item_id: 'cl:msg_b1:0' })]);
pushB(2500, () => [done('cl:msg_b1:0', 'reasoning', 'completed', r0)]);
const e0 = now() + 100;
pushB(100, () => [upsert('cl:toolu_b2', 'tool', { id: 'cl:toolu_b2', conv, turn_id: 'tr-2', kind: 'tool', status: 'running',
  ordinal: 12, started_at_ms: now(), ended_at_ms: null, tool: { name: 'Edit', call_id: 'toolu_b2', category: 'edit',
    group: null, label: 'src/tokenizer.ts', command: null, input_preview: { file_path: 'src/tokenizer.ts' },
    explain: null, output: null, diff: null } }),
  status('tool', { tool: { name: 'Edit', call_id: 'toolu_b2', label: 'src/tokenizer.ts', item_id: 'cl:toolu_b2',
    started_at_ms: now() }, running_tools: 1, headline: 'Editing src/tokenizer.ts', item_id: 'cl:toolu_b2' })]);
pushB(150, () => [done('cl:toolu_b2', 'tool', 'completed', e0, { tool: { diff: { added: 2, removed: 1,
  files: [{ path: 'src/tokenizer.ts', added: 2, removed: 1 }],
  preview: '@@ -40,3 +40,4 @@\n   while (i < src.length) {\n-    if (src[i + 1] === "\\n") break;\n+    const next = src[i + 1];\n+    if (next === undefined || next === "\\n") break;\n     i++;' } } }),
  status('thinking', { headline: 'Thinking' })]);
const x0 = now() + 200;
pushB(200, () => [upsert('cl:toolu_b3', 'tool', { id: 'cl:toolu_b3', conv, turn_id: 'tr-2', kind: 'tool', status: 'running',
  ordinal: 13, started_at_ms: now(), ended_at_ms: null, tool: { name: 'Bash', call_id: 'toolu_b3', category: 'exec',
    group: null, label: 'npm test', command: 'npm test -- --run src/parser.test.ts',
    input_preview: { command: 'npm test -- --run src/parser.test.ts' },
    explain: EXPLAIN_START ? { text: 'Pending', level: 2, status: 'pending' } : null,
    output: { tail: [], total_lines: 0, truncated: false, exit_code: null }, diff: null } }),
  status('tool', { tool: { name: 'Bash', call_id: 'toolu_b3', label: 'npm test', item_id: 'cl:toolu_b3',
    started_at_ms: now() }, running_tools: 1, headline: 'Running npm test', item_id: 'cl:toolu_b3' })]);
if (EXPLAIN_START) pushB(250, () => [upsert('cl:toolu_b3', 'tool', { tool: { explain: { text: 'Runs only the parser tests once, without watch mode', level: 2, status: 'ready' } } })]);
let total = 0;
for (let i = 0; i < 12; i++) {
  const lines = Array.from({ length: 6 }, (_, k) => `  ✓ parser › case ${i * 6 + k + 1} (${(k * 3) % 7} ms)`);
  total += lines.length;
  pushB(250, () => [append('cl:toolu_b3', 'tool', { field: 'tool.output', lines, total_lines: total })]);
}
total += 2;
pushB(250, () => [append('cl:toolu_b3', 'tool', { field: 'tool.output', lines: ['', 'Tests  72 passed (72)'], total_lines: total })]);
pushB(100, () => [done('cl:toolu_b3', 'tool', 'completed', x0, { tool: { output: { exit_code: 0 } } }),
  status('thinking', { headline: 'Thinking' })]);
const answer = [
  'Fixed. The tokenizer read one character past the end of the input when the last line ended in a newline, ',
  'so the parser saw a phantom empty token on CI (where files end in `\\n`).\n\n',
  '**What changed**\n\n',
  '- `src/tokenizer.ts`: the lookahead now stops at the end of input\n',
  '- no other files were touched\n\n',
  '```ts\nconst next = src[i + 1];\nif (next === undefined || next === "\\n") break;\n```\n\n',
  'All 72 parser tests pass. I did not run the full suite.',
].join('');
const m0 = now() + 300;
pushB(300, () => [upsert('cl:msg_b4:0', 'message', { id: 'cl:msg_b4:0', conv, turn_id: 'tr-2', kind: 'message',
  status: 'running', ordinal: 14, started_at_ms: now(), ended_at_ms: null, text: '', phase: 'final', row_id: 'live-b' }),
  status('responding', { headline: 'Responding', item_id: 'cl:msg_b4:0' })]);
for (let i = 0; i < answer.length; i += 18) {
  pushB(100, () => [append('cl:msg_b4:0', 'message', { field: 'text', chunk: answer.slice(i, i + 18) })]);
}
pushB(200, () => [done('cl:msg_b4:0', 'message', 'completed', m0),
  { op: 'turn', conv, turn: { turn_id: 'tr-2', status: 'completed', started_at_ms: Math.round(bStart),
    ended_at_ms: now(), worked_ms: now() - Math.round(bStart), tool_count: 2 } },
  { ...status('idle', { headline: null }), activity: { state: 'idle', tool: null, running_tools: 0, headline: null,
    item_id: null, since_ms: now(), turn_id: 'tr-2', turn_started_ms: null } }]);
timeline.push({ at: bClock + 400, log: { id: 'live-b', role: 'assistant', text: answer,
  timestamp: new Date(bClock).toISOString(), revision: 4 } });
timeline.sort((a, b) => a.at - b.at);

// ---- state the Host would hold -------------------------------------------------

let EXPLAIN = EXPLAIN_START;
let liveState = applyLiveSnapshot(blankLive(), snapshot0, start);
const logTurns = [
  { id: 'u-a', role: 'user', text: 'The parser test fails on CI only. Can you find out why?',
    timestamp: new Date(start - 30000).toISOString(), revision: 1 },
  { id: 'live-abc', role: 'assistant', kind: 'live', text: 'Let me look at the parser and its tests.',
    timestamp: new Date(start + 4000).toISOString(), revision: 2 },
];
let logRevision = 2;
const clients = new Set();
const requests = [];
let sseId = 100;

function broadcast(payload, withId = true) {
  const frame = (withId ? `id: ${++sseId}\n` : '') + `data: ${JSON.stringify(payload)}\n\n`;
  for (const res of clients) res.write(frame);
}

const LEGACY_KIND = { thinking: 'thinking', responding: 'thinking', tool: 'tool', compacting: 'compacting',
  idle: 'done', interrupted: 'interrupted' };
function legacyBroadcast(ev) {
  for (const op of ev.ops) {
    if (op.op === 'status') {
      const a = op.activity || {};
      const kind = LEGACY_KIND[a.state] || 'done';
      broadcast({ type: 'agent-state', session: 'rachel', agent_id: 'agent-1', persona: 'Rachel', kind,
        ts: Math.floor(ev.server_now_ms / 1000), detail: a.tool ? { tool: a.tool.name } : {} });
      broadcast({ type: 'agent-activity', session: 'rachel', agent_id: 'agent-1', persona: 'Rachel', kind,
        phase: kind, status: ['thinking', 'tool', 'compacting'].includes(kind) ? 'running' : 'ok',
        tool: a.tool ? a.tool.name : '', action: a.tool ? (a.headline || '').split(' ')[0] : '',
        summary: a.tool ? a.tool.label : '', ts: Math.floor(ev.server_now_ms / 1000), state: a.state,
        call_id: a.tool ? a.tool.call_id : '', started_at_ms: a.tool ? a.tool.started_at_ms : 0,
        turn_started_ms: a.turn_started_ms || 0 });
    }
    const item = op.id && liveState.items[op.id];
    if (item && item.kind === 'message' && item.row_id) {
      logRevision += 1;
      const turn = { id: item.row_id, role: 'assistant', kind: item.status === 'running' ? 'live' : null,
        text: item.text || '', timestamp: new Date(item.started_at_ms).toISOString(), revision: logRevision };
      const at = logTurns.findIndex(x => x.id === turn.id);
      if (at >= 0) logTurns[at] = turn; else logTurns.push(turn);
      broadcast({ type: 'transcript-updated', session: 'rachel', agent_id: 'agent-1' });
    }
  }
}

let cursor = 0;
setInterval(() => {
  const t = Date.now();
  while (cursor < timeline.length && timeline[cursor].at <= t) {
    const step = timeline[cursor++];
    if (step.event) {
      const r = applyLiveEvent(liveState, step.event, step.event.server_now_ms);
      liveState = r.state;
      if (NO_LIVE) legacyBroadcast(step.event);
      else broadcast(step.event, false);
    } else if (step.log) {
      logRevision += 1;
      const turn = { ...step.log, revision: logRevision };
      const at = logTurns.findIndex(x => x.id === turn.id);
      if (at >= 0) logTurns[at] = turn; else logTurns.push(turn);
      broadcast({ type: 'transcript-updated', session: 'rachel', agent_id: 'agent-1' });
    }
  }
}, 20);

// ---- HTTP -----------------------------------------------------------------------

const MIME = { '.js': 'text/javascript', '.css': 'text/css', '.html': 'text/html', '.json': 'application/json',
  '.png': 'image/png', '.svg': 'image/svg+xml', '.woff2': 'font/woff2', '.ttf': 'font/ttf' };

function sendJson(res, body, status = 200) {
  res.writeHead(status, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' });
  res.end(JSON.stringify(body));
}

function sendFile(res, file) {
  fs.readFile(file, (err, data) => {
    if (err) { res.writeHead(404); res.end(); return; }
    res.writeHead(200, { 'Content-Type': MIME[path.extname(file)] || 'application/octet-stream' });
    res.end(data);
  });
}

function agentRow() {
  const a = liveState.activity || { state: 'idle' };
  const busy = ['thinking', 'responding', 'tool', 'compacting'].includes(a.state);
  return { session: 'rachel', name: 'Rachel', persona: 'Rachel', agent_id: 'agent-1', alive: true,
    backend: 'claude', latest_state: busy ? (a.state === 'tool' ? 'tool' : 'thinking') : 'done', busy,
    conversation_id: conv, head_revision: logRevision, live_activity: a, cwd: '/home/demo/parser' };
}

// A second, idle agent with a short history, for agent switching.
// 150 turns, paged 100 at a time, with Codex-style display cells on some.
const mikeTurns = Array.from({ length: 150 }, (_, i) => ({ id: `mike-${i}`, role: i % 2 ? 'assistant' : 'user',
  text: i % 2 ? `Answer ${i}: the **deploy** finished.\n\n\`\`\`sh\nmake deploy-${i}\n\`\`\`` : `Question ${i}?`,
  timestamp: new Date(start - (200 - i) * 60000).toISOString(), revision: i + 1,
  display_cells: i % 2 ? [
    { id: `cx-${i}-a`, kind: 'exploration', title: 'Explored', summary: 'Read Makefile, deploy.sh', status: 'ok',
      lines: [{ label: 'Read', text: 'Makefile', kind: 'detail' }, { label: 'Read', text: 'scripts/deploy.sh', kind: 'detail' }] },
    { id: `cx-${i}-b`, kind: 'command', title: 'Ran', summary: `make deploy-${i}`, status: i % 10 === 9 ? 'error' : 'ok',
      lines: [{ text: 'deploying…', kind: 'output' }, { text: i % 10 === 9 ? 'error: timeout' : 'done', kind: 'output' }] },
  ] : [] }));

const server = http.createServer((req, res) => {
  const url = new URL(req.url, `http://127.0.0.1:${PORT}`);
  const p = url.pathname;
  requests.push(`${req.method} ${p}${url.search}`);
  if (p === '/' || p === '/index.html') return sendFile(res, path.join(BUILD, 'index.html'));
  if (p === '/styles.css') return sendFile(res, path.join(repo, 'static/styles.css'));
  if (p.startsWith('/static/app/')) return sendFile(res, path.join(BUILD, p.slice('/static/'.length)));
  if (p.startsWith('/static/')) return sendFile(res, path.join(repo, p.slice(1)));
  if (p === '/server-info') {
    return sendJson(res, { name: 'fake', capabilities: { version: 1,
      features: NO_LIVE ? ['tool_explanations'] : ['live_items', 'tool_explanations'] },
      contract: { host: 40, min_ios: 1, features: { live_items: 40 } } });
  }
  if (p === '/agents/snapshot') {
    return sendJson(res, { agents: [agentRow(), { session: 'mike', name: 'Mike', persona: 'Mike', agent_id: 'agent-2',
      alive: true, backend: 'codex', latest_state: 'done', busy: false, conversation_id: 'conv-m', head_revision: 150 }],
      focused: 'rachel',
      tool_explanations: { enabled: EXPLAIN, detail_level: 2 } });
  }
  if (p === '/log' && url.searchParams.get('session') === 'mike') {
    const after = Number(url.searchParams.get('after_revision') || 0);
    const before = url.searchParams.get('before');
    if (before) {
      const end = mikeTurns.findIndex(t => t.id === before);
      const page = mikeTurns.slice(Math.max(0, end - 100), end);
      return sendJson(res, { session: 'mike', conversation_id: 'conv-m', turns: page, latest_revision: 150,
        has_more: end - 100 > 0 });
    }
    if (after) return sendJson(res, { session: 'mike', conversation_id: 'conv-m', turns: mikeTurns.filter(t => t.revision > after),
      latest_revision: 150, has_more: false });
    return sendJson(res, { session: 'mike', conversation_id: 'conv-m', turns: mikeTurns.slice(-100),
      latest_revision: 150, has_more: true, cwd: '/home/demo/deploy' });
  }
  if (p === '/live' && url.searchParams.get('session') === 'mike') {
    return sendJson(res, { conv: 'conv-m', session: 'mike', agent_id: 'agent-2', epoch: 'boot-a', lseq: 0,
      server_now_ms: Date.now(), activity: { state: 'idle' }, turn: null, items: [] });
  }
  if (p === '/log') {
    const after = Number(url.searchParams.get('after_revision') || 0);
    const turns = logTurns.filter(t => t.revision > after);
    return sendJson(res, { session: 'rachel', conversation_id: conv, turns, latest_revision: logRevision,
      has_more: false, cwd: '/home/demo/parser' });
  }
  if (p === '/live') {
    if (url.searchParams.get('session') !== 'rachel') return sendJson(res, { error: 'unknown session' }, 404);
    return sendJson(res, { conv, session: 'rachel', agent_id: 'agent-1', epoch: liveState.epoch,
      lseq: liveState.lseq, server_now_ms: Date.now(), activity: liveState.activity, turn: liveState.turn,
      items: liveItems(liveState), tool_explanations: { enabled: EXPLAIN, detail_level: 2 } });
  }
  if (p === '/tool-explanations/settings') {
    if (req.method === 'POST') {
      let body = '';
      req.on('data', c => { body += c; });
      req.on('end', () => {
        try { const b = JSON.parse(body || '{}'); if ('enabled' in b) EXPLAIN = !!b.enabled; } catch (_) {}
        sendJson(res, { enabled: EXPLAIN, detail_level: 2 });
      });
      return;
    }
    return sendJson(res, { enabled: EXPLAIN, detail_level: 2 });
  }
  if (p === '/events') {
    res.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-store', Connection: 'keep-alive' });
    res.write(': hello\n\n');
    clients.add(res);
    const ping = setInterval(() => res.write(': ping\n\n'), 5000);
    req.on('close', () => { clearInterval(ping); clients.delete(res); });
    return;
  }
  if (p === '/fake/clock') return sendJson(res, { ms: Date.now() - start, turnB_ms: bStart - start, end_ms: bClock - start });
  if (p === '/fake/requests') return sendJson(res, requests);
  if (p === '/clog' && req.method === 'POST') {
    let body = '';
    req.on('data', c => { body += c; });
    req.on('end', () => { requests.push(`CLOG ${body.slice(0, 2000)}`); sendJson(res, { ok: true }); });
    return;
  }
  if (req.method === 'POST' || p === '/sw.js') return sendJson(res, { ok: true });
  return sendJson(res, {}, 404);
});

server.listen(PORT, '127.0.0.1', () => {
  console.log(`fake live host on http://127.0.0.1:${PORT} (turn A at +0 s, turn B at +${Math.round((bStart - start) / 1000)} s, ends +${Math.round((bClock - start) / 1000)} s)`);
});
