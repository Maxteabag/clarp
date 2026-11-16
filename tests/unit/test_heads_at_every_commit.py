"""At the end of every transaction that wrote a message or a head, no
conversation has messages above its head (issue 371).

The agent list reads what changed from the conversation heads alone. A writer
that gave a row a revision in one commit and raised the head in the next left
the head behind between the two, and for good when the second gave up on the
lock or was abandoned: the reply finalized after a newer turn had opened was
rewritten under a new revision and its head never raised (review of #374).
Looking once the writer has returned does not see the first of these; this
looks at every commit.
"""
from __future__ import annotations

import re
import sqlite3

import pytest

from lib import agents, db, message_previews, message_store, turn_lifecycle

_WRITES = re.compile(r"\b(INSERT|UPDATE|DELETE|REPLACE)\b[^;]*?\b(messages|conversation_heads)\b", re.I | re.S)
_ENDS = re.compile(r"^\s*(COMMIT|END)\b", re.I)
_BEHIND = """SELECT m.agent_id, m.backend_session_id, m.revision, h.revision
               FROM (SELECT agent_id, backend_session_id, MAX(revision) AS revision
                       FROM messages GROUP BY agent_id, backend_session_id) m
               LEFT JOIN conversation_heads h
                 ON h.agent_id = m.agent_id AND h.backend_session_id IS m.backend_session_id
              WHERE h.revision IS NULL OR h.revision < m.revision"""


_ROWS = """SELECT agent_id, backend_session_id, COUNT(*), MAX(revision), SUM(revision)
             FROM messages GROUP BY agent_id, backend_session_id"""
_HEADS = "SELECT agent_id, backend_session_id, revision FROM conversation_heads"


def _state(connection):
    """{conversation: (its rows: how many, their highest and summed revisions; its head)}."""
    rows = {(a, s): tuple(rest) for a, s, *rest in sqlite3.Connection.execute(connection, _ROWS)}
    heads = {(a, s): revision for a, s, revision in sqlite3.Connection.execute(connection, _HEADS)}
    return {key: (rows.get(key), heads.get(key)) for key in rows.keys() | heads.keys()}


@pytest.fixture
def commits(monkeypatch):
    """Every commit of this process's store connections that wrote a message
    or a head, with the conversations it left wrong:
    [(the statement that ended it, [what is wrong])]. Wrong is a conversation
    with messages above its head, or one whose rows changed (one more, one
    fewer, a new revision) in a commit that did not move its head."""
    seen: list[tuple[str, list]] = []
    plain = db._TrackedConnection.execute
    before: dict[int, dict] = {}

    def execute(self, sql, parameters=(), /):
        text = str(sql)
        if _WRITES.search(text) and id(self) not in before:
            before[id(self)] = _state(self)              # as it stood before this transaction's first write
        result = plain(self, sql, parameters)
        if id(self) in before and not self.in_transaction and (_WRITES.search(text) or _ENDS.match(text)):
            was, now = before.pop(id(self)), _state(self)
            wrong = [("above its head", *row) for row in sqlite3.Connection.execute(self, _BEHIND)]
            for key in was.keys() | now.keys():
                rows_was, head_was = was.get(key, (None, None))
                rows_now, head_now = now.get(key, (None, None))
                if rows_was != rows_now and head_was == head_now:
                    wrong.append(("rows changed, head did not", *key))
            seen.append((" ".join(text.split())[:60], wrong))
        elif id(self) in before and not self.in_transaction:
            before.pop(id(self))                         # rolled back
        return result

    monkeypatch.setattr(db._TrackedConnection, "execute", execute)
    return seen


def _left_behind(commits):
    return [(statement, behind) for statement, behind in commits if behind]


def _agent(name="Writer"):
    return agents.create_agent(persona=name, voice_id="", cwd="/tmp", session=name.lower())


