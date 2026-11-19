"""A prompt's copy in the transcript is never linked to a client row sent for another turn (R89).

An automation sends the same words every 20 minutes. Claude's transcript holds a copy of each
prompt, linked back to its client row by its words. A copy that has no row of its own (a prompt
that reached the transcript some other way) took the next turn's row, that turn took the one after,
and the last an old failed send's: every later reply carried another prompt's trace, the live row
of its own turn was not taken over, and the reply was shown twice."""
import json
import time

import pytest

from lib import agents, clock, db, message_import_rows, message_store, message_turns, transcript_incremental, \
    turn_lifecycle

BASE = 1_795_000_000_000
STEP = 20 * 60 * 1000
PROMPT = "Watch the August feeds and say what changed since the last look, in one short paragraph only."
ANSWER = "Nothing changed in the August feeds since the last look; every source is quiet and no new item arrived."


class _Automation:
    """Eight turns with the same words: a tool call, its result, then the text, streamed live first.
    Each turn has two turn rows of one trace a second apart, as the dispatcher and the prompt hook
    leave them."""

    def __init__(self, tmp_path, monkeypatch, *, unsent=(), old_sends=0, turns=8, old_settled=True):
        self.now = [BASE]
        monkeypatch.setattr(time, 'time', lambda: self.now[0] / 1000)
        self.id = agents.create_agent(persona='August', session='august', voice_id='v', cwd=str(tmp_path),
                                      backend='claude')
        agents.bind_backend_session(self.id, 'native')
        self.path = tmp_path / 'native.jsonl'
        self.path.write_text('')
        self.right = {}
        for k in range(old_sends):        # sends of days ago with these words that never reached this transcript
            self.now[0] = BASE - 10 * 86_400_000 + k * STEP
            agents.record_user_message(agent_id=self.id, backend_session_id='native', client_msg_id=f"old{k}",
                                       text=PROMPT, trace_id=f"old-{k}", origin="automation")
            agents.open_turn(agent_id=self.id, source='pwa', trace_id=f"old-{k}")
            if old_settled:
                with db.conn() as con:
                    con.execute("UPDATE turns SET settled_at=?, outcome='failed' WHERE trace_id=?",
                                (self.now[0] + 1000, f"old-{k}"))
                db.close_local()
        for k in range(turns):
            self._turn(k, sent=k not in unsent)

    def _line(self, role, content, at, message_id=None):
        message = {'role': role, 'model': 'synthetic-fixture', 'content': content}
        if message_id:
            message['id'] = message_id
        with self.path.open('a') as stream:
            stream.write(json.dumps({'type': role, 'uuid': f'{role}-{at}', 'timestamp': clock.iso_from_ms(at),
                                     'message': message}) + '\n')

    def _turn(self, k, *, sent):
        t0, trace = BASE + k * STEP, f"turn-{k}"
        self.now[0] = t0
        if sent:
            agents.record_user_message(agent_id=self.id, backend_session_id='native', client_msg_id=f"c{k}",
                                       text=PROMPT, trace_id=trace, origin="automation")
        self.now[0] = t0 + 600
        agents.open_turn(agent_id=self.id, source='pwa', trace_id=trace)
        turn_lifecycle.transition(self.id, turn_lifecycle.TurnEvent.PROMPT_ADMITTED,
                                  {'trace_id': trace, 'origin': 'automation'})
        self.now[0] = t0 + 1700
        agents.open_turn(agent_id=self.id, source='pwa', trace_id=trace)
        self._line('user', PROMPT, t0 + 2000)
        self._line('assistant', [{'type': 'tool_use', 'id': f'toolu_{k}', 'name': 'Bash',
                                  'input': {'command': 'cat feeds'}}], t0 + 8000, f'msg_tool_{k}')
        self._line('user', [{'type': 'tool_result', 'tool_use_id': f'toolu_{k}', 'content': 'quiet'}], t0 + 9000)
        self.now[0] = t0 + 12_000
        for text in (ANSWER[:30], ANSWER):
            message_store.upsert_live_assistant_message(agent_id=self.id, backend_session_id='native',
                                                        trace_id=trace, item_key=f'msg_text_{k}', text=text)
        self._line('assistant', [{'type': 'text', 'text': ANSWER}], t0 + 13_000, f'msg_text_{k}')
        self.right[clock.iso_from_ms(t0 + 8000)] = self.right[clock.iso_from_ms(t0 + 13_000)] = trace
        self.now[0] = t0 + 15_000
        turn_lifecycle.transition(self.id, turn_lifecycle.TurnEvent.PROCESS_EXITED_OK, {'trace_id': trace})
        self.now[0] = t0 + 20_000
        self.imported()

    def imported(self):
        self.now[0] += 3
        transcript_incremental.import_transcript(backend='claude', path=self.path, agent_id=self.id,
                                                 backend_session_id='native')
        db.close_local()

    def replies(self):
        """(transcript time, the trace it carries, the right one) of every imported reply; the live rows left."""
        rows = db.conn().execute(
            "SELECT source_file, trace_id, timestamp FROM messages WHERE agent_id=? AND role='assistant'"
            " ORDER BY seq", (self.id,)).fetchall()
        db.close_local()
        imported = [(row["timestamp"], row["trace_id"], self.right.get(row["timestamp"])) for row in rows
                    if not str(row["source_file"]).startswith("live:")]
        left = [row["trace_id"] for row in rows if str(row["source_file"]).startswith("live:")]
        return imported, left


