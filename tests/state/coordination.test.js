import { describe, expect, it } from 'vitest';
import {
  countAgents, countLine, hidesLiveTurn, logParams, needsReload, normalizeCount, normalizeEcho,
} from '../../static/lib/coordination.js';
import { Effects, applyLog, applySnapshot, blankSync, onEvent } from '../../static/lib/conversation-sync.js';
import { cacheRecord } from '../../static/lib/transcript-cache.js';

const HIDE = { hidden: true, source: 'global', visibility: 'hide' };
const run = (messages, extra = {}) => ({
  messages, exchanges: 1, agents: [{ agent_id: 'a-2', name: 'Mike' }], capped: false, ...extra,
});
const tail = (extra = {}) => ({
  conversation_id: 'c1', latest_revision: 4, has_more: false, missing: false,
  turns: [{ id: 'u1', role: 'user', text: 'synthetic', revision: 1 },
          { id: 'm2', role: 'assistant', text: 'synthetic', revision: 4, hidden_before: run(3) }],
  coordination: HIDE, ...extra,
});
const loaded = (extra) => applyLog(blankSync('s1'), tail(extra), 'tail').state;

describe('the count lines', () => {
  it('say how many messages between agents were left out', () => {
    expect(countLine(run(3))).toBe('3 messages between agents');
    expect(countLine(run(1))).toBe('1 message between agents');
    expect(countLine(run(100, { capped: true }))).toBe('100+ messages between agents');
    expect(countLine(run(100))).toBe('100 messages between agents');      // exactly 100 is not capped
    expect(countLine({ messages: 0, exchanges: 2, agents: [], capped: false })).toBe('2 exchanges between agents');
    expect(countLine({ messages: 0, exchanges: 0, agents: [], capped: false })).toBe('');
    expect(countLine(null)).toBe('');
    expect(countLine({ messages: 0, exchanges: 0, agents: [], capped: true })).toBe('Messages between agents');
    expect(countLine(run(40, { capped: true }))).toBe('40+ messages between agents');
  });

  it('name the agents of the run', () => {
    expect(countAgents(run(2))).toBe('with Mike');
    expect(countAgents(run(2, { agents: [{ agent_id: 'a', name: 'Mike' }, { agent_id: 'b', name: 'Rachel' },
      { agent_id: 'c', name: 'Ida' }] }))).toBe('with Mike, Rachel, and Ida');
    expect(countAgents(run(2, { agents: [{ agent_id: 'a', name: '' }] }))).toBe('');
    expect(normalizeCount({ messages: 'x', exchanges: 1, agents: 'no' }).agents).toEqual([]);
  });
});

describe('what /log is asked', () => {
  it('reads auto, sends the kept visibility on deltas, and shows a chat for one visit', () => {
    expect(logParams({ feature: false, echo: HIDE, delta: true })).toEqual({});
    expect(logParams({ feature: true })).toEqual({ coordination: 'auto' });
    expect(logParams({ feature: true, echo: HIDE })).toEqual({ coordination: 'auto' });   // a tail
    expect(logParams({ feature: true, echo: HIDE, delta: true }))
      .toEqual({ coordination: 'auto', coordination_read: 'hide' });
    expect(logParams({ feature: true, reveal: true, echo: { ...HIDE, source: 'request', visibility: 'show' }, delta: true }))
      .toEqual({ coordination: 'show', coordination_read: 'show' });
  });
});

