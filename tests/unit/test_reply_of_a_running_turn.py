"""A reply belongs to the latest turn that had begun when it was written (issue 360, R91).

A reply that follows no linked prompt (an automation's, a hook's) is given the
turn running at its time. A turn counts from two seconds before it began, for a
prompt written a moment before its turn is recorded. That slack gave a reply the
agent's next turn when that turn opened within two seconds after it. Adoption and
the leftover-live cleanup then looked under the wrong trace, and the reply was
stored twice: its live row, and the imported row carrying the next turn."""
import json
import time

import pytest

from lib import agents, clock, db, message_store, message_turns, transcript_incremental, turn_lifecycle
from lib.backend import claude

BASE = 1_794_900_000_000
PROMPT = "Check the watch list and report anything new."
ANSWER = "Nothing new on the watch list since the last check: all twelve feeds are quiet."
SHAPES = ["text only", "a tool first", "thinking first"]


def _record(path, role, content, offset, *, message_id=None):
    message = {'role': role, 'model': 'synthetic-fixture', 'content': content}
    if message_id:
        message['id'] = message_id
    with path.open('a') as stream:
        stream.write(json.dumps({'type': role, 'uuid': f'{role}-{offset}',
                                 'timestamp': clock.iso_from_ms(BASE + offset), 'message': message}) + '\n')


class _Agent:
    """An agent whose automation turn streamed its reply live, then settled; its
    next turn opens 1.5 s after the reply was written."""

    def __init__(self, tmp_path, monkeypatch, shape):
        self.now = [BASE]
        monkeypatch.setattr(time, 'time', lambda: self.now[0] / 1000)
        self.id = agents.create_agent(persona='Watch', session='watch', voice_id='v',
                                      cwd=str(tmp_path), backend='claude')
        agents.bind_backend_session(self.id, 'native-session')
        self.path = tmp_path / 'native.jsonl'
        self.path.write_text('')
        self.now[0] = BASE + 1000
        agents.open_turn(agent_id=self.id, source='local', trace_id='automation-turn')
        turn_lifecycle.transition(self.id, turn_lifecycle.TurnEvent.PROMPT_ADMITTED,
                                  {'trace_id': 'automation-turn', 'origin': 'automation'})
        _record(self.path, 'user', PROMPT, 1010)
        if shape == "a tool first":
            _record(self.path, 'assistant', [{'type': 'tool_use', 'id': 'toolu_1', 'name': 'Bash',
                                              'input': {'command': 'cat feeds.txt'}}], 1020, message_id='msg_tool')
            _record(self.path, 'user', [{'type': 'tool_result', 'tool_use_id': 'toolu_1', 'content': 'quiet'}], 1025)
        if shape == "thinking first":
            _record(self.path, 'assistant', [{'type': 'thinking', 'thinking': 'Looking.'}], 1020,
                    message_id='msg_think')
        self.now[0] = BASE + 1030
        for text in (ANSWER[:20], ANSWER):
            message_store.upsert_live_assistant_message(
                agent_id=self.id, backend_session_id='native-session', trace_id='automation-turn',
                item_key='msg_text', text=text)
        claude._settle_live_message(agent_id=self.id, backend_session_id='native-session',
                                    trace_id='automation-turn', item_key='msg_text', session='watch', stream=None)
        _record(self.path, 'assistant', [{'type': 'text', 'text': ANSWER}], 1040, message_id='msg_text')
        self.now[0] = BASE + 1120
        turn_lifecycle.transition(self.id, turn_lifecycle.TurnEvent.PROCESS_EXITED_OK,
                                  {'trace_id': 'automation-turn'})
        self.now[0] = BASE + 1040 + 1500
        agents.open_turn(agent_id=self.id, source='local', trace_id='next-turn')

    def imported(self):
        self.now[0] += 5
        transcript_incremental.import_transcript(backend='claude', path=self.path, agent_id=self.id,
                                                 backend_session_id='native-session')
        db.close_local()
        return [(row["phase"], row["trace_id"]) for row in message_store.list_messages(
            agent_id=self.id, backend_session_id='native-session')
            if row["role"] == "assistant" and row["text"]]


