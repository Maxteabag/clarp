import { describe, expect, it } from 'vitest';
import { resolveAvatarUrl } from '../../static/lib/avatar.js';

describe('PWA avatar resolution', () => {
  it('uses the content-versioned snapshot route for a custom agent', () => {
    const agents = {
      gordon: {
        name: 'Gordon',
        avatar_url: '/avatars/custom-agent?v=portrait-1',
      },
    };

    expect(resolveAvatarUrl(agents, 'Gordon', 'gordon'))
      .toBe('/avatars/custom-agent?v=portrait-1');
  });

  it('keeps bundled portraits as the fallback for built-in agents', () => {
    const agents = { mike: { name: 'Mike', avatar_url: '' } };

    expect(resolveAvatarUrl(agents, 'Mike', 'mike'))
      .toBe('/static/avatars/mike.png');
  });

  it('does not borrow an avatar from a duplicate display name', () => {
    const agents = {
      first: { name: 'Nova', avatar_url: '/avatars/first?v=1' },
      second: { name: 'Nova', avatar_url: '/avatars/second?v=2' },
    };

    expect(resolveAvatarUrl(agents, 'Nova', 'second'))
      .toBe('/avatars/second?v=2');
  });

  it('uses an initial placeholder instead of a broken image for custom agents without portraits', () => {
    const agents = { nova: { name: 'Nova', avatar_url: '' } };

    expect(resolveAvatarUrl(agents, 'Nova', 'nova')).toBe('');
  });
});
