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
    monkeypatch.setattr(message_import_rows, "_START_SLACK_MS", 10**15)      # as before: no row is ruled out
    monkeypatch.setattr(message_turns.TurnIndex, "ended_before", lambda self, trace, timestamp: False)
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


def test_a_turn_ended_when_it_settled_or_the_next_turn_began():
    index = _index(("t", 10_000, 20_000), ("t", 11_000, 21_000), ("never", 30_000, None),
                   ("never", 31_000, 32_000), ("late", 40_000, 60_000), ("next", 50_000, None))
    assert [index.ended_before("t", BASE + at) for at in (21_000, 21_001)] == [False, True]
    # One row never settled: the turn ended when the next turn began, not never.
    assert [index.ended_before("never", BASE + at) for at in (40_000, 40_001)] == [False, True]
    # Settled after the next turn began: it ended at that start.
    assert [index.ended_before("late", BASE + at) for at in (50_000, 50_001)] == [False, True]
    assert index.ended_before("next", BASE + 10**9) is False             # the latest still runs
    assert index.ended_before("steered", BASE + 15_000) is False         # no turn of its own


def _rows(*sent):
    return [(f"c{n}", "automation", "", clock.iso_from_ms(BASE + at)) for n, (_trace, at) in enumerate(sent)]


@pytest.mark.parametrize("sent, chosen", [
    ((("turn-3", 60_000), ("turn-4", 80_000)), None),                  # both sent after the copy
    ((("old-0", -900_000), ("turn-4", 80_000)), None),                 # an old send's turn had ended
    ((("steered", 41_000), ("turn-4", 80_000)), 0),                    # steered into this turn
    ((("turn-4", 80_000), ("turn-2", 39_000)), 1),                     # its own row, wherever it stands
    ((("late", 40_500),), 0),                                          # sent before; its turn recorded later
    ((("old-0", -900_000),), None),                                    # the one row left is an old send's
], ids=["sent after the copy", "old and later", "steered", "its own", "a turn recorded late",
        "only an old send"])
def test_a_copy_without_its_own_row_takes_none_that_cannot_be_its_own(sent, chosen):
    index = _index(("turn-2", 40_000, 55_000), ("turn-3", 60_000, 75_000), ("turn-4", 80_000, 95_000),
                   ("old-0", -900_000, None), ("late", 300_000, 301_000))
    rows = _rows(*sent)
    traces = {row[0]: trace for row, (trace, _at) in zip(rows, sent)}
    copy_at = clock.iso_from_ms(BASE + 41_000)
    assert message_import_rows._own_turns_row(
        None, rows, "turn-2", BASE + 40_000, trace_ids=traces, written_at=copy_at,
        ended_before=lambda trace: index.ended_before(trace, copy_at)) == chosen


@pytest.mark.parametrize("second", ["its own turn a minute later", "steered into the first turn"])
def test_the_same_words_sent_twice_by_the_user_link_each_copy_to_its_own_row(tmp_path, monkeypatch, second):
    """Two 'yes' a minute apart, each its own turn; or the second steered into the first turn while it
    runs (its row has a trace with no turn of its own). Each transcript copy takes its own row: no
    copy is stored beside its row, and each reply carries the turn it answers."""
    now = [BASE]
    monkeypatch.setattr(time, 'time', lambda: now[0] / 1000)
    aid = agents.create_agent(persona='Ann', session='ann', voice_id='v', cwd=str(tmp_path), backend='claude')
    agents.bind_backend_session(aid, 'native')
    path = tmp_path / 'native.jsonl'
    path.write_text('')

    def line(role, content, at):
        with path.open('a') as stream:
            stream.write(json.dumps({'type': role, 'uuid': f'{role}-{at}', 'timestamp': clock.iso_from_ms(at),
                                     'message': {'role': role, 'model': 'synthetic-fixture', 'content': content}}) + '\n')

    def send(cid, trace, at, *, opens):
        now[0] = at
        agents.record_user_message(agent_id=aid, backend_session_id='native', client_msg_id=cid, text="yes",
                                   trace_id=trace, origin="user")
        if opens:
            agents.open_turn(agent_id=aid, source='pwa', trace_id=trace)
            turn_lifecycle.transition(aid, turn_lifecycle.TurnEvent.PROMPT_ADMITTED, {'trace_id': trace, 'origin': 'user'})

    def settle(trace, at):
        now[0] = at
        turn_lifecycle.transition(aid, turn_lifecycle.TurnEvent.PROCESS_EXITED_OK, {'trace_id': trace})

    send("first", "turn-a", BASE, opens=True)
    line('user', "yes", BASE + 500)
    if second == "steered into the first turn":
        send("again", "steered-b", BASE + 3000, opens=False)
        line('user', "yes", BASE + 3500)
        line('assistant', [{'type': 'text', 'text': "Done, both noted."}], BASE + 6000)
        settle("turn-a", BASE + 7000)
        expected = {"Done, both noted.": "turn-a"}
    else:
        line('assistant', [{'type': 'text', 'text': "Noted the first."}], BASE + 4000)
        settle("turn-a", BASE + 5000)
        send("again", "turn-b", BASE + 60_000, opens=True)
        line('user', "yes", BASE + 60_500)
        line('assistant', [{'type': 'text', 'text': "Noted the second."}], BASE + 64_000)
        settle("turn-b", BASE + 65_000)
        expected = {"Noted the first.": "turn-a", "Noted the second.": "turn-b"}
    for _ in range(2):
        now[0] += 5
        transcript_incremental.import_transcript(backend='claude', path=path, agent_id=aid,
                                                 backend_session_id='native')
        db.close_local()
        rows = message_store.list_messages(agent_id=aid, backend_session_id='native')
        assert [row["id"] for row in rows if row["role"] == "user"] == ["u-first", "u-again"]
        assert {row["text"]: row["trace_id"] for row in rows if row["role"] == "assistant"} == expected


