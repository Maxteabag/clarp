import { describe, it, expect } from 'vitest';
import { chooseSession, visibleSessions } from '../../static/lib/session-select.js';

describe('chooseSession', () => {
  it('keeps the current session when the server still lists it', () => {
    expect(chooseSession({
      sessions: ['elli', 'mike-085f'],
      serverDefault: 'claude',
      current: 'mike-085f',
    })).toBe(null);
  });

  it('ignores a server default that names no live session (regression)', () => {
    // /sessions answers {"sessions": [...26 live...], "default": "claude"}
    // where "claude" has no agent row. Selecting it made /select 404 and left
    // the client on a session the server does not know.
    expect(chooseSession({
      sessions: ['elli', 'mike-085f'],
      serverDefault: 'claude',
      current: 'claude',
    })).toBe('elli');
  });

  it('prefers the server default when it is live', () => {
    expect(chooseSession({
      sessions: ['elli', 'mike-085f'],
      serverDefault: 'mike-085f',
      current: 'stale',
    })).toBe('mike-085f');
  });

  it('falls back to the first live session', () => {
    expect(chooseSession({
      sessions: ['elli', 'mike-085f'],
      current: 'stale',
    })).toBe('elli');
  });

  it('keeps the current session when the roster is empty', () => {
    expect(chooseSession({ sessions: [], serverDefault: 'claude', current: 'claude' })).toBe(null);
    expect(chooseSession({ serverDefault: 'claude', current: 'claude' })).toBe(null);
    expect(chooseSession()).toBe(null);
  });

  it('skips blank and non-string entries', () => {
    expect(chooseSession({
      sessions: ['', null, 0, 'yuki'],
      current: 'stale',
    })).toBe('yuki');
  });
});

describe('visibleSessions', () => {
  it('keeps archived chats in the snapshot but out of daily navigation', () => {
    expect(visibleSessions(['mike', 'old'], {
      mike: { archived_at: 0 }, old: { archived_at: 1_788_000_000 },
    })).toEqual(['mike']);
  });

  it('keeps compatibility behavior when old snapshots omit archive state', () => {
    expect(visibleSessions(['mike', 'rachel'], {})).toEqual(['mike', 'rachel']);
  });
});
