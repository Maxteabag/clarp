"""The /send rows and the latest user row, taken from rows read anyway.

An import that publishes a frontier reads every row of the session, every
column. ``message_import_common._session_parts`` takes from those rows what
two statements used to ask SQLite for again:

    SELECT message_id, text, origin, sender_agent_id, timestamp FROM messages
     WHERE agent_id = ? AND backend_session_id = ? AND source_file LIKE 'client:%'
     ORDER BY updated_at ASC, seq ASC

    SELECT message_id, origin, sender_agent_id FROM messages
     WHERE agent_id = ? AND backend_session_id = ? AND role = 'user'
     ORDER BY updated_at DESC, revision DESC, seq DESC LIMIT 1

No index holds source_file or role, so each fetched every row of the session
from the table. The answers must be the statements' own, to the row; every
test here is a pair with the statement on a real table.
"""
from __future__ import annotations

import random
import sqlite3

import pytest

from lib import agents, db, message_import_common as common, message_turns, message_writes

SESSIONS = ("one", "two")
SOURCES = ("client:a", "client:b", "CLIENT:upper", "cLiEnT:mixed", "client:", "client", "clientx",
           "clıent:dotless", "Ｃlient:fullwidth", "live:t", "final:t", "/tmp/t.jsonl", "",
           None, " client:space", "client:%", "Client:é")
ROLES = ("user", "user", "assistant", "User", "USER", "", None, "tool")


def _agent(tmp_path, name="generated"):
    return agents.create_agent(persona=name, voice_id="v", cwd=str(tmp_path), session=name)


def _insert(connection, agent, rows):
    connection.executemany(
        "INSERT INTO messages(message_id,agent_id,backend_session_id,source_file,seq,role,timestamp,"
        "text,updated_at,revision,origin,sender_agent_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [(row["message_id"], agent, row["session"], row["source"], row["seq"], row["role"],
          row["timestamp"], row["text"], row["updated_at"], row["revision"], row["origin"],
          row["sender"]) for row in rows])


def _generated(seed, *, count=140, odd_kinds=False):
    """Rows with many equal ``updated_at`` and ``revision`` values, so that
    most of the order is decided by the last key. ``seq`` is unique within a
    session, as the table demands."""
    chance = random.Random(seed)
    stamps = [1000, 1000, 1001, 1002, 1002, 1002, 2000]
    revisions = [1, 1, 2, 3, 3]
    if odd_kinds:
        # INTEGER columns keep what cannot be read as a number, as text; a
        # real stays a real. SQLite orders numbers before text.
        stamps += [1001.5, "1002", "later", "", 1e3]
        revisions += [2.5, "x", "3"]
    seqs = {session: chance.sample(range(-300, 300), count) for session in (*SESSIONS, None)}
    rows = []
    for number in range(count):
        session = chance.choice((*SESSIONS, *SESSIONS, None))
        rows.append({
            "message_id": f"m-{seed}-{number:04d}", "session": session,
            "source": chance.choice(SOURCES), "seq": seqs[session].pop(),
            "role": chance.choice(ROLES),
            "timestamp": chance.choice((None, "", "2026-11-14T10:00:00Z", "2026-11-14T10:00:01Z")),
            "text": chance.choice(("hello", "hello", "Hello ", "another", "")),
            "updated_at": chance.choice(stamps), "revision": chance.choice(revisions),
            "origin": chance.choice(("user", "heartbeat", "agent", "")),
            "sender": chance.choice((None, "", "peer"))})
    return rows


def _every_column(connection, agent, session=None):
    if session is None:
        return connection.execute(
            "SELECT * FROM messages WHERE agent_id=? ORDER BY message_id", (agent,)).fetchall()
    return message_writes._carry_message_rows(connection, agent, session, True)


def _asked(connection, agent, session):
    return (common._client_provenance_rows(connection, agent, session),
            common._latest_user_provenance(connection, agent, session))


@pytest.mark.parametrize("odd_kinds", [False, True], ids=["numbers", "numbers, reals and text"])
@pytest.mark.parametrize("seed", range(40))
def test_the_parts_are_the_statements_answers_row_for_row(tmp_path, seed, odd_kinds):
    agent, other = _agent(tmp_path), _agent(tmp_path, "another")
    connection = db.conn()
    _insert(connection, agent, _generated(seed, odd_kinds=odd_kinds))
    _insert(connection, other, _generated(seed + 1000, count=30))
    for session in SESSIONS:
        asked = _asked(connection, agent, session)
        assert common._session_parts(_every_column(connection, agent, session), session) == asked
        # The whole agent's rows (no carry_session_rows) hold the same answer.
        assert common._session_parts(_every_column(connection, agent), session) == asked
    # The generated rows do reach what is being compared.
    sends, user = _asked(connection, agent, "one")
    assert len(sends) > 5 and user[0]
    assert len({row[0] for row in sends}) == len(sends)


def test_equal_times_and_revisions_are_decided_by_seq_as_the_statements_decide(tmp_path):
    """Every row written in the same millisecond under the same revision:
    the statements order by seq, which the table keeps unique in a session."""
    agent = _agent(tmp_path)
    connection = db.conn()
    order = [5, -2, 9, 0, 3, -7, 8]
    _insert(connection, agent, [
        {"message_id": f"m-{seq}", "session": "one", "source": f"client:{seq}", "seq": seq,
         "role": "user", "timestamp": None, "text": "the same words", "updated_at": 1000,
         "revision": 4, "origin": "user", "sender": None} for seq in order])
    sends, user = common._session_parts(_every_column(connection, agent, "one"), "one")
    assert [row[0] for row in sends] == [f"m-{seq}" for seq in sorted(order)]
    assert user == ("m-9", "user", "")
    assert (sends, user) == _asked(connection, agent, "one")


