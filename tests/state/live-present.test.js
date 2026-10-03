// What a live turn looks like on screen: the rows docs/live-items.md §7 asks
// for, built from the reducer's items. The end state of the recorded
// turn-full stream is the main input, so these read like the transcript a
// user would see after that turn.

import { describe, expect, it } from 'vitest';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  applyLiveEvent, applyLiveSnapshot, blankLive, liveItems,
} from '@core/live-items.js';
import {
  activityFromStatus, cellRow, clock, currentTurnItems, liveInsertIndex, formatDuration, liveRows, settledFold, statusLine, takenOverTurns,
} from '@core/live-present.js';

const here = path.dirname(fileURLToPath(import.meta.url));
const fixture = name => JSON.parse(fs.readFileSync(
  path.join(here, '..', '..', 'contract', 'live', `${name}.json`), 'utf8'));

/** Replay a fixture, stopping after the event with `untilLseq`. */
function replay(name, untilLseq = Infinity) {
  let live = blankLive();
  for (const step of fixture(name).steps) {
    if (step.snapshot) live = applyLiveSnapshot(live, step.snapshot, step.snapshot.server_now_ms);
    else {
      if (step.event.lseq > untilLseq) break;
      live = applyLiveEvent(live, step.event, step.event.server_now_ms).state;
    }
  }
  return live;
}

const T0 = 1759480000000;
const on = { explanations: true };
const off = { explanations: false };

describe('durations', () => {
  it('reads like a CLI footer', () => {
    expect(formatDuration(400)).toBe('0s');
    expect(formatDuration(4000)).toBe('4s');
    expect(formatDuration(72000)).toBe('1m 12s');
    expect(formatDuration(65000)).toBe('1m 05s');
    expect(formatDuration(3725000)).toBe('1h 02m');
  });

  it('ticks like a stopwatch on the status line', () => {
    expect(clock(12000)).toBe('0:12');
    expect(clock(151000)).toBe('2:31');
    expect(clock(3723000)).toBe('1:02:03');
    expect(clock(-5)).toBe('0:00');
  });
});

describe('thinking rows', () => {
  it('say what the model is thinking about while it thinks', () => {
    const live = replay('turn-full', 4);
    const [row] = liveRows(liveItems(live), { ...on, now: T0 + 1000 });
    expect(row).toMatchObject({ type: 'reasoning', id: 'cl:msg_01:0', running: true,
      label: 'Thinking: Finding the flaky test' });
  });

  it('say how long it took once done, and keep the text for expanding', () => {
    const live = replay('turn-full');
    const row = liveRows(liveItems(live), { ...on, now: T0 + 20000 })[0];
    expect(row).toMatchObject({ type: 'reasoning', running: false,
      label: 'Thought for 4s: Finding the flaky test' });
    expect(row.text).toContain('check the tokenizer');
  });

  it('fall back to a plain verb before a title arrives', () => {
    const live = replay('turn-full', 2);
    expect(liveRows(liveItems(live), { ...on, now: T0 + 300 })[0].label).toBe('Thinking');
  });
});

describe('explore groups', () => {
  it('show one Exploring row while any read or search runs', () => {
    const live = replay('turn-full', 10);
    const rows = liveRows(liveItems(live), { ...on, now: T0 + 4800 });
    const group = rows.filter(r => r.type === 'explore');
    expect(group).toHaveLength(1);
    expect(group[0]).toMatchObject({ id: 'explore:cl:toolu_01', running: true, label: 'Exploring' });
    expect(rows.some(r => r.type === 'tool' && r.id === 'cl:toolu_02')).toBe(false);
  });

  it('settle into a count of what was explored', () => {
    const live = replay('turn-full');
    const group = liveRows(liveItems(live), { ...on, now: T0 + 20000 }).find(r => r.type === 'explore');
    expect(group).toMatchObject({ running: false, label: 'Explored 1 file, 1 search' });
    expect(group.members.map(m => m.label)).toEqual(['src/parser.ts', 'tokenize']);
  });
});

