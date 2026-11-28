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
// A tool call that failed or was cut short stays in its row. Commentary (text between tool calls)
// folds with the work, except commentary that was spoken (it held a <speak> block): it stays in
// view above the work row in every mode. The label counts tool calls only.
import { english, englishDuration } from './i18n-en.js';

export const TOOL_ACTIVITY = ['grouped', 'always', 'group_old', 'hide_done'];
/** Each mode's catalog key for t() (tool_activity.<mode>); the *_LABELS maps have the English. */
export const toolActivityKey = mode => (mode === 'follow' ? 'chat_choices.follow' : `tool_activity.${mode}`);
export const TOOL_ACTIVITY_LABELS = Object.fromEntries(TOOL_ACTIVITY.map(m => [m, english(toolActivityKey(m))]));

// One chat's own choice (kept on the device, keyed by the Host's server id and the session):
// 'follow' follows the device's Tool activity setting.
export const FOLLOW = 'follow';
export const CHAT_TOOL_ACTIVITY = [FOLLOW, ...TOOL_ACTIVITY];
export const CHAT_TOOL_ACTIVITY_LABELS = { [FOLLOW]: english(toolActivityKey(FOLLOW)), ...TOOL_ACTIVITY_LABELS };

/** The device key of one chat's choice: the Host's server id and the session ('' without either). */
export function chatKey(serverId, session) {
  return serverId && session ? `${serverId}|${session}` : '';
}

/** A chat's own Tool activity, or FOLLOW. */
export function chatToolChoice(byChat, key) {
  const mode = key && byChat && typeof byChat === 'object' ? byChat[key] : '';
  return TOOL_ACTIVITY.includes(mode) ? mode : FOLLOW;
}

/** The Tool activity a chat folds by: its own choice, else the device's setting. */
export function effectiveToolActivity(global, byChat, key) {
  const own = chatToolChoice(byChat, key);
  return own !== FOLLOW ? own : TOOL_ACTIVITY.includes(global) ? global : 'grouped';
}

const KEPT = new Set(['error', 'failed', 'killed', 'interrupted', 'cancelled']);

const SPEAK = /<speak\b/i;

function hasText(row) { return !!String(row.text || '').trim(); }

/**
 * Which of a turn's rows are commentary: text written between tool calls, not the reply. A row
 * says its phase; without one (an older row) the turn's last text is the reply and earlier text
 * is commentary. Only an ordinary agent row is: a note, a Host notice or a live row never is.
 */
function commentaryOf(members) {
  const answer = members.findLastIndex(hasText);
  return members.map((row, i) => hasText(row) && !row.kind && row.origin !== 'system'
    && (row.phase === 'commentary' || (row.phase !== 'final' && i !== answer)));
}

/** Commentary that held a <speak> block was spoken to the user: it stays in view. */
function spoken(row) { return SPEAK.test(String(row.text || '')); }

/**
 * A row's work that folds: its tool calls and cells (sub-agent cells and failed work stay in the
 * row) and its commentary text (`text`, '' when its words stay: a reply, or spoken commentary).
 */
function foldable(row, commentary) {
  const tools = (row.tools || []).filter(t => t && !KEPT.has(String(t.status || '')));
  const cells = (row.display_cells || []).filter(c => c && c.kind !== 'subagents'
    && !KEPT.has(String(c.status || '')));
  const text = commentary && !spoken(row) ? String(row.text) : '';
  return { tools, cells, text };
}

/** 'Worked for 11s · 3 tools', 'Worked for 11s' with none folded, '3 tools' without a worked
 * time, 'Working · 2 tools' running. `tr` and `duration` give the app's language. */
export function workLabel({ running = false, worked_ms = null, tool_count = 0 } = {},
  { tr = english, duration = englishDuration } = {}) {
  const tools = tr('live.fold.tools', { count: tool_count });
  if (running) return tr('history.working', { tools });
  if (worked_ms == null) return tools;
  const time = tr('live.fold.worked_for', { time: duration(worked_ms) });
  return tool_count ? tr('live.fold.with_tools', { time, tools }) : time;
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
    const commentary = commentaryOf(members);
    const calls = members.map((row, i) => ({ rowId: row.id, ...foldable(row, commentary[i]) }))
      .filter(c => c.tools.length || c.cells.length || c.text);
    // The label counts what the fold opens to: a failed call stays in its row, so the turn
    // summary's tool_count (every call) is not used.
    const counted = calls.reduce((n, c) => n + c.tools.length + c.cells.length, 0);
    const summary = members.findLast(m => m.turn)?.turn || null;
    const later = prompt < current;                   // a prompt came after this turn's
    const streaming = members.some(m => m.kind === 'live' || m.optimistic);
    const settled = !streaming && (!!summary || later || !busy);
    // A settled turn whose calls all failed (none folds) still says how long it worked
    // ('Worked for 11s'); with neither a worked time nor a folded call, or no call at all, no row.
    const failedOnly = settled && summary && summary.worked_ms != null && Number(summary.tool_count) > 0;
    if (!counted && !failedOnly) continue;
    const turnId = (prompt >= 0 && rows[prompt].trace_id) || members.find(m => m.trace_id)?.trace_id
      || members[0].id;
    // `work` is what the label says, for a view that words it in the app's language.
    const work = { running: !settled, worked_ms: summary ? summary.worked_ms ?? null : null, tool_count: counted };
    const fold = {
      key: `fold:${turnId}`, running: !settled,
      label: workLabel(work), work,
      calls,
    };
    // After the turn's last spoken commentary, which stays above it; else after its prompt.
    const said = members.findLast((row, i) => commentary[i] && spoken(row));
    const anchor = said ? said.id : prompt >= 0 ? rows[prompt].id : null;
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