@pytest.mark.parametrize("shape", SHAPES)
def test_a_reply_is_stored_once_under_its_own_turn_when_the_next_turn_opens_just_after_it(
        tmp_path, monkeypatch, shape):
    agent = _Agent(tmp_path, monkeypatch, shape)
    assert agent.imported() == [("final", "automation-turn")]
    assert agent.imported() == [("final", "automation-turn")]


@pytest.mark.parametrize("shape", SHAPES)
def test_a_reply_stored_twice_before_is_one_row_again_after_the_next_import(tmp_path, monkeypatch, shape):
    repaired = message_turns.TurnIndex.running_at
    monkeypatch.setattr(message_turns.TurnIndex, "running_at",
                        lambda self, stamp, *, reply=False: repaired(self, stamp))
    agent = _Agent(tmp_path, monkeypatch, shape)
    assert agent.imported() == [(None, "automation-turn"), ("final", "next-turn")]    # as before
    monkeypatch.setattr(message_turns.TurnIndex, "running_at", repaired)
    agent.now[0] += 60_000
    assert agent.imported() == [("final", "automation-turn")]


def _index(*turns):
    return message_turns.TurnIndex.from_rows(
        [{"trace_id": trace, "started_at": BASE + started,
          "settled_at": None if settled is None else BASE + settled, "outcome": None}
         for trace, started, settled in turns])


@pytest.mark.parametrize("earlier_settled, reply, prompt", [
    (None, "earlier", "next"),       # still running: the reply is its own, a prompt takes the next turn
    (5_000, "earlier", "next"),      # settled at the reply: still its own
    (4_999, "earlier", "next"),      # settled before it, the next not yet begun: the earlier (R91)
    (5_000 - 120_001, "", "next"),   # past the settled turn's two minutes: none, as before
], ids=["running", "settled at the reply", "settled before it", "out of its reach"])
def test_a_reply_is_the_latest_turns_that_had_begun_by_its_time(earlier_settled, reply, prompt):
    index = _index(("earlier", 1_000 - (120_001 if earlier_settled == 5_000 - 120_001 else 0), earlier_settled),
                   ("next", 6_500, None))
    assert index.trace_at(BASE + 5_000, reply=True) == reply
    assert index.trace_at(BASE + 5_000) == prompt


def test_a_reply_with_no_turn_begun_by_its_time_takes_none():
    index = _index(("next", 6_500, None))
    assert index.trace_at(BASE + 5_000, reply=True) == ""
    assert index.trace_at(BASE + 5_000) == "next"                     # a prompt keeps the slack


def test_a_settled_turn_with_a_twin_row_that_never_settled_reaches_two_minutes_past_its_settle():
    """The two turn rows a prompt has a second apart (R92): one settled, its twin never. The turn
    ended at that settle (TurnIndex.ended_at, the one definition), so a reply finds it for the two
    minutes after it and not for the six hours an unsettled row would have had."""
    index = _index(("prompt", 1_000, 20_000), ("prompt", 2_000, None))
    assert index.trace_at(BASE + 20_000 + 120_000, reply=True) == "prompt"
    assert index.trace_at(BASE + 20_000 + 120_001, reply=True) == ""
    assert index.ended_at("prompt") == BASE + 20_000


def test_a_turn_that_never_settled_ends_at_the_next_turns_start_and_reaches_two_minutes_past_it():
    index = _index(("stuck", 1_000, None), ("stuck", 2_000, None), ("next", 600_000, 610_000))
    assert index.ended_at("stuck") == BASE + 600_000
    assert index.trace_at(BASE + 599_999, reply=True) == "stuck"
    assert index.trace_at(BASE + 600_000, reply=True) == "next"


def test_the_latest_turn_that_never_settled_still_has_six_hours():
    """With no settle and no later turn there is no end yet: a running turn, or one whose end was
    never recorded. Its reach is six hours from its start, as before."""
    index = _index(("running", 1_000, None))
    assert index.ended_at("running") is None
    assert index.trace_at(BASE + 1_000 + 6 * 3_600_000, reply=True) == "running"
    assert index.trace_at(BASE + 1_000 + 6 * 3_600_000 + 1, reply=True) == ""