describe('tool rows', () => {
  const bash = (live, opts) => liveRows(liveItems(live), opts).find(r => r.id === 'cl:toolu_03');

  it('name the command and tick while it runs', () => {
    const row = bash(replay('turn-full', 13), { ...on, now: T0 + 7000 });
    expect(row).toMatchObject({ type: 'tool', verb: 'Running', label: 'npm test',
      running: true, elapsed: '2s', status: 'running' });
  });

  it('show the explanation under the label when explanations are on', () => {
    const row = bash(replay('turn-full', 14), { ...on, now: T0 + 5100 });
    expect(row.secondary).toBe('Runs the parser tests');
  });

  it('show the raw command under the label when explanations are off', () => {
    const row = bash(replay('turn-full', 14), { ...off, now: T0 + 5100 });
    expect(row.secondary).toBe('npm test -- parser');
  });

  it('fill the secondary line with the command until the explanation lands', () => {
    const row = bash(replay('turn-full', 13), { ...on, now: T0 + 5100 });
    expect(row.secondary).toBe('npm test -- parser');
  });

  it('hold output back for the first 600 ms so fast commands never flash', () => {
    const live = replay('turn-full', 15);
    expect(bash(live, { ...on, now: T0 + 5500 }).tail).toEqual([]);
    const later = bash(live, { ...on, now: T0 + 5700 });
    expect(later.tail).toHaveLength(5);
    expect(later.more).toBe(25);
  });

  it('keep a short tail and a count of the rest once a command fails', () => {
    const row = bash(replay('turn-full'), { ...on, now: T0 + 20000 });
    expect(row).toMatchObject({ verb: 'Ran', status: 'failed', running: false,
      elapsed: '3s', exit: 'exit 1' });
    expect(row.tail).toEqual(['line 810', 'line 811', 'line 812']);
    expect(row.more).toBe(809);
  });

  it('drop the tail of a quick successful command', () => {
    const quick = {
      id: 'cx:q', kind: 'tool', status: 'completed', ordinal: 1,
      started_at_ms: T0, ended_at_ms: T0 + 300,
      tool: { name: 'Bash', category: 'exec', label: 'ls',
        output: { tail: ['a', 'b'], total_lines: 2, truncated: false, exit_code: 0 } },
    };
    expect(liveRows([quick], { ...on, now: T0 + 5000 })[0].tail).toEqual([]);
  });

  it('count added and removed lines on an edit', () => {
    const row = liveRows(liveItems(replay('turn-full')), { ...on, now: T0 + 20000 })
      .find(r => r.id === 'cl:toolu_04');
    expect(row).toMatchObject({ verb: 'Edited', label: 'src/tokenizer.ts', diff: '+3 −1' });
  });

  it('say a stopped command was interrupted', () => {
    const row = liveRows(liveItems(replay('turn-full')), { ...on, now: T0 + 20000 })
      .find(r => r.id === 'cl:toolu_05');
    expect(row).toMatchObject({ status: 'interrupted', running: false });
  });
});

describe('messages', () => {
  it('stream in place under their item id', () => {
    const live = replay('turn-full', 21);
    const msg = liveRows(liveItems(live), { ...on, now: T0 + 9100 }).find(r => r.id === 'cl:msg_02:0');
    expect(msg).toMatchObject({ type: 'message', streaming: true,
      text: 'The failure came from an off-by-one ' });
  });

  it('end an explore group', () => {
    const items = [
      { id: 'a', kind: 'tool', status: 'completed', ordinal: 1,
        tool: { category: 'read', label: 'a.ts', group: 'explore:a' } },
      { id: 'm', kind: 'message', status: 'completed', ordinal: 2, text: 'hm' },
      { id: 'b', kind: 'tool', status: 'completed', ordinal: 3,
        tool: { category: 'read', label: 'b.ts', group: 'explore:b' } },
    ];
    expect(liveRows(items, on).map(r => r.type)).toEqual(['explore', 'message', 'explore']);
  });
});

