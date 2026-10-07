// How a live turn reads on screen (docs/live-items.md §7): rows built from
// the reducer's items, the settled-turn fold and the status line. Pure, so
// the timing rules (600 ms output delay, quick-command collapse, Host-clock
// timers) are tested without a browser.

/** A running command shows its output only after this long. */
export const TAIL_DELAY_MS = 600;
/** A successful command quicker than this collapses to one line. */
export const QUICK_COMMAND_MS = 1200;
export const RUNNING_TAIL_LINES = 5;
export const SETTLED_TAIL_LINES = 3;

const pad2 = n => String(n).padStart(2, '0');

/** `4s`, `1m 05s`, `1h 02m`: a settled duration. */
export function formatDuration(ms) {
  const s = Math.max(0, Math.floor((Number(ms) || 0) / 1000));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${pad2(s % 60)}s`;
  return `${Math.floor(m / 60)}h ${pad2(m % 60)}m`;
}

/** `0:12`, `1:02:03`: a ticking timer. */
export function clock(ms) {
  const s = Math.max(0, Math.floor((Number(ms) || 0) / 1000));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return h ? `${h}:${pad2(m)}:${pad2(s % 60)}` : `${m}:${pad2(s % 60)}`;
}

const plural = (n, one, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

const VERBS = {
  exec: ['Running', 'Ran'],
  read: ['Reading', 'Read'],
  list: ['Listing', 'Listed'],
  search: ['Searching', 'Searched'],
  edit: ['Editing', 'Edited'],
  write: ['Writing', 'Wrote'],
  fetch: ['Fetching', 'Fetched'],
  todo: ['Updating plan', 'Updated plan'],
  mcp: ['Calling', 'Called'],
  agent: ['Delegating', 'Delegated'],
  other: ['Calling', 'Called'],
};

const isRunning = item => item.status === 'running' || item.status === 'pending';

function elapsedMs(item, hostNow) {
  if (!item.started_at_ms) return 0;
  const end = item.ended_at_ms || (isRunning(item) ? hostNow : item.started_at_ms);
  return Math.max(0, end - item.started_at_ms);
}

function secondaryLine(tool, explanations) {
  const command = tool.command && tool.command !== tool.label ? String(tool.command) : '';
  if (explanations && tool.explain && tool.explain.status === 'ready' && tool.explain.text) {
    return String(tool.explain.text);
  }
  return command;
}

function outputTail(item, ms) {
  const out = item.tool && item.tool.output;
  if (!out || !Array.isArray(out.tail) || !out.tail.length) return { tail: [], more: 0 };
  let n;
  if (isRunning(item)) {
    if (ms < TAIL_DELAY_MS) return { tail: [], more: 0 };
    n = RUNNING_TAIL_LINES;
  } else {
    const clean = item.status === 'completed' && (out.exit_code === 0 || out.exit_code == null);
    if (clean && ms < QUICK_COMMAND_MS) return { tail: [], more: 0 };
    n = SETTLED_TAIL_LINES;
  }
  const tail = out.tail.slice(-n);
  return { tail, more: Math.max(0, (Number(out.total_lines) || out.tail.length) - tail.length) };
}

function toolRow(item, { explanations, hostNow }) {
  const tool = item.tool || {};
  const running = isRunning(item);
  const [live, done] = VERBS[tool.category] || VERBS.other;
  const ms = elapsedMs(item, hostNow);
  const exitCode = tool.output && tool.output.exit_code;
  const diff = tool.diff && (tool.diff.added || tool.diff.removed)
    ? `+${tool.diff.added || 0} −${tool.diff.removed || 0}` : '';
  return {
    type: 'tool',
    id: item.id,
    status: item.status,
    running,
    category: tool.category || 'other',
    name: tool.name || '',
    verb: running ? live : done,
    label: tool.label || tool.name || '',
    secondary: secondaryLine(tool, explanations),
    elapsed: item.started_at_ms ? formatDuration(ms) : '',
    exit: item.status === 'failed' && exitCode != null && exitCode !== 0 ? `exit ${exitCode}` : '',
    diff,
    diffPreview: (tool.diff && tool.diff.preview) || '',
    ...outputTail(item, ms),
    item,
  };
}

function reasoningRow(item, hostNow) {
  const running = isRunning(item);
  const title = item.title ? `: ${item.title}` : '';
  const label = running
    ? `Thinking${title}`
    : `Thought for ${formatDuration(elapsedMs(item, hostNow))}${title}`;
  return { type: 'reasoning', id: item.id, status: item.status, running, label,
    text: String(item.text || ''), item };
}

function exploreSummary(members) {
  const counts = { read: 0, search: 0, list: 0 };
  for (const m of members) counts[m.category] = (counts[m.category] || 0) + 1;
  const parts = [];
  if (counts.read) parts.push(plural(counts.read, 'file'));
  if (counts.search) parts.push(plural(counts.search, 'search', 'searches'));
  if (counts.list) parts.push(plural(counts.list, 'listing'));
  return parts.length ? `Explored ${parts.join(', ')}` : 'Explored';
}

function worstStatus(members) {
  if (members.some(m => m.running)) return 'running';
  if (members.some(m => m.status === 'failed')) return 'failed';
  if (members.some(m => m.status === 'interrupted')) return 'interrupted';
  return 'completed';
}

/**
 * The rows of a live turn, in ordinal order. Consecutive tools sharing a
 * `group` become one explore row whose id is the group id, so it keeps its
 * place (and its expand state) while it grows.
 */
export function liveRows(items, { explanations = true, now = Date.now(), skewMs = 0 } = {}) {
  const hostNow = now + skewMs;
  const rows = [];
  for (const item of items) {
    if (item.kind === 'tool') {
      const row = toolRow(item, { explanations, hostNow });
      const group = item.tool && item.tool.group;
      const last = rows[rows.length - 1];
      if (group && last && last.type === 'explore' && last.id === group) {
        last.members.push(row);
      } else if (group) {
        rows.push({ type: 'explore', id: group, members: [row] });
      } else {
        rows.push(row);
      }
    } else if (item.kind === 'reasoning') {
      rows.push(reasoningRow(item, hostNow));
    } else if (item.kind === 'message') {
      rows.push({ type: 'message', id: item.id, status: item.status, phase: item.phase || '',
        streaming: isRunning(item), text: String(item.text || ''), rowId: item.row_id || '', item });
    } else if (item.kind === 'plan' || item.kind === 'diff' || item.kind === 'compaction') {
      rows.push({ type: item.kind, id: item.id, status: item.status, running: isRunning(item), item });
    }
  }
  for (const row of rows) {
    if (row.type !== 'explore') continue;
    row.status = worstStatus(row.members);
    row.running = row.status === 'running';
    row.label = row.running ? 'Exploring' : exploreSummary(row.members);
  }
  return rows;
}

const SETTLED_TURN = new Set(['completed', 'failed', 'interrupted']);

/**
 * Fold a settled turn's work behind `Worked for …`: everything before the
 * final answer, except failed or interrupted work and messages, which stay
 * in view (text written before a tool call is a reply too).
 * Null while the turn runs.
 */
export function settledFold(turn, rows) {
  if (!turn || !SETTLED_TURN.has(turn.status)) return null;
  let answer = -1;
  for (let i = rows.length - 1; i >= 0; i--) {
    if (rows[i].type === 'message') { answer = i; break; }
  }
  const folded = [];
  const visible = [];
  rows.forEach((row, i) => {
    const keep = i >= answer || row.type === 'message'
      || row.status === 'failed' || row.status === 'interrupted';
    (keep ? visible : folded).push(row);
  });
  const worked = turn.worked_ms != null ? turn.worked_ms
    : (turn.ended_at_ms && turn.started_at_ms ? turn.ended_at_ms - turn.started_at_ms : 0);
  const tools = turn.tool_count != null ? turn.tool_count
    : rows.reduce((n, r) => n + (r.type === 'explore' ? r.members.length : r.type === 'tool' ? 1 : 0), 0);
  const lead = { interrupted: 'Stopped after', failed: 'Failed after' }[turn.status] || 'Worked for';
  const label = `${lead} ${formatDuration(worked)}${tools ? ` · ${plural(tools, 'tool')}` : ''}`;
  return { label, folded, visible, error: turnError(turn) };
}

/** Why a failed turn failed (`turn.error`, Host contract 54); older Hosts
 * send no reason, and the turn still must not read as one that answered. */
function turnError(turn) {
  if (turn.status !== 'failed') return null;
  const e = turn.error || {};
  return { message: String(e.message || 'Turn failed'), detail: String(e.detail || '') };
}

/**
 * A /log row the Host wrote itself (`origin: system`: a turn that failed, or
 * one a restart cut short) as a notice: the first paragraph says what
 * happened, the rest is the provider's own words. Null for anything else,
 * so replies, prompts and the dream digest render as they always have.
 */
export function systemNotice(row) {
  if (!row || row.role !== 'assistant' || row.origin !== 'system') return null;
  const text = String(row.text || '').trim();
  const cut = text.indexOf('\n\n');
  return cut < 0 ? { message: text, detail: '' }
    : { message: text.slice(0, cut).trim(), detail: text.slice(cut + 2).trim() };
}

const STATUS_TEXT = {
  thinking: 'Thinking',
  responding: 'Responding',
  tool: 'Working',
  compacting: 'Compacting',
  waiting: 'Needs your attention',
  background: 'Background work',
  limited: 'Waiting for the usage limit',
};
const INTERRUPTIBLE = new Set(['thinking', 'responding', 'tool', 'compacting']);

/**
 * The one status line: what is happening now, a timer from the Host clock
 * and whether it can be interrupted. Null when nothing runs.
 */
export function statusLine(activity, { now = Date.now(), skewMs = 0 } = {}) {
  const state = activity && activity.state;
  if (!state || !STATUS_TEXT[state]) return null;
  const hostNow = now + skewMs;
  const tool = state === 'tool' ? activity.tool : null;
  const since = (tool && tool.started_at_ms) || activity.turn_started_ms || activity.since_ms || 0;
  const running = Number(activity.running_tools) || 0;
  return {
    state,
    key: `${state}:${(tool && tool.item_id) || ''}`,
    text: activity.headline || STATUS_TEXT[state],
    since,
    time: since ? clock(hostNow - since) : '',
    more: tool && running > 1 ? `+${running - 1}` : '',
    itemId: activity.item_id || '',
    interrupt: INTERRUPTIBLE.has(state),
  };
}

/** The items of the turn the Host calls current, by ordinal. */
export function currentTurnItems(state) {
  const turnId = state && state.turn && state.turn.turn_id;
  if (!turnId) return [];
  return Object.values(state.items || {})
    .filter(item => item.turn_id === turnId)
    .sort((a, b) => (a.ordinal || 0) - (b.ordinal || 0));
}

function sameSet(a, b) {
  if (!a || a.size !== b.size) return false;
  for (const x of b) if (!a.has(x)) return false;
  return true;
}

/**
 * The /log rows the live turn stands in for (docs/live-items.md §7.5): the
 * row a message item names as `row_id`, rows whose every tool or display
 * cell is a live tool call, and any streaming `live` row. They are hidden
 * while the live turn is shown, so nothing renders twice and nothing is
 * deleted and re-inserted when the durable copy lands.
 */
export function takenOverTurns(turns, items, previous = null, liveTurn = null) {
  const hidden = new Set();
  if (!items || !items.length) return sameSet(previous, hidden) ? previous : hidden;
  // The live turn shows its own failure; the Host's failure row for it
  // would say the same thing twice.
  const failedTrace = liveTurn && liveTurn.status === 'failed' && liveTurn.error
    ? liveTurn.turn_id : null;
  const rowIds = new Set();
  const callIds = new Set();
  for (const item of items) {
    if (item.row_id) rowIds.add(item.row_id);
    if (item.tool && item.tool.call_id) callIds.add(item.tool.call_id);
  }
  for (const turn of turns || []) {
    if (rowIds.has(turn.id) || turn.kind === 'live') { hidden.add(turn.id); continue; }
    if (failedTrace && turn.trace_id === failedTrace && systemNotice(turn)) {
      hidden.add(turn.id);
      continue;
    }
    const parts = [...(turn.tools || []), ...(turn.display_cells || [])];
    const text = String(turn.text || '').trim();
    if (!text && parts.length && parts.every(p => p && callIds.has(p.id))) hidden.add(turn.id);
  }
  // The same rows as last time: keep the old set, so views keyed on it stay put.
  return sameSet(previous, hidden) ? previous : hidden;
}

const FALLBACK_STATES = new Set(['thinking', 'responding', 'tool', 'compacting']);
const BUSY_KINDS = new Set(['thinking', 'tool', 'compacting']);

/**
 * A status-line activity from an /agents/snapshot row, for Hosts without
 * the live channel: the same line, from what `agent-state` and
 * `agent-activity` already say. Newer Hosts put the status-line state, the
 * tool's start and the turn's start on `agent-activity`; older ones only
 * the agent state and the turn start in seconds.
 */
export function activityFromStatus(s) {
  if (!s || !(s.busy || BUSY_KINDS.has(s.latest_state))) return { state: 'idle' };
  const a = s.activity || {};
  const state = FALLBACK_STATES.has(a.state) ? a.state : s.latest_state;
  if (!FALLBACK_STATES.has(state)) return { state: 'idle' };
  const turnStarted = a.turn_started_ms || (s.turn_started_at ? s.turn_started_at * 1000 : null);
  const summary = String(s.activity_summary || a.summary || '').trim();
  const action = String(s.activity_action || a.action || '').trim();
  let headline = { compacting: 'Compacting', responding: 'Responding' }[state] || 'Thinking';
  if (state === 'tool' && summary) headline = action ? `${action} ${summary}` : summary;
  const tool = state === 'tool' && a.started_at_ms
    ? { started_at_ms: a.started_at_ms, item_id: a.call_id || '', label: summary }
    : null;
  return { state, headline, tool, running_tools: state === 'tool' ? 1 : 0,
    turn_started_ms: turnStarted, since_ms: turnStarted };
}

const CELL_STATUS = { ok: 'completed', error: 'failed', running: 'running', recorded: 'completed' };

/**
 * A /log display cell as a settled row, so history reads like the live
 * turn did. Provider sub-agent cells are not rendered here.
 */
export function cellRow(cell) {
  if (!cell || !cell.id || cell.kind === 'subagents') return null;
  return {
    type: 'cell',
    id: cell.id,
    kind: cell.kind || '',
    status: CELL_STATUS[cell.status] || 'completed',
    running: cell.status === 'running',
    verb: String(cell.title || ''),
    label: String(cell.summary || ''),
    lines: (cell.lines || []).filter(l => l && l.text).map(l => ({
      label: l.label || '', text: String(l.text), kind: l.kind || '' })),
  };
}

function rowTime(row) {
  const raw = row && (row.timestamp != null ? row.timestamp : row.ts);
  if (raw == null || raw === '') return null;
  const ms = typeof raw === 'number' ? raw : Date.parse(raw);
  return Number.isFinite(ms) ? ms : null;
}

/**
 * Where the live turn goes among the transcript's rows: after the rows from
 * before its turn started, and above anything newer (a prompt sent once the
 * turn settled must not end up under the last answer).
 */
export function liveInsertIndex(rows, turn) {
  const list = rows || [];
  const started = turn && Number(turn.started_at_ms);
  if (!started) return list.length;
  for (let i = 0; i < list.length; i++) {
    const t = rowTime(list[i]);
    if (t != null && t > started) return i;
  }
  return list.length;
}
