// The quiet chat (docs/live-items.md §7.6, as the desktop's history_fold): each settled turn of
// the /log rows folds its tool work behind one work row, 'Worked for 11s · 3 tools', which opens
// in place. Built from the Host's turn summaries (log_turn_summary: every row of a turn carries
// its trace id, its last row `turn` {worked_ms, tool_count}), never from live items. The label
// counts the calls the fold opens to, not the summary's tool_count. Pure.
//
// Tool activity (a device setting):
//   grouped    every settled turn folds (the default)
//   always     nothing folds; every tool call shows where it was made
//   group_old  every settled turn but the newest folds
//   hide_done  a running turn shows its work row; a settled turn's work is not shown at all
// Never hidden: the user's messages, replies, decision cards, artifacts, errors, Host notices.
// A tool call that failed or was cut short stays in its row.
import { formatDuration } from './live-present.js';

export const TOOL_ACTIVITY = ['grouped', 'always', 'group_old', 'hide_done'];
export const TOOL_ACTIVITY_LABELS = {
  grouped: 'Grouped', always: 'Always visible', group_old: 'Group old', hide_done: 'Hide when done',
};

const KEPT = new Set(['error', 'failed', 'killed', 'interrupted', 'cancelled']);

function plural(n, word) { return `${n} ${word}${n === 1 ? '' : 's'}`; }

/** A row's tool calls and cells that fold (sub-agent cells and failed work stay in the row). */
function foldable(row) {
  const tools = (row.tools || []).filter(t => t && !KEPT.has(String(t.status || '')));
  const cells = (row.display_cells || []).filter(c => c && c.kind !== 'subagents'
    && !KEPT.has(String(c.status || '')));
  return { tools, cells };
}

/** 'Worked for 11s · 3 tools', '3 tools' without a worked time, 'Working · 2 tools' running. */
export function workLabel({ running = false, worked_ms = null, tool_count = 0 } = {}) {
  const tools = plural(tool_count, 'tool');
  if (running) return `Working · ${tools}`;
  return worked_ms != null ? `Worked for ${formatDuration(worked_ms)} · ${tools}` : tools;
}

/**
 * The folds of a chat's /log rows.
 * rows: the rows in display order ({id, role, trace_id, tools, display_cells, turn, kind,
 * optimistic}); busy: the agent works (its last turn may not be over).
 * Returns {byRow: Map<row id, fold placed after that row>, stripped: Map<row id, {tools, cells}>
 * (what to leave out of the row), folds: [...]}; each fold is {key, label, running, calls}
 * with calls [{rowId, tools, cells}] for the opened view, or calls null when its work is not
 * shown at all (hide_done).
 */
export function foldTurns(rows, { mode = 'grouped', busy = false } = {}) {
  const out = { byRow: new Map(), stripped: new Map(), folds: [] };
  if (mode === 'always' || !Array.isArray(rows) || !rows.length) return out;
  // Each agent row's turn: the last prompt so far with its trace id, else the last prompt above
  // it. One walk with a trace -> prompt map: a chat of thousands of rows folds in linear time.
  const promptOfTrace = new Map();
  const turns = new Map();         // prompt index -> its rows, in prompt order (-1: before any)
  let current = -1;
  rows.forEach((row, index) => {
    if (row.role === 'user') {
      if (row.trace_id) promptOfTrace.set(row.trace_id, index);
      current = index;
      turns.set(index, []);
      return;
    }
    if (row.role !== 'assistant') return;
    const traced = row.trace_id ? promptOfTrace.get(row.trace_id) : undefined;
    const owner = traced !== undefined ? traced : current;
    if (!turns.has(owner)) turns.set(owner, []);
    turns.get(owner).push(index);
  });
  const settledTurns = [];
  for (const [prompt, indexes] of turns) {
    const members = indexes.map(i => rows[i]);
    if (!members.length) continue;
    const calls = members.map(row => ({ rowId: row.id, ...foldable(row) }))
      .filter(c => c.tools.length || c.cells.length);
    // The label counts what the fold opens to: a failed call stays in its row, so the turn
    // summary's tool_count (every call) is not used.
    const counted = calls.reduce((n, c) => n + c.tools.length + c.cells.length, 0);
    if (!counted) continue;
    const summary = members.findLast(m => m.turn)?.turn || null;
    const later = prompt < current;                   // a prompt came after this turn's
    const streaming = members.some(m => m.kind === 'live' || m.optimistic);
    const settled = !streaming && (!!summary || later || !busy);
    const turnId = (prompt >= 0 && rows[prompt].trace_id) || members.find(m => m.trace_id)?.trace_id
      || members[0].id;
    const fold = {
      key: `fold:${turnId}`, running: !settled,
      label: workLabel({ running: !settled, worked_ms: summary ? summary.worked_ms ?? null : null,
                         tool_count: counted }),
      calls,
    };
    const anchor = prompt >= 0 ? rows[prompt].id : null;
    if (settled) settledTurns.push({ fold, anchor, calls, firstRow: members[0].id });
    else place(out, fold, anchor, calls, members[0].id);
  }
  const newest = settledTurns.length - 1;
  settledTurns.forEach(({ fold, anchor, calls, firstRow }, i) => {
    if (mode === 'group_old' && i === newest) return;          // the newest turn shows its work
    if (mode === 'hide_done') {
      for (const call of calls) out.stripped.set(call.rowId, call);
      return;
    }
    place(out, fold, anchor, calls, firstRow);
  });
  return out;
}

function place(out, fold, anchor, calls, firstRow) {
  for (const call of calls) out.stripped.set(call.rowId, call);
  out.folds.push(fold);
  // After the prompt that started the turn; a turn with no prompt above it, before its first row.
  out.byRow.set(anchor || `before:${firstRow}`, fold);
}
