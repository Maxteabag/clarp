// The all-Hosts rules every client shares (issue 651; clarp-ios AllHostsReadingTests), in the
// web app's copy static/lib/all-hosts.js.
import { describe, expect, it } from 'vitest';
import {
  badgeHost, hostLabel, mergeChats, migration, MIGRATION_KEY, originOf, PREFERENCE_KEY, PRIMARY,
  readIds, resolveAllHosts, superseded, unreachable, unreachableLine,
} from '../../static/lib/all-hosts.js';
import { hostBadgeText } from '../../static/lib/host-badge.js';

function memory() {
  const data = new Map();
  return { getItem: k => (data.has(k) ? data.get(k) : null), setItem: (k, v) => data.set(k, String(v)), data };
}

describe('the preference and its one-time default', () => {
  it('switches on once at first launch with two saved Hosts, and the user decides afterwards', () => {
    const store = memory();
    expect(resolveAllHosts(store, 2)).toBe(true);
    expect(store.getItem(MIGRATION_KEY)).toBe('1');
    store.setItem(PREFERENCE_KEY, '0');
    expect(resolveAllHosts(store, 3)).toBe(false);
  });
  it('stays off with one saved Host, and adding one later changes nothing', () => {
    const store = memory();
    expect(resolveAllHosts(store, 1)).toBe(false);
    expect(resolveAllHosts(store, 2)).toBe(false);
  });
  it('defers the default while the saved Hosts cannot be read', () => {
    const store = memory();
    expect(resolveAllHosts(store, 0, false)).toBe(false);
    expect(store.getItem(MIGRATION_KEY)).toBe(null);
    expect(resolveAllHosts(store, 2)).toBe(true);
  });
  it('keeps a choice made before the migration', () => {
    expect(migration({ stored: true, savedHosts: 1 })).toEqual({ on: true, migrated: true });
    expect(migration({ stored: false, migrated: true, savedHosts: 3 })).toBe(null);
  });
});

describe('which Hosts are read', () => {
  it('reads the primary alone while off, and every saved Host after it while on', () => {
    expect(readIds(['studio', 'lab'], false)).toEqual([PRIMARY]);
    expect(readIds(['studio', 'lab'], true)).toEqual([PRIMARY, 'studio', 'lab']);
    expect(readIds([], true)).toEqual([PRIMARY]);
  });
});

describe('naming the Host', () => {
  it('shows the badge with the setting and more than one saved Host', () => {
    expect(hostLabel(' Studio ', true, 2)).toBe('Studio');
    expect(hostLabel('Studio', true, 1)).toBe('');
    expect(hostLabel('Studio', false, 3)).toBe('');
    expect(hostLabel('  ', true, 3)).toBe('');
  });
  it('names a moved row by its destination when that is not read', () => {
    const listed = new Set(['sim', 'studio-id']);
    expect(badgeHost('Simulated', { to: { host: 'Rust Host', id: 'other' } }, listed)).toBe('Rust Host');
    expect(badgeHost('Simulated', null, listed)).toBe('Simulated');
    expect(badgeHost('Simulated', { to: { host: ' ', id: 'x' } }, listed)).toBe('Simulated');
    expect(badgeHost('Simulated', { to: { host: 'Studio', id: 'studio-id' } }, listed)).toBe('Simulated');
  });
  it('counts saved Hosts for the badge and read ones for the moved row', () => {
    const hosts = [{ id: 'a', name: 'host-a' }, { id: 'b', name: 'host-b' }];
    const away = { to: { host: 'host-b', id: 'b' }, at: 1 };
    expect(hostBadgeText({ hosts, listed: ['a'], rowHost: 'a', moved: away })).toBe('host-b');   // b saved, not read
    expect(hostBadgeText({ hosts, listed: ['a', 'b'], rowHost: 'a', moved: away })).toBe('host-a');
    expect(hostBadgeText({ hosts, listed: ['a'], rowHost: 'a' })).toBe('host-a');                // shown while only one is read
  });
});

describe('a moved agent', () => {
  it('is left out when its destination is read, matched by server_id and never by name', () => {
    const listed = new Set(['sim', 'studio-id']);
    expect(superseded({ to: { host: 'Studio', id: 'studio-id' } }, listed)).toBe(true);
    expect(superseded({ to: { host: 'studio-id', id: 'other' } }, listed)).toBe(false);
    expect(superseded({ to: { host: 'Studio', id: '' } }, listed)).toBe(false);
    expect(superseded(null, listed)).toBe(false);
  });
});

describe('the merged list', () => {
  it('orders by last activity, then the primary first, then saved order, and drops superseded rows', () => {
    const groups = [
      { hostId: PRIMARY, rows: [{ sid: 'theo', name: 'Theo', lastActivity: 10 },
                                { sid: 'mia', name: 'Mia', lastActivity: 5, moved: { to: { host: 'Lab', id: 'lab-id' } } }] },
      { hostId: 'studio', rows: [{ sid: 'ada', name: 'Ada', lastActivity: 20 }, { sid: 'theo', name: 'Theo', lastActivity: 10 }] },
      { hostId: 'lab', rows: [{ sid: 'mia', name: 'Mia', lastActivity: 10 }] },
    ];
    const out = mergeChats(groups, new Set(['primary-id', 'studio-id', 'lab-id']));
    expect(out.map(r => r.key)).toEqual(['studio:ada', 'primary:theo', 'studio:theo', 'lab:mia']);
  });
});

describe('a Host that cannot be reached', () => {
  it('gets the quiet line only among several', () => {
    const states = { studio: 'live', lab: 'offline', old: 'refused', new: 'connecting' };
    expect(unreachable([PRIMARY, 'studio', 'lab', 'old', 'new'], states)).toEqual(['lab', 'old']);
    expect(unreachable([PRIMARY], states)).toEqual([]);
    expect(unreachableLine('Lab')).toBe('Lab unreachable');
  });
});

describe('a Host\'s address', () => {
  it('is kept as its origin', () => {
    expect(originOf('https://studio.example.ts.net/some/path?x=1')).toBe('https://studio.example.ts.net');
    expect(originOf('studio.example.ts.net')).toBe('https://studio.example.ts.net');
    expect(originOf('http://100.64.0.2:7682')).toBe('http://100.64.0.2:7682');
    expect(originOf('ftp://studio')).toBe('');
    expect(originOf('https://u:p@studio')).toBe('');
    expect(originOf('')).toBe('');
  });
});