@pytest.mark.parametrize("sent, chosen", [
    ((("steered-a", 40_500), ("steered-b", 40_800)), 0),             # both before the copy: the older
    ((("steered-a", 40_500), ("steered-b", 41_500)), 0),             # one before, one in the 2 s after
    ((("stale", 30_000), ("steered-b", 40_800)), 1),                 # sent after this turn began first
    ((("stale", 30_000),), 0),                                       # else the oldest left
], ids=["two before the copy", "before and just after", "after the turn began first", "only an older one"])
def test_of_several_rows_that_could_be_the_copys_the_oldest_sent_since_its_turn_began_is_taken(sent, chosen):
    """Sends reach the provider in the order they were sent, so the first copy is the oldest send
    still unlinked: the row sent closest to the copy would swap two sends of the same words."""
    index = _index(("turn-2", 40_000, 55_000))
    rows = _rows(*sent)
    traces = {row[0]: trace for row, (trace, _at) in zip(rows, sent)}
    copy_at = clock.iso_from_ms(BASE + 41_000)
    assert message_import_rows._own_turns_row(
        None, rows, "turn-2", BASE + 40_000, trace_ids=traces, written_at=copy_at,
        ended_before=lambda trace: index.ended_before(trace, copy_at)) == chosen


def test_two_sends_of_the_same_words_into_a_running_turn_link_in_the_order_they_were_sent(tmp_path, monkeypatch):
    """The user says 'yes', then another agent sends 'yes', both steered into the running turn;
    the provider writes both copies after them. Each copy takes the row sent first, so the reply
    after the second copy is the other agent's; the same in a tail as in a full import."""
    from lib import transcript_incremental as inc
    now = [BASE]
    monkeypatch.setattr(time, 'time', lambda: now[0] / 1000)
    aid = agents.create_agent(persona='Ann', session='ann', voice_id='v', cwd=str(tmp_path), backend='claude')
    other = agents.create_agent(persona='Bob', session='bob', voice_id='v2', cwd=str(tmp_path), backend='claude')
    agents.bind_backend_session(aid, 'native')
    path = tmp_path / 'native.jsonl'
    path.write_text('')

    def line(role, content, at):
        with path.open('a') as stream:
            stream.write(json.dumps({'type': role, 'uuid': f'{role}-{at}', 'timestamp': clock.iso_from_ms(at),
                                     'message': {'role': role, 'model': 'synthetic-fixture', 'content': content}}) + '\n')

    agents.record_user_message(agent_id=aid, backend_session_id='native', client_msg_id="start", text="begin",
                               trace_id="turn-a", origin="user")
    agents.open_turn(agent_id=aid, source='pwa', trace_id="turn-a")
    line('user', "begin", BASE + 300)
    now[0] = BASE + 5000
    agents.record_user_message(agent_id=aid, backend_session_id='native', client_msg_id="from-user", text="yes",
                               trace_id="steer-1", origin="user")
    now[0] = BASE + 10_000
    agents.record_user_message(agent_id=aid, backend_session_id='native', client_msg_id="from-bob", text="yes",
                               trace_id="steer-2", origin="agent", sender_agent_id=other)
    line('user', "yes", BASE + 12_000)
    line('user', "yes", BASE + 12_100)
    line('assistant', [{'type': 'text', 'text': "Both answers noted."}], BASE + 14_000)

    def reply_sender():
        now[0] += 5
        transcript_incremental.import_transcript(backend='claude', path=path, agent_id=aid,
                                                 backend_session_id='native')
        db.close_local()
        rows = message_store.list_messages(agent_id=aid, backend_session_id='native')
        return [row.get("origin") for row in rows if row["role"] == "assistant"]

    # Linked the other way round (the row sent closest to each copy), the reply would be the user's.
    assert reply_sender() == ["agent"]
    assert reply_sender() == ["agent"]                              # again, as a tail
    monkeypatch.setattr(inc, "parser_version", lambda backend: "a-new-importer")
    assert reply_sender() == ["agent"]                              # a full import decides the same
