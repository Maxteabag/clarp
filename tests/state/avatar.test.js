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

  it('wears the model portrait only when the computer prefers it', () => {
    const agents = {
      rachel: {
        name: 'Rachel',
        avatar_url: '',
        model_avatar_url: '/static/avatars/models/rachel.opus.png?v=abc',
      },
    };

    expect(resolveAvatarUrl(agents, 'Rachel', 'rachel'))
      .toBe('/static/avatars/rachel.png');
    expect(resolveAvatarUrl(agents, 'Rachel', 'rachel', { preferModel: true }))
      .toBe('/static/avatars/models/rachel.opus.png?v=abc');
  });

  it('falls back to the bundled portrait when no model variant is bundled', () => {
    const agents = { mike: { name: 'Mike', avatar_url: '', model_avatar_url: '' } };

    expect(resolveAvatarUrl(agents, 'Mike', 'mike', { preferModel: true }))
      .toBe('/static/avatars/mike.png');
  });

  it('keeps a chosen portrait even when model portraits are preferred', () => {
    // The server withholds model_avatar_url for a custom portrait; this
    // pins the client half of that contract.
    const agents = {
      gordon: { name: 'Gordon', avatar_url: '/avatars/gordon?v=1', model_avatar_url: '' },
    };

    expect(resolveAvatarUrl(agents, 'Gordon', 'gordon', { preferModel: true }))
      .toBe('/avatars/gordon?v=1');
  });
});
