import { describe, expect, it } from 'vitest';
import { foldTurns, workLabel } from '../../static/lib/history-fold.js';

const prompt = (id, trace) => ({ id, role: 'user', trace_id: trace, text: 'synthetic prompt' });
const tool = (id, status = 'ok') => ({ id, name: 'Bash', command: 'ls', status });
const row = (id, trace, extra = {}) => ({ id, role: 'assistant', trace_id: trace, text: 'synthetic', ...extra });

const twoTurns = () => [
  prompt('u1', 't1'),
  row('a1', 't1', { phase: 'commentary', tools: [tool('c1'), tool('c2')] }),
  row('a2', 't1', { phase: 'final', tools: [tool('c3')], turn: { worked_ms: 11000, tool_count: 3 } }),
  prompt('u2', 't2'),
  row('a3', 't2', { phase: 'final', tools: [tool('c4')], turn: { worked_ms: 2000, tool_count: 1 } }),
];

describe('the quiet chat: one work row per settled turn', () => {
  it('says what the turn did', () => {
    expect(workLabel({ worked_ms: 11000, tool_count: 3 })).toBe('Worked for 11s · 3 tools');
    expect(workLabel({ tool_count: 3 })).toBe('3 tools');
    expect(workLabel({ tool_count: 1 })).toBe('1 tool');
    expect(workLabel({ running: true, tool_count: 2 })).toBe('Working · 2 tools');
    expect(workLabel({ worked_ms: 11000, tool_count: 0 })).toBe('Worked for 11s');
  });

  it('folds every settled turn behind its summary, after its prompt (grouped)', () => {
    const out = foldTurns(twoTurns());
    expect(out.byRow.get('u1').label).toBe('Worked for 11s · 3 tools');
    expect(out.byRow.get('u2').label).toBe('Worked for 2s · 1 tool');
    expect([...out.stripped.keys()]).toEqual(['a1', 'a2', 'a3']);
    expect(out.byRow.get('u1').calls.map(c => c.tools.map(t => t.id))).toEqual([['c1', 'c2'], ['c3']]);
  });

  it('leaves every tool where it was (always visible)', () => {
    const out = foldTurns(twoTurns(), { mode: 'always' });
    expect(out.byRow.size).toBe(0);
    expect(out.stripped.size).toBe(0);
  });

  it('shows the newest turn\'s work and folds the older ones (group old)', () => {
    const out = foldTurns(twoTurns(), { mode: 'group_old' });
    expect([...out.byRow.keys()]).toEqual(['u1']);
    expect(out.stripped.has('a3')).toBe(false);
  });

  it('removes a settled turn\'s work and keeps a running one\'s work row (hide when done)', () => {
    const rows = [...twoTurns(), prompt('u3', 't3'), row('a4', 't3', { kind: 'live', tools: [tool('c5'), tool('c6')] })];
    const out = foldTurns(rows, { mode: 'hide_done', busy: true });
    expect([...out.byRow.keys()]).toEqual(['u3']);
    expect(out.byRow.get('u3').label).toBe('Working · 2 tools');
    expect(out.stripped.has('a1') && out.stripped.has('a3')).toBe(true);
  });

  it('never folds failed work, a turn with no tools, or a turn still running while grouped', () => {
    const rows = [prompt('u1', 't1'), row('a1', 't1', { tools: [tool('c1', 'error')], turn: { worked_ms: 1, tool_count: 1 } }),
      prompt('u2', 't2'), row('a2', 't2', { turn: { worked_ms: 1, tool_count: 0 } }),
      prompt('u3', 't3'), row('a3', 't3', { kind: 'live', tools: [tool('c3')] })];
    const out = foldTurns(rows, { busy: true });
    expect(out.stripped.has('a1')).toBe(false);                 // the failed call stays in its row
    expect(out.byRow.get('u1').label).toBe('Worked for 0s');    // only its worked time
    expect(out.byRow.get('u1').calls).toEqual([]);
    expect(out.byRow.has('u2')).toBe(false);                    // nothing to fold
    expect(out.byRow.get('u3').label).toBe('Working · 1 tool'); // running: its count so far
  });

  it('keys a fold by its turn, and finds a turn by its trace before its place', () => {
    const rows = [prompt('u1', 't1'), prompt('u2', 't2'),
      row('a1', 't1', { tools: [tool('c1')], turn: { worked_ms: 5000, tool_count: 1 } })];
    const out = foldTurns(rows);
    expect(out.byRow.get('u1').key).toBe('fold:t1');
  });

  it('counts only the calls the fold opens to, not failed ones left in their rows', () => {
    const rows = [prompt('u1', 't1'),
      row('a1', 't1', { tools: [tool('c1'), tool('c2', 'error')], display_cells: [{ id: 'd1', kind: 'command' }],
                        turn: { worked_ms: 4000, tool_count: 3 } })];
    const fold = foldTurns(rows).byRow.get('u1');
    expect(fold.label).toBe('Worked for 4s · 2 tools');
    expect(fold.calls[0].tools.map(t => t.id)).toEqual(['c1']);
  });

  it('folds a chat of thousands of rows in one walk', () => {
    const rows = [];
    for (let i = 0; i < 4000; i++) {
      rows.push(prompt(`u${i}`, `t${i}`), row(`a${i}`, `t${i}`, { tools: [tool(`c${i}`)], turn: { worked_ms: 1000, tool_count: 1 } }));
    }
    rows.push(row('late', 't7', { tools: [tool('cl')] }));      // a late row of an early turn
    const out = foldTurns(rows);
    expect(out.folds.length).toBe(4000);
    expect(out.byRow.get('u7').calls.map(c => c.rowId)).toEqual(['a7', 'late']);
    expect(out.byRow.get('u3999').running).toBe(false);
  });

  it('gives a settled turn with every call failed a row with its worked time, and none without one', () => {
    const failed = { tools: [tool('c1', 'error')] };
    const rows = [prompt('u1', 't1'), row('a1', 't1', { ...failed, turn: { worked_ms: 11000, tool_count: 1 } }),
      prompt('u2', 't2'), row('a2', 't2', { ...failed, turn: { tool_count: 1 } })];
    const out = foldTurns(rows);
    expect(out.byRow.get('u1').label).toBe('Worked for 11s');
    expect(out.byRow.has('u2')).toBe(false);
    expect(out.stripped.has('a1')).toBe(false);                 // the failed call stays in its row
  });
});
