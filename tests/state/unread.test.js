// The unread badge is counted on every agent status change (many a second
// while agents work), across every agent: it must stay linear and must not
// re-parse localStorage per call.

import { describe, expect, it } from 'vitest';
import { countUnread, createJsonReader } from '@core/unread.js';

describe('countUnread', () => {
  const base = { current: 'a', seen: { b: 10, c: 50 }, notifications: { a: 99, b: 20, c: 40, d: 5, x: 9 } };

  it('counts other visible agents with a notification newer than the last visit', () => {
    expect(countUnread(['a', 'b', 'c', 'd', 'x'], { ...base, available: ['a', 'b', 'c', 'd'] })).toBe(2);
  });

  it('leaves out the open chat and agents that are not listed', () => {
    expect(countUnread(['a', 'x'], { ...base, available: ['a'] })).toBe(0);
  });

  it('stays fast across a large fleet', () => {
    const sessions = Array.from({ length: 20000 }, (_, i) => `s${i}`);
    const notifications = Object.fromEntries(sessions.map(s => [s, 2]));
    const t = performance.now();
    expect(countUnread(sessions, { available: sessions, current: 's0', seen: {}, notifications })).toBe(19999);
    expect(performance.now() - t).toBeLessThan(100);
  });
});

describe('createJsonReader', () => {
  it('parses a stored value once until it changes', () => {
    const store = new Map([['k', '{"a":1}']]);
    let parses = 0;
    const read = createJsonReader(key => store.get(key) ?? null, raw => { parses++; return JSON.parse(raw); });
    expect(read('k')).toEqual({ a: 1 });
    read('k'); read('k');
    expect(parses).toBe(1);
    store.set('k', '{"a":2}');
    expect(read('k')).toEqual({ a: 2 });
    expect(parses).toBe(2);
  });

  it('reads a missing or broken value as empty', () => {
    const read = createJsonReader(key => (key === 'bad' ? '{' : null));
    expect(read('none')).toEqual({});
    expect(read('bad')).toEqual({});
  });
});
