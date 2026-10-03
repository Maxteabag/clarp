// Where the live turn goes among the durable /log rows (same rules as
// clarp-ios 3fb7d67): before the first row written after its turn started,
// drawn only for the newest turn_id, so the previous turn's durable rows take
// over in place without anything moving. Driven by contract/live/turn-full.

import { describe, expect, it } from 'vitest';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { applyLiveEvent, applyLiveSnapshot, blankLive } from '@core/live-items.js';
import { composeTimeline } from '@core/live-timeline.js';

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
