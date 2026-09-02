import { AgentState } from './protocol.js';

const TRANSIENT_STATES = new Set([
  AgentState.THINKING,
  AgentState.IDLE,
  AgentState.DONE,
  AgentState.STOPPED,
  AgentState.SPAWNED,
]);

export function isLoggableActivity(item, currentSession) {
  if (!item || item.session !== currentSession) return false;
  if (!item.summary && !item.action && !item.tool) return false;
  return !TRANSIENT_STATES.has(item.kind);
}