WRITERS = {
    "a user message": lambda aid: message_store.record_user_message(
        agent_id=aid, backend_session_id="bs", client_msg_id="one", text="A prompt"),
    "a notice": lambda aid: message_store.record_system_notice(
        agent_id=aid, backend_session_id="bs", notice_id="n1", text="A notice"),
    "an interruption marker": lambda aid: (
        message_store.record_user_message(agent_id=aid, backend_session_id="bs", client_msg_id="cause", text="Cut"),
        message_store.record_interruption_marker(
            agent_id=aid, backend_session_id="bs", cause_message_id="u-cause", text="The turn was cut short")),
    "the trace given to a client row again": lambda aid: (
        message_store.record_user_message(agent_id=aid, backend_session_id="bs", client_msg_id="again", text="Again",
                                          trace_id="first"),
        message_store.relink_client_message("again", trace_id="second")),
    "a dream's digest": lambda aid: message_store.record_dream_digest(
        agent_id=aid, backend_session_id="bs", run_id="r1", text="A digest"),
    "a live row written, grown and deleted": lambda aid: (
        message_store.upsert_live_assistant_message(agent_id=aid, backend_session_id="bs", text="One"),
        message_store.upsert_live_assistant_message(agent_id=aid, backend_session_id="bs", text="One, two"),
        message_store.delete_live_assistant_message(agent_id=aid, backend_session_id="bs")),
    "a live row ended": lambda aid: (
        message_store.upsert_live_assistant_message(agent_id=aid, backend_session_id="bs", text="One"),
        message_store.mark_live_message_final(agent_id=aid, backend_session_id="bs")),
    "an import": lambda aid: (
        message_store.store_transcript_turns(
            agent_id=aid, backend_session_id="bs", source_file="/t/one.jsonl",
            turns=[{"role": "user", "text": "A prompt"}, {"role": "assistant", "text": "A reply"}]),
        message_store.store_transcript_turns(
            agent_id=aid, backend_session_id="bs", source_file="/t/one.jsonl",
            turns=[{"role": "user", "text": "A prompt, reworded"}, {"role": "assistant", "text": "A reply"}])),
}


@pytest.mark.parametrize("writer", list(WRITERS))
def test_no_commit_of_a_writer_leaves_a_head_below_its_messages(commits, writer):
    aid = _agent()
    WRITERS[writer](aid)
    assert len(commits) >= 1, "the fixture saw no commit"
    assert _left_behind(commits) == []
    assert db.conn().execute("SELECT COUNT(*) FROM messages WHERE agent_id=?", (aid,)).fetchone()[0] >= 0


def test_a_writer_inside_a_callers_transaction_joins_it(commits):
    """A caller's transaction is not committed under it, and a rollback takes
    the row and the head together."""
    aid = _agent()
    c = db.conn()
    c.execute("BEGIN IMMEDIATE")
    message_store.record_user_message(agent_id=aid, backend_session_id="bs", client_msg_id="inside", text="Inside")
    assert c.in_transaction and commits == []
    c.execute("ROLLBACK")
    assert c.execute("SELECT COUNT(*) FROM messages WHERE agent_id=?", (aid,)).fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM conversation_heads WHERE agent_id=?", (aid,)).fetchone()[0] == 0


def test_a_writer_whose_head_fails_leaves_no_row_behind(monkeypatch):
    """The second statement gives up (the lock, say): the row goes with it."""
    aid = _agent()
    plain = db._TrackedConnection.execute

    def execute(self, sql, parameters=(), /):
        if "INSERT INTO conversation_heads" in str(sql):
            raise sqlite3.OperationalError("database is locked")
        return plain(self, sql, parameters)
    monkeypatch.setattr(db._TrackedConnection, "execute", execute)
    with pytest.raises(sqlite3.OperationalError):
        message_store.record_system_notice(agent_id=aid, backend_session_id="bs", notice_id="n1", text="A notice")
    monkeypatch.setattr(db._TrackedConnection, "execute", plain)
    assert not db.conn().in_transaction
    assert db.conn().execute("SELECT COUNT(*) FROM messages WHERE agent_id=?", (aid,)).fetchone()[0] == 0