describe('the settled turn', () => {
  it('folds its work behind how long it took', () => {
    const live = replay('gap-recovers-from-snapshot');
    const work = [
      { id: 't1', kind: 'tool', status: 'completed', ordinal: 1, tool: { category: 'exec', label: 'make' } },
      { id: 'm1', kind: 'message', status: 'completed', ordinal: 2, phase: 'final', text: 'Done.' },
    ];
    const rows = liveRows(work, on);
    const fold = settledFold({ ...live.turn, tool_count: 1 }, rows);
    expect(fold.label).toBe('Worked for 0s · 1 tool');
    expect(fold.folded.map(r => r.id)).toEqual(['t1']);
    expect(fold.visible.map(r => r.id)).toEqual(['m1']);
  });

  it('keeps failed and interrupted work in view, and says the turn was stopped', () => {
    const live = replay('turn-full');
    const fold = settledFold(live.turn, liveRows(liveItems(live), { ...on, now: T0 + 20000 }));
    expect(fold.label).toBe('Stopped after 12s · 5 tools');
    expect(fold.visible.map(r => r.id)).toEqual(
      expect.arrayContaining(['cl:toolu_03', 'cl:toolu_05', 'cl:msg_02:0']));
    expect(fold.folded.map(r => r.id)).toEqual(
      ['cl:msg_01:0', 'cl:msg_01:1', 'explore:cl:toolu_01', 'cl:toolu_04']);
  });

  it('does not fold a running turn', () => {
    const live = replay('turn-full', 20);
    expect(settledFold(live.turn, liveRows(liveItems(live), on))).toBe(null);
  });
});

describe('status line', () => {
  it('names the running tool with its own timer and how many more run beside it', () => {
    const live = replay('turn-full', 10);
    expect(statusLine(live.activity, { now: T0 + 16750 })).toMatchObject({
      state: 'tool', text: 'Searching tokenize', time: '0:12', more: '+1', interrupt: true });
  });

  it('shows the thinking headline against the turn timer', () => {
    const live = replay('turn-full', 4);
    expect(statusLine(live.activity, { now: T0 + 41000 })).toMatchObject({
      state: 'thinking', text: 'Thinking: Finding the flaky test', time: '0:41', more: '' });
  });

  it('corrects the timer for the Host clock', () => {
    const live = replay('turn-full', 13);
    expect(statusLine(live.activity, { now: T0 + 5000 + 12000 - 3000, skewMs: 3000 }).time).toBe('0:12');
  });

  it('is gone when nothing runs', () => {
    expect(statusLine(replay('turn-full').activity, { now: T0 })).toBe(null);
    expect(statusLine({ state: 'idle' }, { now: T0 })).toBe(null);
  });
});

describe('durable rows and the live turn', () => {
  const items = [
    { id: 'cl:m:0', kind: 'message', turn_id: 'tr-2', ordinal: 1, row_id: 'live-abc', text: 'Hi' },
    { id: 'cl:toolu_9', kind: 'tool', turn_id: 'tr-2', ordinal: 2, tool: { call_id: 'toolu_9' } },
    { id: 'cl:old', kind: 'message', turn_id: 'tr-1', ordinal: 0, row_id: 'msg-old', text: 'Old' },
  ];
  const state = { turn: { turn_id: 'tr-2', status: 'running' },
    items: Object.fromEntries(items.map(i => [i.id, i])) };

  it('shows only the items of the turn the Host calls current', () => {
    expect(currentTurnItems(state).map(i => i.id)).toEqual(['cl:m:0', 'cl:toolu_9']);
    expect(currentTurnItems({ turn: null, items: state.items })).toEqual([]);
  });

  it('lets the live turn stand in for the rows that carry the same message or tool', () => {
    const turns = [
      { id: 'u-1', role: 'user', text: 'Go' },
      { id: 'live-abc', role: 'assistant', kind: 'live', text: 'H' },
      { id: 'msg-tools', role: 'assistant', text: '', tools: [{ id: 'toolu_9', name: 'Bash' }] },
      { id: 'msg-old', role: 'assistant', text: 'Old' },
      { id: 'live-other', role: 'assistant', kind: 'live', text: 'stale live row' },
    ];
    const hidden = takenOverTurns(turns, currentTurnItems(state));
    expect([...hidden].sort()).toEqual(['live-abc', 'live-other', 'msg-tools']);
  });

  it('keeps a durable row that carries more than the live turn shows', () => {
    const turns = [{ id: 'm', role: 'assistant', text: 'Words',
      tools: [{ id: 'toolu_9' }, { id: 'toolu_other' }] }];
    expect(takenOverTurns(turns, currentTurnItems(state)).size).toBe(0);
  });

  it('takes nothing over without live items', () => {
    expect(takenOverTurns([{ id: 'live-abc', kind: 'live' }], []).size).toBe(0);
  });
});

