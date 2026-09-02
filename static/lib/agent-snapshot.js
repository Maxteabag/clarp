// Agent snapshot cache.
//
// The server sends a full `/agents/snapshot` response on demand and small
// `agent-state` SSE diffs as hooks write state_log rows. This store keeps the
// UI on one shape so initial load, reconnect fallback, and push updates all
// paint from the same source.

import { ActivityStatus, AgentState } from './protocol.js';

function toSeconds(value) {
  const n = Number(value || 0);
  if (!Number.isFinite(n) || n <= 0) return 0;
  return n > 9_999_999_999 ? Math.floor(n / 1000) : Math.floor(n);
}

function normalizeActivity(row) {
  if (!row || typeof row !== 'object') return null;
  const ts = toSeconds(row.ts || row.latest_state_ts);
  const summary = String(row.summary || row.activity_summary || '').trim();
  const action = String(row.action || row.activity_action || '').trim();
  const phase = String(row.phase || row.activity_phase || row.kind || '').trim();
  const status = ActivityStatus.VALID.has(row.status)
    ? row.status
    : (row.kind && AgentState.BUSY.has(row.kind) ? ActivityStatus.RUNNING : ActivityStatus.OK);
  return {
    type: row.type || 'agent-activity',
    agent_id: row.agent_id || '',
    session: row.session || '',
    persona: row.persona || row.name || '',
    kind: row.kind || '',
    phase,
    status,
    tool: row.tool || '',
    action,
    summary,
    file_path: row.file_path || '',
    ts,
  };
}

function isTimelineActivity(activity) {
  if (!activity) return false;
  if (!activity.summary && !activity.action && !activity.tool) return false;
  return !new Set([
    AgentState.THINKING,
    AgentState.IDLE,
    AgentState.DONE,
    AgentState.STOPPED,
    AgentState.SPAWNED,
  ]).has(activity.kind);
}

function normalizeAgent(row) {
  const sid = row.session || '';
  const activity = normalizeActivity(row.activity);
  return {
    agent_id: row.agent_id || '',
    name: row.persona || row.name || sid,
    persona: row.persona || row.name || sid,
    voice_id: row.voice_id || '',
    avatar_symbol: row.avatar_symbol || '',
    avatar_url: row.avatar_url || '',
    cwd: row.cwd || '',
    session: sid,
    backend: row.backend || '',
    model: row.model || '',
    effort: row.effort || '',
    mcp_servers: Array.isArray(row.mcp_servers) ? [...row.mcp_servers] : [],
    heartbeat_enabled: !!row.heartbeat_enabled,
    dreaming_enabled: !!row.dreaming_enabled,
    muted: !!row.muted,
    archived_at: toSeconds(row.archived_at),
    backend_session_id: row.backend_session_id || '',
    alive: row.alive !== false,
    busy: !!row.busy,
    focused: !!row.focused,
    last_activity: toSeconds(row.last_activity || row.latest_state_ts),
    last_turn_end: toSeconds(row.last_turn_end),
    turn_started_at: toSeconds(row.turn_started_at),
    latest_state: row.latest_state || row.kind || '',
    latest_state_ts: toSeconds(row.latest_state_ts || row.ts),
    status_text: String(row.status_text || '').trim(),
    last_message: String(row.last_message || '').trim(),
    conversation_id: row.conversation_id || '',
    head_revision: Number(row.head_revision) || 0,
    last_message_id: row.last_message_id || '',
    team_ids: Array.isArray(row.team_ids) ? [...row.team_ids] : [],
    context_tokens: Number(row.context_tokens) || 0,
    context_window: Number(row.context_window) || 0,
    compacting: !!row.compacting,
    queued_turn_count: Number(row.queued_turn_count) || 0,
    queued_turn_revision: Number(row.queued_turn_revision) || 0,
    queue_paused: !!row.queue_paused,
    activity,
    activity_summary: activity ? activity.summary : '',
    activity_action: activity ? activity.action : '',
    activity_phase: activity ? activity.phase : '',
    activity_status: activity ? activity.status : '',
    // Last seen tool name (from PostToolUse / TOOL state detail).
    last_tool: row.last_tool || (activity && activity.tool) || '',
    // Stream-json result fields, populated when an IDLE row carries them.
    last_tokens_in: Number(row.last_tokens_in) || 0,
    last_tokens_out: Number(row.last_tokens_out) || 0,
    last_cost_usd: Number(row.last_cost_usd) || 0,
    last_duration_ms: Number(row.last_duration_ms) || 0,
  };
}

