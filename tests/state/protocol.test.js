import { describe, expect, it } from 'vitest';
import { ActivityStatus, AgentState, ClientAction, ClipProducerStatus, ClipStatus, SSEType, Timing } from '../../static/lib/protocol.js';

describe('protocol constants', () => {
  it('keeps wire event names in one tested module', () => {
    expect(SSEType.AGENT_STATE).toBe('agent-state');
    expect(SSEType.AGENT_ACTIVITY).toBe('agent-activity');
    expect(SSEType.AGENT_ROSTER).toBe('agent-roster');
    expect(SSEType.AGENT_FOCUS).toBe('agent-focus');
    expect(SSEType.USER_NOTIFICATION).toBe('user-notification');
    expect(SSEType.REMOTE_ACTION).toBe('remote-action');
    expect(SSEType.SERVER_VERSION).toBe('server-version');
  });

  it('exports shared state/mode/action sets', () => {
    expect(AgentState.BUSY).toEqual(new Set(['thinking', 'tool', 'compacting']));
    expect(ActivityStatus.VALID).toEqual(new Set(['running', 'ok', 'error', 'recorded']));
    expect(ClientAction.VALID).toEqual(new Set([
      'record', 'record-toggle', 'stop-agent', 'controller-event',
    ]));
    expect(ClipStatus.VALID).toEqual(new Set([
      'synthesized', 'broadcast', 'queued', 'play-start', 'play-ok', 'play-fail', 'held',
    ]));
    expect(ClipProducerStatus.VALID).toEqual(new Set(['streaming', 'complete', 'failed']));
  });

  it('names timing knobs instead of leaving unexplained numbers inline', () => {
    expect(Timing.SERVICE_WORKER_UPDATE_MS).toBe(5 * 60 * 1000);
    expect(Timing.CLIENT_LOG_FLUSH_MS).toBe(500);
    expect(Timing.SSE_STALE_MS).toBeGreaterThan(Timing.SSE_RECONNECT_BASE_MS);
    expect(Timing.CAPTURE_STOP_WATCHDOG_MS).toBeGreaterThan(Timing.MIN_UTTER_MS);
  });
});
