// Regression: tool calls rendered below the last message regardless of when
// they ran, because the transcript drew every turn and then every live
// activity row as two separate blocks.

import { describe, it, expect } from 'vitest';
import { mergeTimeline } from '@core/timeline.js';

const at = iso => new Date(iso).getTime();
const turn = (id, iso, text = id) => ({ id, timestamp: iso, text });
const act = (key, iso, summary = key) => ({ key, ts: at(iso), summary });

const order = merged => merged.map(e => (e.type === 'turn' ? e.item.id : e.item.key));

describe('mergeTimeline', () => {
  it('interleaves activity between the messages it happened between', () => {
    const turns = [turn('m1', '2026-09-01T10:00:00Z'), turn('m2', '2026-09-01T10:02:00Z')];
    const activity = [act('tool', '2026-09-01T10:01:00Z')];
    expect(order(mergeTimeline(turns, activity))).toEqual(['m1', 'tool', 'm2']);
  });

  it('no longer parks every tool call at the end', () => {
    const turns = [
      turn('m1', '2026-09-01T10:00:00Z'),
      turn('m2', '2026-09-01T10:05:00Z'),
      turn('m3', '2026-09-01T10:09:00Z'),
    ];
    const activity = [
      act('a', '2026-09-01T10:01:00Z'),
      act('b', '2026-09-01T10:06:00Z'),
      act('c', '2026-09-01T10:07:00Z'),
    ];
    expect(order(mergeTimeline(turns, activity)))
      .toEqual(['m1', 'a', 'm2', 'b', 'c', 'm3']);
  });

  it('keeps activity at the end when it really is the most recent', () => {
    const turns = [turn('m1', '2026-09-01T10:00:00Z')];
    const activity = [act('a', '2026-09-01T10:01:00Z')];
    expect(order(mergeTimeline(turns, activity))).toEqual(['m1', 'a']);
  });

  it('keeps the live thinking row last however stale its neighbours are', () => {
    const turns = [turn('m1', '2030-01-01T00:00:00Z')];
    const activity = [{ key: 'thinking-live', thinkingLive: true }];
    expect(order(mergeTimeline(turns, activity))).toEqual(['m1', 'thinking-live']);
  });

  it('preserves arrival order within the same instant', () => {
    const iso = '2026-09-01T10:00:00Z';
    const turns = [turn('m1', iso), turn('m2', iso)];
    const activity = [act('a', iso), act('b', iso)];
    // Turns before activity at an identical timestamp, each group in order.
    expect(order(mergeTimeline(turns, activity))).toEqual(['m1', 'm2', 'a', 'b']);
  });

  it('keeps an untimed turn beside the ones it arrived with', () => {
    const turns = [
      turn('m1', '2026-09-01T10:00:00Z'),
      { id: 'm2', text: 'no timestamp' },
      turn('m3', '2026-09-01T10:09:00Z'),
    ];
    const activity = [act('a', '2026-09-01T10:05:00Z')];
    expect(order(mergeTimeline(turns, activity))).toEqual(['m1', 'm2', 'a', 'm3']);
  });

  it('handles an epoch-ms turn timestamp as well as an ISO one', () => {
    const turns = [{ id: 'm1', timestamp: at('2026-09-01T10:03:00Z') }];
    const activity = [act('a', '2026-09-01T10:01:00Z')];
    expect(order(mergeTimeline(turns, activity))).toEqual(['a', 'm1']);
  });

  it('puts wholly untimed rows first rather than losing them', () => {
    const merged = mergeTimeline([{ id: 'm1' }], [{ key: 'a', ts: at('2026-09-01T10:00:00Z') }]);
    expect(order(merged)).toEqual(['m1', 'a']);
  });

  it('survives junk timestamps', () => {
    const turns = [turn('m1', 'not a date'), turn('m2', '2026-09-01T10:00:00Z')];
    const activity = [{ key: 'a', ts: 'nonsense' }];
    expect(order(mergeTimeline(turns, activity))).toHaveLength(3);
  });

  it('is empty for empty input, and works with either side missing', () => {
    expect(mergeTimeline()).toEqual([]);
    expect(order(mergeTimeline([turn('m1', '2026-09-01T10:00:00Z')], []))).toEqual(['m1']);
    expect(order(mergeTimeline([], [act('a', '2026-09-01T10:00:00Z')]))).toEqual(['a']);
  });

  it('gives every entry a key that cannot collide across the two sources', () => {
    const merged = mergeTimeline(
      [turn('x', '2026-09-01T10:00:00Z')],
      [act('x', '2026-09-01T10:01:00Z')],
    );
    expect(new Set(merged.map(e => e.key)).size).toBe(2);
  });
});
