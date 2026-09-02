import { describe, expect, it } from 'vitest';
import {
  buildAgentOverview, formatRelativeActivity,
} from '../../static/lib/agent-overview.js';

describe('agent overview view model', () => {
  it('keeps duplicate Contact names as separate session-addressed chats', () => {
    const result = buildAgentOverview({
      agentsBySession: {
        'nova-one': { agent_id: 'a1', name: 'Nova', last_activity: 10 },
        'nova-two': { agent_id: 'a2', name: 'Nova', last_activity: 20 },
      },
      personas: [{ id: 'p1', name: 'Nova' }],
      availableSessions: ['nova-one', 'nova-two'],
    });

    expect(result.chats.map(row => row.session)).toEqual(['nova-two', 'nova-one']);
    expect(result.contacts).toEqual([]);
  });

  it('separates active chats, archived chats, and unused Contacts', () => {
    const result = buildAgentOverview({
      agentsBySession: {
        mike: { agent_id: 'a1', name: 'Mike', busy: true, last_activity: 30 },
        old: { agent_id: 'a2', name: 'Rachel', archived_at: 100, last_activity: 20 },
      },
      personas: [
        { id: 'p1', name: 'Mike', builtin: true },
        { id: 'p2', name: 'Rachel', builtin: true },
        { id: 'p3', name: 'Nova', personality: 'Careful researcher.' },
      ],
      availableSessions: ['mike', 'old'],
      currentSession: 'mike',
    });

    expect(result.chats.map(row => row.name)).toEqual(['Mike']);
    expect(result.archived.map(row => row.name)).toEqual(['Rachel']);
    expect(result.contacts.map(row => row.name)).toEqual(['Nova']);
    expect(result.counts).toMatchObject({ chats: 1, working: 1, contacts: 1, archived: 1 });
  });

  it('searches operational and Contact context', () => {
    const input = {
      agentsBySession: {
        opus: { agent_id: 'a1', name: 'OPUS', model: 'gpt-5.6', cwd: '/repo/clarp' },
      },
      personas: [{ id: 'p2', name: 'Nova', personality: 'Travel planning' }],
      availableSessions: ['opus'],
    };

    expect(buildAgentOverview({ ...input, query: 'clarp' }).chats).toHaveLength(1);
    expect(buildAgentOverview({ ...input, query: 'travel' }).contacts).toHaveLength(1);
  });

  it('formats recent activity without pretending missing timestamps are recent', () => {
    expect(formatRelativeActivity(0, 1000)).toBe('No activity yet');
    expect(formatRelativeActivity(970, 1000)).toBe('Just now');
    expect(formatRelativeActivity(700, 1000)).toBe('5m ago');
  });
});