@pytest.mark.parametrize("old_sends, old_settled", [(0, True), (3, True), (3, False)],
                         ids=["no old sends", "three old failed sends", "three old sends never settled"])
@pytest.mark.parametrize("unsent", [(), (2,), (0, 5)], ids=["every prompt sent", "one copy unsent", "two unsent"])
def test_every_reply_carries_its_own_prompts_trace_and_none_is_shown_twice(
        tmp_path, monkeypatch, unsent, old_sends, old_settled):
    world = _Automation(tmp_path, monkeypatch, unsent=unsent, old_sends=old_sends, old_settled=old_settled)
    for _ in range(2):
        world.now[0] += STEP
        world.imported()
        imported, left = world.replies()
        assert len(imported) == 16
        assert [(at, carried) for at, carried, right in imported if carried != right] == []
        assert left == []


def test_replies_given_another_prompts_trace_carry_their_own_after_the_next_import(tmp_path, monkeypatch):
    monkeypatch.setattr(message_turns.TurnIndex, "ran_at", lambda self, trace, timestamp: None)   # as before
    world = _Automation(tmp_path, monkeypatch, unsent=(2,), old_sends=3)
    imported, left = world.replies()
    assert sum(carried != right for _at, carried, right in imported) == 12 and left == ["turn-2"]
    monkeypatch.undo()
    world.now[0] += STEP
    world.imported()
    imported, left = world.replies()
    assert [(at, carried) for at, carried, right in imported if carried != right] == [] and left == []


def _index(*turns):
    return message_turns.TurnIndex.from_rows(
        [{"trace_id": trace, "started_at": BASE + started, "settled_at": None if settled is None else BASE + settled,
          "outcome": "completed"} for trace, started, settled in sorted(turns, key=lambda turn: turn[1])])


def test_a_turn_runs_from_two_seconds_before_it_began_until_it_settled_or_the_next_began():
    index = _index(("t", 10_000, 20_000), ("t", 11_000, 21_000), ("never", 30_000, None),
                   ("never", 31_000, 32_000), ("late", 40_000, 60_000), ("next", 50_000, None))
    assert [index.ran_at("t", BASE + at) for at in (7_999, 8_000, 21_000, 21_001)] == [False, True, True, False]
    # One row never settled: the turn ended when the next turn began, not never.
    assert [index.ran_at("never", BASE + at) for at in (32_000, 40_000, 40_001)] == [True, True, False]
    # Settled after the next turn began: it ended at that start.
    assert [index.ran_at("late", BASE + at) for at in (50_000, 50_001)] == [True, False]
    assert index.ran_at("next", BASE + 10**9) is True                  # the latest still runs
    assert index.ran_at("steered", BASE + 15_000) is None


def _rows(*sent):
    return [(f"c{n}", "automation", "", clock.iso_from_ms(BASE + at)) for n, (_trace, at) in enumerate(sent)]


@pytest.mark.parametrize("sent, chosen", [
    ((("turn-3", 60_000), ("turn-4", 80_000)), None),                  # both sent for later turns
    ((("old-0", -900_000), ("turn-4", 80_000)), None),                 # an old failed send, a later turn's
    ((("steered", 41_000), ("turn-4", 80_000)), 0),                    # steered into this turn: no turn of its own
    ((("turn-4", 80_000), ("turn-2", 39_000)), 1),                     # its own row, wherever it stands
    ((("racing", 41_500),), 0),                                        # a turn beginning just after the copy
], ids=["later turns", "old and later", "steered", "its own", "running at the time"])
def test_a_copy_without_its_own_row_takes_none_sent_for_another_turn(sent, chosen):
    index = _index(("turn-2", 40_000, 55_000), ("turn-3", 60_000, 75_000), ("turn-4", 80_000, 95_000),
                   ("old-0", -900_000, -899_000), ("racing", 42_000, 50_000))
    rows = _rows(*sent)
    traces = {row[0]: trace for row, (trace, _at) in zip(rows, sent)}
    copy_at = BASE + 41_000
    assert message_import_rows._own_turns_row(
        None, rows, "turn-2", BASE + 40_000, trace_ids=traces,
        ran_at=lambda trace: index.ran_at(trace, copy_at)) == chosen
