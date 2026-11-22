"""A follow-up steered into a running turn whose copy reaches the transcript before its row.

The CLI writes its copy of a follow-up into the transcript when it reads the line, and the
Host writes the follow-up's row at the CLI's echo, after that (here 2.3 s after). An import in
between found no row for the copy and stored it as a user row of the running turn: another
agent's words drawn as the user's own. A later import never took the row either, because it
was written more than _START_SLACK_MS after the copy. Now the import links the copy to the row
the pending steer will have (lib.steer_records, read before the /send rows), and a steered row
is judged by when it was sent: the same in a tail as in a full import."""
import json
import time

import pytest

from lib import agents, clock, db, message_store, steer_records, turn_queue

BASE = 1_795_000_000_000
WORDS = "Same on Android: a moved value that is not an object breaks the list."
HOSTS = {"carry_live_rows": True, "tail_yield_after_first_write": True, "carry_settled_turns": True,
         "boundary_skips_dropped_records": True, "tail_keeps_frontier_when_grown": True,
         "audit_grown_by_digest": True, "full_digest_while_parsing": True, "carry_live_rows_by_words": True,
         "tail_hashes_while_parsing": True}       # tests/readmodel/rm_n14_harness.HOSTS_SWITCHES
CONFIGURATIONS = pytest.mark.parametrize("configuration", ["defaults", "hosts"])


class _Agent:
    """A Claude agent in a running turn, and another agent whose message is steered into it."""

    def __init__(self, tmp_path, monkeypatch, configuration):
        from lib import transcript_incremental as inc
        if configuration == "hosts":
            settings = dict(inc._DEFAULTS, **HOSTS)
            monkeypatch.setattr(inc, "settings", lambda: dict(settings))
        self.now = [BASE]
        monkeypatch.setattr(time, 'time', lambda: self.now[0] / 1000)
        self.monkeypatch = monkeypatch
        self.id = agents.create_agent(persona='Arden', session='arden', voice_id='v', cwd=str(tmp_path),
                                      backend='claude')
        self.peer = agents.create_agent(persona='Ellis', session='ellis', voice_id='v2', cwd=str(tmp_path),
                                        backend='claude')
        agents.bind_backend_session(self.id, 'native')
        self.path = tmp_path / 'native.jsonl'
        self.path.write_text('')
        agents.record_user_message(agent_id=self.id, backend_session_id='native', client_msg_id="work",
                                   text="Fix the list on iOS.", trace_id="running", origin="user")
        agents.open_turn(agent_id=self.id, source='pwa', trace_id="running")
        self.line('user', "Fix the list on iOS.", BASE + 300)
        self.line('assistant', [{'type': 'text', 'text': "Looking at the list now."}], BASE + 2000)

    def line(self, role, content, at):
        with self.path.open('a') as stream:
            stream.write(json.dumps({'type': role, 'uuid': f'{role}-{at}', 'timestamp': clock.iso_from_ms(at),
                                     'message': {'role': role, 'model': 'synthetic-fixture',
                                                 'content': content}}) + '\n')

    def steer(self, at):
        """The dispatcher's steer: the queue item, then the durable record, then the stdin write."""
        self.now[0] = at
        turn_queue.enqueue(queue_id="q-steer", agent_id=self.id, session='arden', text=WORDS,
                           trace_id="steered", client_msg_id="from-ellis", synthesize_audio=False,
                           origin="agent", sender_agent_id=self.peer)
        steer_records.record(agent_id=self.id, trace_id="steered", queue_id="q-steer", backend="claude",
                             session_id="native", cwd="/tmp")
        self.sent = at

    def echoed(self, at):
        """The CLI's echo: the row (with the time it was sent), the item shown, the record cleared."""
        self.now[0] = at
        agents.record_user_message(agent_id=self.id, backend_session_id='native', client_msg_id="from-ellis",
                                   text=WORDS, trace_id="steered", origin="agent", sender_agent_id=self.peer,
                                   sent_at=self.sent)
        turn_queue.show("q-steer")
        steer_records.clear("steered")
        db.close_local()

    def imported(self, *, full=False):
        from lib import transcript_incremental as inc
        if full:
            self.monkeypatch.setattr(inc, "parser_version", lambda backend: f"another-importer-{self.now[0]}")
        self.now[0] += 5
        inc.import_transcript(backend='claude', path=self.path, agent_id=self.id, backend_session_id='native')
        db.close_local()
        rows = message_store.list_messages(agent_id=self.id, backend_session_id='native')
        return ([(row["id"], row.get("origin"), row["text"]) for row in rows if row["role"] == "user"],
                [(row["text"], row.get("origin"), row.get("reply_to_agent_id")) for row in rows
                 if row["role"] == "assistant"])


@CONFIGURATIONS
def test_a_copy_imported_before_the_echo_is_never_a_user_row(tmp_path, monkeypatch, configuration):
    world = _Agent(tmp_path, monkeypatch, configuration)
    world.steer(BASE + 5000)
    world.line('user', WORDS, BASE + 5400)                               # the CLI read it
    world.line('assistant', [{'type': 'text', 'text': "Noted, Android too."}], BASE + 6000)
    world.now[0] = BASE + 6500
    users, replies = world.imported()                                    # before the echo: no row yet
    assert [text for _id, _origin, text in users] == ["Fix the list on iOS."], users
    assert dict((text, origin) for text, origin, _sender in replies)["Noted, Android too."] == "agent"
    world.echoed(BASE + 7700)                                            # 2.3 s after the copy
    for full in (False, False, True):
        users, replies = world.imported(full=full)
        assert users == [("u-work", "user", "Fix the list on iOS."), ("u-from-ellis", "agent", WORDS)], users
        assert ("Noted, Android too.", "agent", world.peer) in replies


@CONFIGURATIONS
def test_a_copy_imported_after_the_echo_links_the_row_written_after_it(tmp_path, monkeypatch, configuration):
    """No import ran in between: the row was written 2.3 s after the copy, and is the copy's own."""
    world = _Agent(tmp_path, monkeypatch, configuration)
    world.imported()
    world.steer(BASE + 5000)
    world.line('user', WORDS, BASE + 5400)
    world.line('assistant', [{'type': 'text', 'text': "Noted, Android too."}], BASE + 6000)
    world.echoed(BASE + 7700)
    for full in (False, False, True):
        users, replies = world.imported(full=full)
        assert users == [("u-work", "user", "Fix the list on iOS."), ("u-from-ellis", "agent", WORDS)], users
        assert ("Noted, Android too.", "agent", world.peer) in replies


@CONFIGURATIONS
def test_the_same_words_typed_before_the_steer_stay_a_row_of_their_own(tmp_path, monkeypatch, configuration):
    """A copy written before the steer was sent is not the steer's: the steer's own copy takes it."""
    world = _Agent(tmp_path, monkeypatch, configuration)
    world.line('user', WORDS, BASE + 2500)                               # typed in the terminal: no row
    world.steer(BASE + 5000)
    world.line('user', WORDS, BASE + 5400)
    world.line('assistant', [{'type': 'text', 'text': "Noted, Android too."}], BASE + 6000)
    world.now[0] = BASE + 6500
    world.imported()
    world.echoed(BASE + 7700)
    for full in (False, True):
        users, _replies = world.imported(full=full)
        typed = [row_id for row_id, _origin, text in users if text == WORDS and not row_id.startswith("u-")]
        assert len(typed) == 1 and ("u-from-ellis", "agent", WORDS) in users, users
