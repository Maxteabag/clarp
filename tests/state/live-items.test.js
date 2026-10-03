// Reducer rules beyond the recorded streams in contract/live.

import { describe, expect, it } from 'vitest';
import { applyLiveEvent, applyLiveSnapshot, blankLive, liveItems } from '@core/live-items.js';

const base = () => applyLiveSnapshot(blankLive(), {
  epoch: 'e', lseq: 3, activity: { state: 'idle' },
  turn: { turn_id: 'tr-1', status: 'completed' },
  items: [{ id: 'cx:a', kind: 'message', turn_id: 'tr-1', ordinal: 1, rev: 2, text: 'old answer' }],
}, 0);
const ev = (lseq, ops) => ({ type: 'live', epoch: 'e', lseq, ops });

describe('live reducer', () => {
  it('drops the last turn’s items when a new turn starts (they are in /log now)', () => {
    const r = applyLiveEvent(base(), ev(4, [{ op: 'turn', turn: { turn_id: 'tr-2', status: 'running' } }]), 0);
    expect(liveItems(r.state)).toEqual([]);
    expect(r.state.turn.turn_id).toBe('tr-2');
  });

  it('keeps the items when the same turn settles', () => {
    const r = applyLiveEvent(base(), ev(4, [{ op: 'turn', turn: { turn_id: 'tr-1', status: 'completed', worked_ms: 5 } }]), 0);
    expect(liveItems(r.state).map(i => i.id)).toEqual(['cx:a']);
  });

  it('asks for a snapshot when an op skips an item revision', () => {
    const r = applyLiveEvent(base(), ev(4, [{ op: 'append', id: 'cx:a', kind: 'message', rev: 4, field: 'text', chunk: '!' }]), 0);
    expect(r.effects).toEqual(['fetch_live']);
    expect(liveItems(r.state)[0].text).toBe('old answer');
  });

  it('ignores ops it does not know', () => {
    const r = applyLiveEvent(base(), ev(4, [{ op: 'sparkle', id: 'cx:a' }]), 0);
    expect(r.effects).toEqual([]);
    expect(r.state.lseq).toBe(4);
  });

  it('leaves untouched items as they were, so their rows do not re-render', () => {
    const held = applyLiveEvent(base(), ev(4, [{ op: 'upsert', id: 'cx:b', kind: 'tool', rev: 1,
      item: { id: 'cx:b', kind: 'tool', turn_id: 'tr-1', ordinal: 2 } }]), 0).state;
    const next = applyLiveEvent(held, ev(5, [{ op: 'done', id: 'cx:b', kind: 'tool', rev: 2, status: 'completed' }]), 0).state;
    expect(next.items['cx:a']).toBe(held.items['cx:a']);
    expect(next.items['cx:b']).not.toBe(held.items['cx:b']);
  });
});
