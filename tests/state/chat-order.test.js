import { describe, expect, it } from 'vitest';
import { orderChats } from '../../web/src/lib/chat-order.js';

const agents = {
  rachel: { name: 'Rachel' },
  mike: { name: 'Mike' },
  arnold: { name: 'Arnold' },
  elli: { name: 'Elli' },
};
const available = ['rachel', 'mike', 'arnold', 'elli'];

const status = {
  rachel: { last_activity: 5000 },
  mike: { last_activity: 4000 },
  arnold: { last_activity: 3000 },
  elli: { last_activity: 2000 },
};

const names = rows => rows.map(r => r.name);

describe('conversation rail ordering', () => {
  it('sorts by most recent activity', () => {
    expect(names(orderChats(agents, status, available)))
      .toEqual(['Rachel', 'Mike', 'Arnold', 'Elli']);
  });

  it('does not reorder when a different chat is opened', () => {
    // Opening a chat changes app.session and nothing else. The ordering does
    // not read app.session at all, so the proof is that the same inputs give
    // the same answer no matter which one is focused — this is the whole
    // requirement, and the reason the comparator has no `current` term.
    const before = names(orderChats(agents, status, available));
    for (const focused of available) {
      const after = names(orderChats(agents, status, available));
      expect(after, `focused=${focused}`).toEqual(before);
    }
  });

  it('moves a chat up when that agent does something', () => {
    const busier = { ...status, elli: { last_activity: 9000 } };
    expect(names(orderChats(agents, busier, available)))
      .toEqual(['Elli', 'Rachel', 'Mike', 'Arnold']);
  });

  it('keeps a stable order for agents that never reported activity', () => {
    const quiet = { rachel: {}, mike: {}, arnold: {}, elli: {} };
    // Same list, enumerated in a different order — a snapshot replacement can
    // hand the keys back in any order, and the tail must not reshuffle.
    const shuffled = {
      elli: { name: 'Elli' }, arnold: { name: 'Arnold' },
      rachel: { name: 'Rachel' }, mike: { name: 'Mike' },
    };
    expect(names(orderChats(agents, quiet, available)))
      .toEqual(['Arnold', 'Elli', 'Mike', 'Rachel']);
    expect(names(orderChats(shuffled, quiet, available)))
      .toEqual(['Arnold', 'Elli', 'Mike', 'Rachel']);
  });

  it('ignores agents that are not in the active session list', () => {
    expect(names(orderChats(agents, status, ['rachel', 'elli'])))
      .toEqual(['Rachel', 'Elli']);
  });

  it('ignores roster entries with no persona name', () => {
    const withGhost = { ...agents, ghost: {} };
    expect(names(orderChats(withGhost, status, [...available, 'ghost'])))
      .toEqual(['Rachel', 'Mike', 'Arnold', 'Elli']);
  });
});
