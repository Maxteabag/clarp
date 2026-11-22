import { describe, expect, it } from 'vitest';
import {
  movedHost, movedLine, movedRefusal, movedSendText, normalizeMoved,
} from '../../static/lib/agent-moved.js';
import { createAgentSnapshotStore } from '../../static/lib/agent-snapshot.js';

describe('an agent that moved to another Host (contract 125)', () => {
  it('keeps the snapshot row\'s moved field, or null', () => {
    const store = createAgentSnapshotStore();
    const status = store.replaceFromSnapshot({ agents: [
      { session: 'north', persona: 'North', moved: { to: { host: 'host-b', id: 'a1' }, at: 5 } },
      { session: 'south', persona: 'South', moved: null },
      { session: 'east', persona: 'East' },
    ] });
    expect(status.north.moved).toEqual({ to: { host: 'host-b', id: 'a1' }, at: 5 });
    expect(status.south.moved).toBeNull();
    expect(status.east.moved).toBeNull();
  });

  it('says where it went, or that it went to another Host', () => {
    expect(movedLine(null)).toBe('');
    expect(movedLine(normalizeMoved({ to: { host: 'host-b', id: 'a1' } }))).toBe('Moved to host-b');
    expect(movedLine(normalizeMoved({ to: { host: '', id: 'a1' } }))).toBe('Moved to another Host');
    expect(movedLine(normalizeMoved({ to: { host: 'x'.repeat(65) } }))).toBe('Moved to another Host');
    expect(movedHost(normalizeMoved({ to: { host: 'bad\nname' } }))).toBe('');
    expect(normalizeMoved({ at: 3 })).toBeNull();
  });

  it('turns a 409 agent_moved into the line the chat shows, and nothing else', () => {
    const body = { error: 'agent_moved', moved_to: { host: 'host-b', id: 'a1' }, moved_at: 5 };
    expect(movedRefusal(409, body, 'North')).toBe('North moved to host-b, so this was not sent.');
    expect(movedRefusal(409, { error: 'already_moved' }, 'North')).toBe('');
    expect(movedRefusal(503, body, 'North')).toBe('');
    expect(movedSendText('', null)).toBe('This agent moved to another Host, so this was not sent.');
  });
});