describe('the sync reducer keeps the policy its rows were read under', () => {
  it('keeps the echo, the run after the newest row, and each row\'s run before it', () => {
    const state = loaded({ hidden_after: run(2) });
    expect(state.coordination).toEqual(HIDE);
    expect(state.hiddenAfter.messages).toBe(2);
    expect(state.turns.m2.hidden_before.messages).toBe(3);
  });

  it('takes a delta\'s hidden_after as the whole run after the newest row, or none', () => {
    let state = loaded({ hidden_after: run(2) });
    state = applyLog(state, { conversation_id: 'c1', turns: [], latest_revision: 6, coordination: HIDE,
                              hidden_after: run(5) }, 'delta').state;
    expect(state.hiddenAfter.messages).toBe(5);
    state = applyLog(state, { conversation_id: 'c1', latest_revision: 7, coordination: HIDE,
      turns: [{ id: 'm3', role: 'assistant', text: 'synthetic', revision: 7, hidden_before: run(5) }] }, 'delta').state;
    expect(state.hiddenAfter).toBe(null);
    expect(state.cursor).toBe(7);
  });

  it('reloads on replace_required (the setting changed while the copy was kept)', () => {
    const r = applyLog(loaded(), { conversation_id: 'c1', turns: [], latest_revision: 4, replace_required: true,
                                   coordination: { ...HIDE, visibility: 'show' } }, 'delta');
    expect(r.effects).toEqual([Effects.DROP_CACHE, Effects.FETCH_TAIL]);
  });

  it('reloads when the snapshot\'s visibility differs from what the rows were read under', () => {
    expect(applySnapshot(loaded(), { conversation_id: 'c1', head_revision: 4, coordination_visibility: 'hide' }).effects)
      .toEqual([]);
    expect(applySnapshot(loaded(), { conversation_id: 'c1', head_revision: 4, coordination_visibility: 'show' }).effects)
      .toEqual([Effects.DROP_CACHE, Effects.FETCH_TAIL]);
    const shown = loaded({ coordination: { hidden: false, source: 'request', visibility: 'show' } });
    expect(applySnapshot(shown, { conversation_id: 'c1', head_revision: 4, coordination_visibility: 'hide' }).effects)
      .toEqual([]);                                                   // shown for this visit: kept
    const old = applyLog(blankSync('s1'), tail({ coordination: undefined }), 'tail').state;
    expect(applySnapshot(old, { conversation_id: 'c1', head_revision: 4, coordination_visibility: 'show' }).effects)
      .toEqual([]);                                                   // a Host without the feature
  });

  it('reloads on coordination-changed for this chat, or for the Host when the chat follows it', () => {
    const changed = (extra) => ({ type: 'agent-roster', kind: 'coordination-changed', coordination_visibility: 'show', ...extra });
    expect(onEvent(loaded(), changed({ session: 's1' })).effects).toEqual([Effects.DROP_CACHE, Effects.FETCH_TAIL]);
    expect(onEvent(loaded(), changed({ session: 'other' })).effects).toEqual([]);
    expect(onEvent(loaded(), changed({})).effects).toEqual([Effects.DROP_CACHE, Effects.FETCH_TAIL]);
    const own = loaded({ coordination: { ...HIDE, source: 'chat' } });
    expect(onEvent(own, changed({})).effects).toEqual([]);           // its own setting stands
    expect(onEvent(loaded(), changed({ session: 's1', coordination_visibility: 'hide' })).effects).toEqual([]);
  });

  it('keeps the echo in the device\'s copy, which reads back as a tail', () => {
    const state = loaded({ hidden_after: run(2) });
    const rec = cacheRecord('s1', { conversationId: state.conversationId, cursor: state.cursor,
      turns: state.order.map(id => state.turns[id]), coordination: state.coordination, hiddenAfter: state.hiddenAfter });
    expect(rec.coordination).toEqual(HIDE);
    const back = applyLog(blankSync('s1'), rec, 'tail').state;
    expect(back.coordination).toEqual(HIDE);
    expect(back.hiddenAfter.messages).toBe(2);
    expect(logParams({ feature: true, echo: back.coordination, delta: true }).coordination_read).toBe('hide');
  });
});

describe('the live turn', () => {
  it('is not drawn under hide when another agent started it', () => {
    expect(hidesLiveTurn('hide', { audience: 'agents' })).toBe(true);
    expect(hidesLiveTurn('hide', { audience: 'user' })).toBe(false);
    expect(hidesLiveTurn('show', { audience: 'agents' })).toBe(false);
    expect(hidesLiveTurn('', { audience: 'agents' })).toBe(false);
  });

  it('reads no echo it cannot use', () => {
    expect(normalizeEcho({ hidden: true })).toBe(null);
    expect(needsReload(null, 'show')).toBe(false);
    expect(needsReload(HIDE, '')).toBe(false);
  });
});