def test_two_rows_of_a_session_cannot_tie_in_every_key(tmp_path):
    """Why the two orders have no tie to break: (agent, session, seq) is unique."""
    agent = _agent(tmp_path)
    connection = db.conn()
    row = {"message_id": "m-1", "session": "one", "source": "client:a", "seq": 7, "role": "user",
           "timestamp": None, "text": "x", "updated_at": 1, "revision": 1, "origin": "user",
           "sender": None}
    _insert(connection, agent, [row])
    with pytest.raises(sqlite3.IntegrityError):
        _insert(connection, agent, [{**row, "message_id": "m-2", "source": "client:b"}])
    with pytest.raises(sqlite3.IntegrityError):
        _insert(connection, agent, [{**row, "message_id": "m-3", "seq": 7.0}])


def test_a_session_without_send_rows_or_user_rows(tmp_path):
    agent = _agent(tmp_path)
    connection = db.conn()
    _insert(connection, agent, [
        {"message_id": "m-1", "session": "one", "source": "/tmp/t.jsonl", "seq": 0,
         "role": "assistant", "timestamp": None, "text": "x", "updated_at": 1, "revision": 1,
         "origin": "user", "sender": None}])
    for session in ("one", "never seen"):
        parts = common._session_parts(_every_column(connection, agent, session), session)
        assert parts == ([], ("", "user", "")) == _asked(connection, agent, session)


@pytest.mark.parametrize("column", ["source_file", "role", "backend_session_id"])
def test_a_blob_where_text_belongs_is_left_to_the_statements(tmp_path, column):
    """LIKE and = read a blob differently from text. Rather than imitate
    that, the rows are not used and the import asks as it did."""
    agent = _agent(tmp_path)
    connection = db.conn()
    _insert(connection, agent, [
        {"message_id": f"m-{n}", "session": "one", "source": "client:a" if n else "/tmp/t.jsonl",
         "seq": n, "role": "user", "timestamp": None, "text": "x", "updated_at": n, "revision": n,
         "origin": "user", "sender": None} for n in range(4)])
    connection.execute(f"UPDATE messages SET {column}=? WHERE message_id='m-3'", (b"client:blob",))
    rows = message_writes._carry_message_rows(connection, agent, "one", False)
    assert common._session_parts(rows, "one") is None
    assert message_writes._sends_and_latest_user(connection, agent, "one", rows) \
        == _asked(connection, agent, "one")
    assert common._session_parts(_every_column(connection, agent, "one"), None) is None


# ---- the import asks SQLite only when it has not read the rows ----------------------

def _statements(connection):
    seen = []
    connection.set_trace_callback(seen.append)
    return seen


def _asks(seen):
    return (sum("source_file LIKE 'client:%'" in sql and "ORDER BY updated_at" in sql for sql in seen),
            sum("role = 'user'" in sql and "LIMIT 1" in sql for sql in seen))


@pytest.mark.parametrize("session_rows", [True, False], ids=["the session's rows", "the agent's rows"])
def test_an_import_that_reads_the_rows_does_not_ask_for_the_two_again(tmp_path, session_rows):
    agent = _agent(tmp_path)
    connection = db.conn()
    _insert(connection, agent, _generated(7))
    expected = _asked(connection, agent, "one")
    seen = _statements(connection)
    try:
        raw = message_writes._prepare_import_prelude(
            connection, agent, "one", [], raw_only=True, session_rows=session_rows)
        assert _asks(seen) == (0, 0)
        assert (raw[1], raw[2]) == expected
        prelude = message_writes._prepare_import_prelude(
            connection, agent, "one", [], guard_carry=True, session_rows=session_rows)
        assert _asks(seen) == (0, 0)
        assert (prelude.client_rows, prelude.user_provenance) == expected
        assert prelude.client_user_provenance == common._client_provenance(expected[0])
        again = message_writes._revalidate_import_prelude(connection, agent, "one", prelude)
        assert _asks(seen) == (0, 0)
        assert (again.client_rows, again.user_provenance) == expected
        assert again.client_user_provenance is prelude.client_user_provenance
        assert again.turn_index is prelude.turn_index
        # Without the rows (a full import's own preparation) it asks, as before.
        plain = message_writes._prepare_import_prelude(connection, agent, "one", [])
        assert _asks(seen) == (1, 1)
        assert (plain.client_rows, plain.user_provenance) == expected
        message_writes._revalidate_import_prelude(connection, agent, "one", plain)
        assert _asks(seen) == (2, 2)
    finally:
        connection.set_trace_callback(None)


def test_a_send_row_written_between_preparation_and_the_lock_is_seen_from_the_rows(tmp_path):
    """Revalidation exists to see what changed meanwhile; taken from the rows
    it sees a new /send row and a new latest user row all the same."""
    agent = _agent(tmp_path)
    connection = db.conn()
    _insert(connection, agent, _generated(11))
    prelude = message_writes._prepare_import_prelude(
        connection, agent, "one", [], guard_carry=True, session_rows=True)
    _insert(connection, agent, [
        {"message_id": "m-new", "session": "one", "source": "client:new", "seq": 900,
         "role": "user", "timestamp": None, "text": "words nobody said", "updated_at": 9000,
         "revision": 50, "origin": "heartbeat", "sender": None}])
    again = message_writes._revalidate_import_prelude(connection, agent, "one", prelude)
    assert again.client_rows[-1][0] == "m-new"
    assert again.user_provenance == ("m-new", "heartbeat", "")
    assert again.client_user_provenance is not prelude.client_user_provenance
    assert (again.client_rows, again.user_provenance) == _asked(connection, agent, "one")
    assert isinstance(again.turn_index, message_turns.TurnIndex)
