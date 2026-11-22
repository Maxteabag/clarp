"""Two turns started within the prompt slack each take their own row.

An automation's prompts, all with the same words, waited while its agent's seat was at its limit.
Each held turn was launched, failed at once, and the next was launched about two seconds later in
the same process, so the CLI wrote the prompt copies in pairs two seconds apart. For a prompt the
turn index names the latest turn that began up to _START_SLACK_MS after it, so the first copy of a
pair was given the second turn's row; the second copy found its own row taken and was stored as a
user row (the owner's bubble with the automation's words). The copy belongs to the earliest-started
turn that may be running at its time and still has a row with those words."""
import json
import time

import pytest

from lib import agents, clock, db, message_store

BASE = 1_795_000_000_000
HOUR = 3_600_000
PROMPT = "Watch the August feeds and say what changed since the last look, in one short paragraph only."


HOSTS = {"carry_live_rows": True, "tail_yield_after_first_write": True, "carry_settled_turns": True,
         "boundary_skips_dropped_records": True, "tail_keeps_frontier_when_grown": True,
         "audit_grown_by_digest": True, "full_digest_while_parsing": True, "carry_live_rows_by_words": True,
         "tail_hashes_while_parsing": True}       # tests/readmodel/rm_n14_harness.HOSTS_SWITCHES
CONFIGURATIONS = pytest.mark.parametrize("configuration", ["defaults", "hosts"])


def _configured(monkeypatch, configuration):
    from lib import transcript_incremental as inc
    if configuration == "hosts":
        settings = dict(inc._DEFAULTS, **HOSTS)
        monkeypatch.setattr(inc, "settings", lambda: dict(settings))


class _Agent:
    def __init__(self, tmp_path, monkeypatch):
        self.now = [BASE]
        monkeypatch.setattr(time, 'time', lambda: self.now[0] / 1000)
        self.monkeypatch = monkeypatch
        self.id = agents.create_agent(persona='Wren', session='wren', voice_id='v', cwd=str(tmp_path),
                                      backend='claude')
        agents.bind_backend_session(self.id, 'native')
        self.path = tmp_path / 'native.jsonl'
        self.path.write_text('')

    def line(self, role, content, at):
        with self.path.open('a') as stream:
            stream.write(json.dumps({'type': role, 'uuid': f'{role}-{at}', 'timestamp': clock.iso_from_ms(at),
                                     'message': {'role': role, 'model': 'synthetic-fixture',
                                                 'content': content}}) + '\n')

    def admitted(self, cid, trace, at):
        self.now[0] = at
        agents.record_user_message(agent_id=self.id, backend_session_id='native', client_msg_id=cid,
                                   text=PROMPT, trace_id=trace, origin="automation")

    def turn(self, trace, at, *, failed_at=None):
        self.now[0] = at
        agents.open_turn(agent_id=self.id, source='pwa', trace_id=trace)
        if failed_at is not None:
            with db.conn() as con:
                con.execute("UPDATE turns SET settled_at=?, outcome='failed' WHERE trace_id=?",
                            (failed_at, trace))
            db.close_local()

    def imported(self, *, full=False):
        from lib import transcript_incremental as inc
        if full:
            self.monkeypatch.setattr(inc, "parser_version", lambda backend: f"another-importer-{self.now[0]}")
        self.now[0] += 5
        inc.import_transcript(backend='claude', path=self.path, agent_id=self.id, backend_session_id='native')
        db.close_local()
        rows = message_store.list_messages(agent_id=self.id, backend_session_id='native')
        return ([(row["id"], row.get("origin")) for row in rows if row["role"] == "user"],
                [(row["text"], row.get("trace_id")) for row in rows if row["role"] == "assistant"])


@CONFIGURATIONS
def test_two_turns_started_within_the_slack_each_take_their_own_row(tmp_path, monkeypatch, configuration):
    _configured(monkeypatch, configuration)
    world = _Agent(tmp_path, monkeypatch)
    world.admitted("first", "t-first", BASE)                 # both admitted while the seat was at its limit
    world.admitted("second", "t-second", BASE + 20 * 60_000)
    start = BASE + 5 * HOUR
    world.turn("t-first", start, failed_at=start + 1900)     # launched, its prompt written, failed at once
    world.line('user', PROMPT, start + 1600)
    world.turn("t-second", start + 2000)                     # the next one, two seconds later
    world.line('user', PROMPT, start + 3500)
    world.line('assistant', [{'type': 'text', 'text': "A look: quiet."}], start + 8000)
    world.now[0] = start + 10_000
    for full in (False, False, True):
        users, replies = world.imported(full=full)
        assert users == [("u-first", "automation"), ("u-second", "automation")], users
        assert dict(replies) == {"A look: quiet.": "t-second"}


def test_a_copy_just_before_its_own_turn_row_still_takes_it(tmp_path, monkeypatch):
    """The case the slack is for: the prompt is written a moment before its turn row, while the
    turn before has not settled. That turn's row was taken by its own copy, so the new turn's is."""
    world = _Agent(tmp_path, monkeypatch)
    world.admitted("first", "t-first", BASE)
    world.turn("t-first", BASE + 500)
    world.line('user', PROMPT, BASE + 900)
    world.line('assistant', [{'type': 'text', 'text': "First: quiet."}], BASE + 4000)
    world.admitted("second", "t-second", BASE + 60_000)
    world.line('user', PROMPT, BASE + 61_000)                # written before its turn row
    world.turn("t-second", BASE + 62_000)
    world.line('assistant', [{'type': 'text', 'text': "Second: quiet."}], BASE + 66_000)
    world.now[0] = BASE + 70_000
    for full in (False, True):
        users, replies = world.imported(full=full)
        assert users == [("u-first", "automation"), ("u-second", "automation")], users
        assert dict(replies) == {"First: quiet.": "t-first", "Second: quiet.": "t-second"}
