// Cache-first chat open: the last page of a chat is kept on the device so a
// chat paints at once and asks the Host only for what changed since. Writes
// are cheap for the device: the first 500 ms after a change, then at most
// one per 10 s per chat, and none while the agent is working.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createTranscriptCache, cacheRecord } from '@core/transcript-cache.js';

function memoryStorage() {
  const rows = new Map();
  return {
    rows,
    puts: 0,
    async get(session) { return rows.get(session) || null; },
    async put(record) { this.puts++; rows.set(record.session, structuredClone(record)); },
    async delete(session) { rows.delete(session); },
  };
}

const turns = n => Array.from({ length: n }, (_, i) => ({ id: `m${i}`, role: 'assistant', text: `t${i}`, revision: i + 1 }));

describe('transcript cache', () => {
  let storage;
  let busy;
  let cache;
  beforeEach(() => {
    vi.useFakeTimers();
    storage = memoryStorage();
    busy = false;
    cache = createTranscriptCache({ storage, isBusy: () => busy });
  });
  afterEach(() => vi.useRealTimers());

  const state = { conversationId: 'c1', cursor: 12, latestTs: 'x', hasMore: true, cwd: '/w' };

  it('keeps the newest page of a chat with its cursor', () => {
    const rec = cacheRecord('rachel', { ...state, turns: turns(130) });
    expect(rec).toMatchObject({ session: 'rachel', conversation_id: 'c1', latest_revision: 12, has_more: true });
    expect(rec.turns).toHaveLength(100);
    expect(rec.turns[0].id).toBe('m30');
  });

  it('leaves optimistic and streaming rows out', () => {
    const rec = cacheRecord('rachel', { ...state, turns: [
      { id: 'u-1', optimistic: true, text: 'hi' }, { id: 'live-1', kind: 'live', text: 'x' }, { id: 'm', text: 'ok', revision: 2 }] });
    expect(rec.turns.map(t => t.id)).toEqual(['m']);
  });

  it('writes 500 ms after a change, then at most once per 10 s', async () => {
    cache.changed('rachel', () => ({ ...state, turns: turns(3) }));
    await vi.advanceTimersByTimeAsync(499);
    expect(storage.puts).toBe(0);
    await vi.advanceTimersByTimeAsync(1);
    expect(storage.puts).toBe(1);
    for (let i = 0; i < 20; i++) {
      cache.changed('rachel', () => ({ ...state, turns: turns(4 + i) }));
      await vi.advanceTimersByTimeAsync(400);
    }
    expect(storage.puts).toBe(1);
    await vi.advanceTimersByTimeAsync(2000);
    expect(storage.puts).toBe(2);
    expect((await storage.get('rachel')).turns).toHaveLength(23);
  });

  it('does not write while the agent works, and catches up after', async () => {
    busy = true;
    cache.changed('rachel', () => ({ ...state, turns: turns(3) }));
    await vi.advanceTimersByTimeAsync(30000);
    expect(storage.puts).toBe(0);
    busy = false;
    await vi.advanceTimersByTimeAsync(3000);
    expect(storage.puts).toBe(1);
  });

  it('reads back what it wrote', async () => {
    cache.changed('rachel', () => ({ ...state, turns: turns(2) }));
    await vi.advanceTimersByTimeAsync(500);
    expect((await cache.load('rachel')).turns.map(t => t.id)).toEqual(['m0', 'm1']);
    expect(await cache.load('nobody')).toBe(null);
  });

  it('forgets a chat whose conversation was replaced', async () => {
    cache.changed('rachel', () => ({ ...state, turns: turns(2) }));
    await vi.advanceTimersByTimeAsync(500);
    await cache.forget('rachel');
    expect(await cache.load('rachel')).toBe(null);
  });
});