export function createAgentSnapshotStore() {
  const bySession = new Map();
  const activityBySession = new Map();
  let focus = '';
  let roster = [];
  let personas = [];
  let availableMcpServers = [];

  function rememberActivity(activity) {
    if (!activity || !activity.session || !activity.ts) return;
    if (!isTimelineActivity(activity)) return;
    const sid = activity.session;
    const list = activityBySession.get(sid) || [];
    const key = `${activity.ts}:${activity.kind}:${activity.phase}:${activity.tool}:${activity.summary}`;
    if (!list.some(item => item._key === key)) {
      list.push({ ...activity, _key: key });
      list.sort((a, b) => a.ts - b.ts);
      activityBySession.set(sid, list.slice(-24));
    }
  }

  function replaceFromSnapshot(snapshot) {
    bySession.clear();
    focus = snapshot && snapshot.focus || '';
    roster = Array.isArray(snapshot && snapshot.roster) ? [...snapshot.roster] : roster;
    personas = Array.isArray(snapshot && snapshot.personas)
      ? snapshot.personas.map(row => ({ ...row })) : personas;
    availableMcpServers = Array.isArray(snapshot && snapshot.available_mcp_servers)
      ? [...snapshot.available_mcp_servers] : availableMcpServers;
    for (const row of (snapshot && snapshot.agents || [])) {
      const agent = normalizeAgent(row);
      if (agent.activity) rememberActivity(agent.activity);
      if (agent.session) bySession.set(agent.session, agent);
    }
    return asStatusMap();
  }

  function remove(session) {
    bySession.delete(session);
    activityBySession.delete(session);
  }

  function setFocus(session, agentId = '') {
    focus = agentId || focus;
    for (const [sid, row] of bySession.entries()) {
      bySession.set(sid, { ...row, focused: sid === session });
    }
  }

  function patchState(ev) {
    const sid = ev && ev.session || '';
    if (!sid) return asStatusMap();
    const current = bySession.get(sid) || normalizeAgent({ session: sid });
    const ts = toSeconds(ev.ts);
    const busy = AgentState.BUSY.has(ev.kind);
    // last_turn_end bumps when the agent reaches a turn-end state:
    // DONE (Stop hook), or any other turn-end kind after a busy state
    // (the reconciler and failure paths write IDLE).
    const isTurnEnd = AgentState.TURN_END.has(ev.kind);
    const wasBusy = !!current.busy;
    const bumpTurnEnd = ev.kind === AgentState.DONE
      || (isTurnEnd && wasBusy);
    // turn_started_at: server snapshot computes the canonical value,
    // but during live operation we mirror it on the first non-busy → busy
    // transition so the elapsed-time counter ticks from zero immediately
    // without waiting for the next /agents/snapshot poll.
    const becameBusy = busy && !wasBusy;
    const turnStart = becameBusy
      ? (ts || current.turn_started_at)
      : (bumpTurnEnd ? 0 : current.turn_started_at);
    const detail = (ev.detail && typeof ev.detail === 'object') ? ev.detail : {};
    const activity = normalizeActivity({
      type: 'agent-activity',
      agent_id: ev.agent_id || current.agent_id,
      session: sid,
      persona: ev.persona || current.persona,
      kind: ev.kind || current.latest_state,
      phase: detail.phase || ev.kind || '',
      status: detail.status || (busy ? ActivityStatus.RUNNING : ActivityStatus.OK),
      tool: detail.tool || '',
      action: detail.action || '',
      summary: detail.summary || detail.message || detail.title || '',
      file_path: detail.file_path || '',
      ts,
    });
    rememberActivity(activity);
    const waitingMessage = ev.kind === AgentState.WAITING
      ? (detail.message || detail.title || '')
      : (ev.kind && ev.kind !== current.latest_state ? '' : (current.waiting_message || ''));
    const compactingTrigger = ev.kind === AgentState.COMPACTING
      ? (detail.trigger || 'auto')
      : (ev.kind && ev.kind !== current.latest_state ? '' : (current.compacting_trigger || ''));
    // Capture the current tool name for the typing banner: state_log
    // rows of kind=TOOL carry {tool: 'Bash'} in their detail.
    const lastTool = ev.kind === AgentState.TOOL
      ? (detail.tool || current.last_tool || '')
      : (bumpTurnEnd ? '' : current.last_tool);
    // Stream-json result fields ride on the IDLE row written by
    // clarp's on_result callback — pick them up so the brief
    // post-turn banner can show tokens/cost like Claude Code's CLI.
    const hasResult = ev.kind === AgentState.IDLE
      && (detail.tokens_in != null
       || detail.tokens_out != null
       || detail.cost_usd != null
       || detail.duration_ms != null);
    bySession.set(sid, {
      ...current,
      agent_id: ev.agent_id || current.agent_id,
      persona: ev.persona || current.persona,
      name: ev.persona || current.name,
      busy,
      latest_state: ev.kind || current.latest_state,
      latest_state_ts: ts || current.latest_state_ts,
      activity: activity || current.activity,
      activity_summary: activity && activity.summary || current.activity_summary || '',
      activity_action: activity && activity.action || current.activity_action || '',
      activity_phase: activity && activity.phase || current.activity_phase || '',
      activity_status: activity && activity.status || current.activity_status || '',
      last_activity: Math.max(current.last_activity || 0, ts || 0),
      last_turn_end: bumpTurnEnd
        ? Math.max(current.last_turn_end || 0, ts || 0)
        : (current.last_turn_end || 0),
      turn_started_at: turnStart || 0,
      waiting_message: waitingMessage,
      compacting_trigger: compactingTrigger,
      last_tool: lastTool,
      last_tokens_in: hasResult ? Number(detail.tokens_in || 0) : current.last_tokens_in,
      last_tokens_out: hasResult ? Number(detail.tokens_out || 0) : current.last_tokens_out,
      last_cost_usd: hasResult ? Number(detail.cost_usd || 0) : current.last_cost_usd,
      last_duration_ms: hasResult ? Number(detail.duration_ms || 0) : current.last_duration_ms,
    });
    return asStatusMap();
  }

  function patchActivity(ev) {
    const activity = normalizeActivity(ev);
    if (!activity || !activity.session) return asStatusMap();
    rememberActivity(activity);
    const current = bySession.get(activity.session) || normalizeAgent({
      session: activity.session,
      agent_id: activity.agent_id,
      persona: activity.persona,
    });
    const busy = activity.status === ActivityStatus.RUNNING
      ? true
      : (activity.status === ActivityStatus.RECORDED ? !!current.busy : AgentState.BUSY.has(activity.kind));
    bySession.set(activity.session, {
      ...current,
      agent_id: activity.agent_id || current.agent_id,
      persona: activity.persona || current.persona,
      name: activity.persona || current.name,
      busy,
      latest_state: activity.kind || current.latest_state,
      latest_state_ts: activity.ts || current.latest_state_ts,
      last_activity: Math.max(current.last_activity || 0, activity.ts || 0),
      activity,
      activity_summary: activity.summary,
      activity_action: activity.action,
      activity_phase: activity.phase,
      activity_status: activity.status,
      last_tool: activity.tool || current.last_tool,
    });
    return asStatusMap();
  }

  function activityTimeline(session) {
    return (activityBySession.get(session) || []).map(({ _key, ...item }) => ({ ...item }));
  }

  function asStatusMap() {
    const out = {};
    for (const [sid, row] of bySession.entries()) {
      out[sid] = { ...row };
    }
    return out;
  }

  return {
    replaceFromSnapshot,
    patchState,
    patchActivity,
    activityTimeline,
    setFocus,
    remove,
    asStatusMap,
    get focus() { return focus; },
    get roster() { return [...roster]; },
    get personas() { return personas.map(row => ({ ...row })); },
    get availableMcpServers() { return [...availableMcpServers]; },
  };
}
