// Where the live turn goes among the durable /log rows (same rules as
// clarp-ios 3fb7d67): before the first row written after its turn started,
// drawn only for the newest turn_id, so the previous turn's durable rows take
// over in place without anything moving. Driven by contract/live/turn-full.

import { describe, expect, it } from 'vitest';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { applyLiveEvent, applyLiveSnapshot, blankLive } from '@core/live-items.js';
import { partFold } from '@core/live-present.js';
import { LIVE_ENTRY, composeTimeline, liveSegmentItems, withLiveSegments } from '@core/live-timeline.js';

const here = path.dirname(fileURLToPath(import.meta.url));
const fixture = JSON.parse(fs.readFileSync(path.join(here, '..', '..', 'contract', 'live', 'turn-full.json'), 'utf8'));
const T0 = 1759480000000;                       // turn tr-1 started_at_ms in the fixture
const iso = ms => new Date(ms).toISOString();

function replay(untilLseq = Infinity) {
  let live = blankLive();
  for (const step of fixture.steps) {
    if (step.snapshot) live = applyLiveSnapshot(live, step.snapshot, 0);
    else if (step.event.lseq <= untilLseq) live = applyLiveEvent(live, step.event, 0).state;
  }
  return live;
}

const prompt = { id: 'u-a', role: 'user', text: 'The parser test fails on CI.', timestamp: iso(T0 - 400), revision: 1 };
// The commentary message's /log row (row_id live-abc), streaming then settled in place.
const liveRow = { id: 'live-abc', role: 'assistant', kind: 'live', text: 'Let me look at the parser and its tests.', timestamp: iso(T0 + 4300), revision: 2 };
const order = r => r.entries.map(e => (e.type === 'live' ? 'LIVE' : e.item.id));

describe('composeTimeline', () => {
  it('a running turn sits right after its prompt', () => {
    const r = composeTimeline({ turns: [prompt, liveRow], liveState: replay(9) });
    expect(order(r)).toEqual(['u-a', 'LIVE']);
  });

  it('a message delivered after a settled turn lands below that turn, not above it', () => {
    const next = { id: 'u-b', role: 'user', text: 'Now fix it.', timestamp: iso(T0 + 15000), revision: 3 };
    const r = composeTimeline({ turns: [prompt, liveRow, next], liveState: replay() });
    expect(r.items.length).toBeGreaterThan(0);
    expect(order(r)).toEqual(['u-a', 'LIVE', 'u-b']);
  });

  it('a new turn retires the last turn’s live rows and its durable rows take over in place', () => {
    const settledRow = { ...liveRow, kind: null, revision: 4 };
    const next = { id: 'u-b', role: 'user', text: 'Now fix it.', timestamp: iso(T0 + 15000), revision: 5 };
    let live = replay();
    const conv = 'conv-1';
    live = applyLiveEvent(live, { type: 'live', epoch: live.epoch, lseq: live.lseq + 1, ops: [
      { op: 'turn', conv, turn: { turn_id: 'tr-2', status: 'running', started_at_ms: T0 + 15100 } },
      { op: 'upsert', conv, id: 'cl:msg_09:0', kind: 'reasoning', rev: 1,
        item: { id: 'cl:msg_09:0', conv, turn_id: 'tr-2', kind: 'reasoning', status: 'running', ordinal: 20,
          started_at_ms: T0 + 15200, text: '' } },
    ] }, 0).state;
    const r = composeTimeline({ turns: [prompt, settledRow, next], liveState: live });
    expect(r.items.map(i => i.turn_id)).toEqual(['tr-2']);
    expect(order(r)).toEqual(['u-a', 'live-abc', 'u-b', 'LIVE']);
  });

  it('keeps the takeover set between events with the same rows', () => {
    const first = composeTimeline({ turns: [prompt, liveRow], liveState: replay(8) });
    const again = composeTimeline({ turns: [prompt, liveRow], liveState: replay(9), previousHidden: first.hidden });
    expect(again.hidden).toBe(first.hidden);
  });
});