describe('status line without the live channel', () => {
  it('names the tool from the agent row and ticks from the turn start', () => {
    const activity = activityFromStatus({ latest_state: 'tool', busy: true,
      activity_summary: 'npm test', activity_action: 'Running', turn_started_at: 1759480000 });
    expect(statusLine(activity, { now: T0 + 12000 })).toMatchObject({
      state: 'tool', text: 'Running npm test', time: '0:12', interrupt: true });
  });

  it('says thinking while the agent works without a tool', () => {
    const activity = activityFromStatus({ latest_state: 'thinking', turn_started_at: 1759480000 });
    expect(statusLine(activity, { now: T0 + 3000 })).toMatchObject({ text: 'Thinking', time: '0:03' });
  });

  it('is gone for an idle agent', () => {
    expect(statusLine(activityFromStatus({ latest_state: 'done' }), { now: T0 })).toBe(null);
    expect(statusLine(activityFromStatus(undefined), { now: T0 })).toBe(null);
  });
});

describe('display cells in history', () => {
  it('read like settled live rows', () => {
    const row = cellRow({ id: 'call_1', kind: 'command', title: 'Ran', summary: 'npm test', status: 'error',
      lines: [{ label: 'Command', text: 'npm test -- parser', kind: 'detail' },
        { text: 'FAIL a', kind: 'output' }, { text: 'FAIL b', kind: 'output' }] });
    expect(row).toMatchObject({ type: 'cell', id: 'call_1', status: 'failed', verb: 'Ran', label: 'npm test' });
    expect(row.lines.map(l => l.text)).toEqual(['npm test -- parser', 'FAIL a', 'FAIL b']);
  });

  it('map the cell statuses onto the live ones', () => {
    expect(cellRow({ id: 'a', status: 'ok', title: 'Explored' }).status).toBe('completed');
    expect(cellRow({ id: 'a', status: 'running', title: 'Running' }).status).toBe('running');
    expect(cellRow({ id: 'a', status: 'recorded', title: 'Read' }).status).toBe('completed');
  });

  it('leave provider sub-agent cells out', () => {
    expect(cellRow({ id: 'a', kind: 'subagents', title: 'Spawned agent' })).toBe(null);
  });
});

describe('status line from agent-activity on Hosts without live items', () => {
  const row = activity => ({ latest_state: 'tool', busy: true, turn_started_at: 1759480000, activity });

  it('times the tool from its own start', () => {
    const a = activityFromStatus(row({ state: 'tool', action: 'Running', summary: 'npm test',
      started_at_ms: T0 + 30000, turn_started_ms: T0 }));
    expect(statusLine(a, { now: T0 + 42000 })).toMatchObject({ text: 'Running npm test', time: '0:12' });
  });

  it('says responding while text streams', () => {
    const a = activityFromStatus({ latest_state: 'thinking', busy: true,
      activity: { state: 'responding', turn_started_ms: T0 } });
    expect(statusLine(a, { now: T0 + 5000 })).toMatchObject({ state: 'responding', text: 'Responding', time: '0:05' });
  });

  it('goes back to thinking once a tool finishes', () => {
    const a = activityFromStatus(row({ state: 'thinking', phase: 'tool_finished', summary: 'npm test',
      turn_started_ms: T0 }));
    expect(statusLine(a, { now: T0 + 9000 })).toMatchObject({ state: 'thinking', text: 'Thinking', time: '0:09' });
  });
});

describe('where the live turn sits in the transcript', () => {
  const at = iso => new Date(iso).getTime();
  const turns = [
    { id: 'u1', role: 'user', timestamp: '2026-10-03T10:00:00Z' },
    { id: 'a1', role: 'assistant', timestamp: '2026-10-03T10:00:05Z' },
    { id: 'u2', role: 'user', timestamp: '2026-10-03T10:01:00Z' },
  ];

  it('goes after the rows from before its turn started', () => {
    expect(liveInsertIndex(turns, { started_at_ms: at('2026-10-03T10:00:01Z') })).toBe(1);
  });

  it('stays above a message sent after its turn, so a new prompt is never under the last answer', () => {
    expect(liveInsertIndex(turns, { started_at_ms: at('2026-10-03T10:00:30Z') })).toBe(2);
  });

  it('goes last when its turn is the newest thing', () => {
    expect(liveInsertIndex(turns, { started_at_ms: at('2026-10-03T10:02:00Z') })).toBe(3);
    expect(liveInsertIndex(turns, null)).toBe(3);
  });
});