def test_a_reply_finalized_after_a_newer_turn_opened_has_its_head(commits):
    """The reviewer's case, through the real writers. A turn streams a reply;
    a message arrives while the agent works and opens a newer turn; then the
    first turn's result is finalized. Its streamed row is given the final
    words under a new revision, and the step that settles the row is
    abandoned because the newest open turn is no longer this reply's. The
    head was raised only in that abandoned step: the list gave the old
    revision and the cut reply until the conversation's next write."""
    aid = _agent("Oracle")
    message_store.record_user_message(agent_id=aid, backend_session_id="bs", client_msg_id="ask", text="A question",
                                      trace_id="first")
    turn_lifecycle.open_turn(agent_id=aid, source="pwa", trace_id="first", synthesize_audio=False)
    assert message_store.upsert_live_assistant_message(
        agent_id=aid, backend_session_id="bs", trace_id="first", text="A reply that streams") is not None
    assert message_store.dashboard_messages()[aid]["head"]["preview"] == "A reply that streams"
    turn_lifecycle.open_turn(agent_id=aid, source="pwa", trace_id="second", synthesize_audio=False)

    settled = message_store.finalize_live_assistant_message(
        agent_id=aid, backend_session_id="bs", trace_id="first", text="A reply that streams to its end.")

    assert settled is None                               # the settling step was abandoned
    row = db.conn().execute("SELECT text, revision FROM messages WHERE agent_id=? AND text LIKE 'A reply%'",
                            (aid,)).fetchone()
    head = db.conn().execute("SELECT revision FROM conversation_heads WHERE agent_id=? AND backend_session_id='bs'",
                             (aid,)).fetchone()[0]
    assert row["text"] == "A reply that streams to its end." and head >= row["revision"]
    assert _left_behind(commits) == []
    listed = message_store.dashboard_messages()[aid]
    assert listed["head"]["preview"] == "A reply that streams to its end."
    assert listed["revisions"] == {"bs": message_store.latest_revision(agent_id=aid, backend_session_id="bs")}


def test_the_walk_at_a_start_says_when_it_finds_a_head_behind(monkeypatch):
    """Both Hosts are silent today: a line means a writer left a head behind,
    or the database has rows from before the heads were kept. A count, no id."""
    lines = []
    monkeypatch.setattr(message_previews.cache_log, "log", lambda event, detail="", **kw: lines.append((event, detail)))
    aid = _agent()
    message_store.record_user_message(agent_id=aid, backend_session_id="bs", client_msg_id="one", text="A prompt")
    message_store.dashboard_messages()
    assert [line for line in lines if line[0] == "dashboardHeadsBehind"] == []

    message_previews._FLOOR_CACHE.reset()
    message_previews._REVISION_CACHE.reset()
    db.conn().execute("""INSERT INTO messages (message_id, agent_id, backend_session_id, seq, role, text, revision, updated_at)
                         VALUES ('behind', ?, 'no-head', 1, 'user', 'from before', 99, 1000)""", (aid,))
    db.conn().execute("""INSERT INTO messages (message_id, agent_id, seq, role, text, revision, updated_at)
                         VALUES ('nowhere', ?, 1, 'user', 'no session', 98, 1000)""", (aid,))
    for _ in range(3):
        message_store.dashboard_messages()
        message_store.record_user_message(agent_id=aid, backend_session_id="bs", client_msg_id=f"more-{_}", text="More")
    said = [line for line in lines if line[0] == "dashboardHeadsBehind"]
    assert said == [("dashboardHeadsBehind", "conversations=2 without_session=1")]      # once for a database
    assert aid not in said[0][1] and "no-head" not in said[0][1]
