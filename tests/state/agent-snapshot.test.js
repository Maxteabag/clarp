import { describe, expect, it } from 'vitest';
import { createAgentSnapshotStore } from '../../static/lib/agent-snapshot.js';

describe('agent snapshot store', () => {
  it('normalizes full snapshot rows into the status map shape', () => {
    const store = createAgentSnapshotStore();
    const status = store.replaceFromSnapshot({
      focus: 'a1',
      agents: [{
        agent_id: 'a1',
        persona: 'Mike',
        voice_id: 'V1',
        avatar_symbol: 'sparkles',
        avatar_url: '/avatars/a1?v=portrait-1',
        backend: 'codex',
        model: 'gpt-5.6',
        effort: 'high',
        mcp_servers: ['github'],
        heartbeat_enabled: true,
        dreaming_enabled: true,
        muted: true,
        last_message: 'Latest reply',
        status_text: 'Waiting for review',
        context_tokens: 120000,
        context_window: 1000000,
        queued_turn_count: 2,
        cwd: '/tmp',
        session: 'claude',
        alive: true,
        busy: true,
        last_activity: 1_770_000_000_123,
        latest_state: 'thinking',
        activity: {
          kind: 'thinking',
          action: 'thinking',
          summary: 'Thinking',
          status: 'running',
          ts: 1_770_000_000_123,
        },
      }],
    });

    expect(status.claude.name).toBe('Mike');
    expect(status.claude.avatar_symbol).toBe('sparkles');
    expect(status.claude.avatar_url).toBe('/avatars/a1?v=portrait-1');
    expect(status.claude.backend).toBe('codex');
    expect(status.claude.model).toBe('gpt-5.6');
    expect(status.claude.mcp_servers).toEqual(['github']);
    expect(status.claude.heartbeat_enabled).toBe(true);
    expect(status.claude.dreaming_enabled).toBe(true);
    expect(status.claude.muted).toBe(true);
    expect(status.claude.last_message).toBe('Latest reply');
    expect(status.claude.status_text).toBe('Waiting for review');
    expect(status.claude.context_tokens).toBe(120000);
    expect(status.claude.queued_turn_count).toBe(2);
    expect(status.claude.busy).toBe(true);
    expect(status.claude.last_activity).toBe(1_770_000_000);
    expect(status.claude.activity_summary).toBe('Thinking');
    expect(store.roster).toEqual([]);
  });

  it('keeps server-provided roster order', () => {
    const store = createAgentSnapshotStore();
    store.replaceFromSnapshot({ roster: ['Rachel', 'Mike'], agents: [] });

    expect(store.roster).toEqual(['Rachel', 'Mike']);
  });

  it('retains Contact definitions and the Computer MCP catalog', () => {
    const store = createAgentSnapshotStore();
    store.replaceFromSnapshot({
      agents: [],
      personas: [{ id: 'p1', name: 'Nova', builtin: false }],
      available_mcp_servers: ['github', 'playwright'],
    });

    expect(store.personas).toEqual([{ id: 'p1', name: 'Nova', builtin: false }]);
    expect(store.availableMcpServers).toEqual(['github', 'playwright']);
  });

  it('patches hook-driven agent-state events without a poll', () => {
    const store = createAgentSnapshotStore();
    store.replaceFromSnapshot({ agents: [
      { agent_id: 'a1', persona: 'Rachel', session: 'rachel', busy: false },
    ] });

    const status = store.patchState({
      type: 'agent-state',
      agent_id: 'a1',
      session: 'rachel',
      persona: 'Rachel',
      kind: 'tool',
      ts: 1_770_000_100_000,
    });

    expect(status.rachel.busy).toBe(true);
    expect(status.rachel.latest_state).toBe('tool');
    expect(status.rachel.last_activity).toBe(1_770_000_100);
  });

  it('patches readable activity events and keeps a timeline', () => {
    const store = createAgentSnapshotStore();
    const status = store.patchActivity({
      type: 'agent-activity',
      agent_id: 'a1',
      session: 'rachel',
      persona: 'Rachel',
      kind: 'tool',
      phase: 'tool_started',
      status: 'running',
      tool: 'Edit',
      action: 'editing file',
      summary: 'static/app.js',
      ts: 1_770_000_200_000,
    });

    expect(status.rachel.busy).toBe(true);
    expect(status.rachel.activity_summary).toBe('static/app.js');
    expect(status.rachel.activity_action).toBe('editing file');
    expect(store.activityTimeline('rachel')).toHaveLength(1);
  });

  it('does not turn historical transcript activity into live busy state', () => {
    const store = createAgentSnapshotStore();
    const status = store.patchActivity({
      type: 'agent-activity',
      session: 'rachel',
      kind: 'tool',
      status: 'recorded',
      tool: 'Read',
      action: 'reading file',
      summary: 'README.md',
      ts: 1_770_000_300,
    });

    expect(status.rachel.busy).toBe(false);
    expect(status.rachel.activity_summary).toBe('README.md');
  });

  it('keeps generic lifecycle states out of the activity timeline', () => {
    const store = createAgentSnapshotStore();
    store.patchActivity({
      type: 'agent-activity',
      session: 'rachel',
      kind: 'thinking',
      status: 'running',
      action: 'thinking',
      summary: 'Thinking',
      ts: 1_770_000_400,
    });
    store.patchActivity({
      type: 'agent-activity',
      session: 'rachel',
      kind: 'idle',
      status: 'ok',
      action: 'idle',
      summary: 'Idle',
      ts: 1_770_000_401,
    });
    store.patchActivity({
      type: 'agent-activity',
      session: 'rachel',
      kind: 'done',
      status: 'ok',
      action: 'done',
      summary: 'Done',
      ts: 1_770_000_402,
    });

    expect(store.activityTimeline('rachel')).toEqual([]);
  });

  it('removes deleted roster entries and tracks focus', () => {
    const store = createAgentSnapshotStore();
    store.replaceFromSnapshot({ agents: [
      { session: 'claude', persona: 'Mike', voice_id: 'V1' },
      { session: 'rachel', persona: 'Rachel', voice_id: 'V2' },
    ] });

    store.setFocus('rachel', 'a2');
    expect(store.asStatusMap().rachel.focused).toBe(true);
    expect(store.asStatusMap().claude.focused).toBe(false);

    store.remove('rachel');
    expect(store.asStatusMap().rachel).toBeUndefined();
  });
});