describe('withLiveSegments: a message the turn read while it ran', () => {
  const T = 1795000000000;
  const at = ms => new Date(ms).toISOString();
  const u1 = { id: 'u1', role: 'user', timestamp: at(T - 100) };
  const steer = { id: 'u2', role: 'user', timestamp: at(T + 5000) };
  const after = { id: 'u3', role: 'user', timestamp: at(T + 30000) };
  const entries = rows => rows.map(item => ({ type: 'turn', key: `t:${item.id}`, item }));
  const keys = list => list.map(e => (e.type === 'live' ? e.key : e.item.id));
  const items = [
    { id: 'list', started_at_ms: T + 200 },
    { id: 'banana', started_at_ms: T + 5000 },
    { id: 'later', started_at_ms: T + 9000 },
  ];

  it('splits the running turn at the steer, so the answer to it is drawn below it', () => {
    const out = withLiveSegments(entries([u1, steer]), 1, T, null);
    expect(keys(out)).toEqual(['u1', 'live-turn', 'u2', 'live-turn:1']);
    expect(out.filter(e => e.type === 'live').map(e => e.last)).toEqual([false, true]);
    expect(liveSegmentItems(items, out[1]).map(i => i.id)).toEqual(['list']);
    expect(liveSegmentItems(items, out[3]).map(i => i.id)).toEqual(['banana', 'later']);
  });

  it('keeps one block, under its prompt, when nothing was read into the turn', () => {
    const out = withLiveSegments(entries([u1]), 1, T, null);
    expect(out[1]).toBe(LIVE_ENTRY);
    expect(liveSegmentItems(items, out[1])).toBe(items);
  });

  it('does not split at a prompt sent after the turn ended', () => {
    const out = withLiveSegments(entries([u1, after]), 1, T, T + 20000);
    expect(keys(out)).toEqual(['u1', 'live-turn', 'u3']);
  });

  it('splits at the steer but not at a later prompt once the turn ended', () => {
    const out = withLiveSegments(entries([u1, steer, after]), 1, T, T + 20000);
    expect(keys(out)).toEqual(['u1', 'live-turn', 'u2', 'live-turn:1', 'u3']);
    expect(liveSegmentItems(items, out[3]).map(i => i.id)).toEqual(['banana', 'later']);
  });
});

describe('partFold: a settled steered turn draws its chrome once', () => {
  const turn = { turn_id: 't', status: 'failed', started_at_ms: 1, ended_at_ms: 9001, worked_ms: 9000,
    tool_count: 2, error: { message: 'Turn failed', detail: 'x' } };
  const tool = id => ({ id, type: 'tool', status: 'completed' });
  const msg = id => ({ id, type: 'message' });
  const before = [tool('a'), tool('b'), msg('list')];
  const after = [msg('banana')];

  it('one fold label and one error, in the part the turn ends in', () => {
    const parts = [partFold(turn, before, { last: false }), partFold(turn, after, { last: true, wholeRows: [...before, ...after] })];
    expect(parts.filter(p => p.label).length).toBe(1);
    expect(parts.filter(p => p.error).length).toBe(1);
    expect(parts[1].label).toMatch(/^Failed after/);
  });

  it('the last part opens the work folded in an earlier part', () => {
    const last = partFold(turn, after, { last: true, wholeRows: [...before, ...after] });
    expect(last.folded.length).toBe(0);
    expect(last.opens).toBe(true);
    expect(partFold(turn, before, { last: false }).folded.map(r => r.id)).toEqual(['a', 'b']);
  });

  it('an unsplit turn is its own last part, as before', () => {
    const whole = partFold({ ...turn, status: 'completed', error: null }, [...before, ...after]);
    expect(whole.label).toMatch(/^Worked for/);
    expect(whole.opens).toBe(true);
    expect(whole.error).toBe(null);
  });
});
