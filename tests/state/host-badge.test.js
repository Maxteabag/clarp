import { describe, expect, it } from 'vitest';
import { hostBadgeShown, hostBadgeText } from '../../static/lib/host-badge.js';
import { normalizeMoved } from '../../static/lib/agent-moved.js';

const two = [{ id: 'h1', name: 'host-a' }, { id: 'h2', name: 'host-b Rust' }];

describe('the Host capsule on a chat', () => {
  it('never shows with one Host, nor with the setting off', () => {
    expect(hostBadgeShown(true, 1)).toBe(false);
    expect(hostBadgeShown(true, 2)).toBe(true);
    expect(hostBadgeShown(false, 2)).toBe(false);
    expect(hostBadgeText({ hosts: [two[0]], rowHost: 'h1' })).toBe('');
    expect(hostBadgeText({ setting: false, hosts: two, rowHost: 'h2' })).toBe('');
  });

  it('names the Host a row came from', () => {
    expect(hostBadgeText({ hosts: two, rowHost: 'h2' })).toBe('host-b Rust');
    expect(hostBadgeText({ hosts: two })).toBe('host-a');          // a row with no Host: the one read first
    expect(hostBadgeText({ hosts: two, rowHost: 'unknown' })).toBe('');
  });

  it('names a moved row\'s destination when that Host is not read', () => {
    const away = normalizeMoved({ to: { host: 'host-c', id: 'h3' } });
    const here = normalizeMoved({ to: { host: 'host-b Rust', id: 'h2' } });
    expect(hostBadgeText({ hosts: two, rowHost: 'h1', moved: away })).toBe('host-c');
    expect(hostBadgeText({ hosts: two, rowHost: 'h1', moved: here })).toBe('host-a');
    expect(hostBadgeText({ hosts: [two[0]], rowHost: 'h1', moved: away })).toBe('');
  });
});
