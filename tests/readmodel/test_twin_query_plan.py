"""Sparse trace lookups must not walk the rest of a long conversation."""
from __future__ import annotations

import sqlite3

import pytest

from lib import db_schema, message_writes


class RecordedConnection(sqlite3.Connection):
    def execute(self, sql, parameters=()):
        self.last_statement = (sql, parameters)
        return super().execute(sql, parameters)


@pytest.fixture
def database():
    connection = sqlite3.connect(":memory:", factory=RecordedConnection)
    connection.row_factory = sqlite3.Row
    connection.executescript(db_schema._SCHEMA_SQL)
    yield connection
    connection.close()


def insert(database, message_id, seq, *, trace="target", agent="agent",
           session="session", source="transcript", role="assistant", text="answer"):
    database.execute(
        "INSERT INTO messages(message_id,agent_id,backend_session_id,source_file,seq,"
        "role,text,updated_at,trace_id) VALUES (?,?,?,?,?,?,?,0,?)",
        (message_id, agent, session, source, seq, role, text, trace))


def lookup(database, *, exclude=frozenset()):
    return message_writes._shifted_twin(
        database, "agent", "session", "transcript", "target", "answer", 10,
        exclude=exclude)


@pytest.mark.parametrize("analysed", [False, True])
def test_shifted_twin_searches_trace_instead_of_unrelated_history(database, analysed):
    insert(database, "adopted", 11)
    database.executemany(
        "INSERT INTO messages(message_id,agent_id,backend_session_id,source_file,seq,"
        "role,text,updated_at,trace_id) VALUES (?,'agent','session','transcript',?,"
        "'assistant','unrelated',0,?)",
        [(f"other-{i}", i + 100, f"other-trace-{i}") for i in range(2000)])
    if analysed:
        database.execute("ANALYZE")
    assert lookup(database) == ("adopted", 11)
    sql, bindings = database.last_statement
    details = [row[3] for row in database.execute("EXPLAIN QUERY PLAN " + sql, bindings)]
    assert any("SEARCH messages" in detail and "idx_messages_trace" in detail
               and "trace_id=?" in detail for detail in details), details
    assert not any("SCAN messages" in detail or "idx_messages_agent_seq" in detail
                   for detail in details), details


def test_sparse_shifted_twin_vm_work_does_not_grow_with_unrelated_history(database):
    insert(database, "adopted", 11)

    def instructions():
        steps = 0

        def progress():
            nonlocal steps
            steps += 1
            return 0

        database.set_progress_handler(progress, 1)
        try:
            assert lookup(database) == ("adopted", 11)
        finally:
            database.set_progress_handler(None, 0)
        return steps

    small = instructions()
    database.executemany(
        "INSERT INTO messages(message_id,agent_id,backend_session_id,source_file,seq,"
        "role,text,updated_at,trace_id) VALUES (?,'agent','session','transcript',?,"
        "'assistant','unrelated',0,?)",
        [(f"other-{i}", i + 100, f"other-trace-{i}") for i in range(4000)])
    large = instructions()
    assert large <= small * 2, (small, large, sqlite3.sqlite_version)


@pytest.mark.parametrize("excluded, expected", [
    (set(), None), ({"adopted-2"}, ("adopted-1", 11)),
    ({"adopted-1", "adopted-2"}, None),
])
def test_shifted_twin_keeps_all_filters_and_exactly_one_rule(database, excluded, expected):
    insert(database, "adopted-1", 11, text="<speak>answer</speak>")
    insert(database, "adopted-2", 12)
    insert(database, "equal-seq", 10)
    insert(database, "earlier", 9)
    insert(database, "wrong-trace", 13, trace="other")
    insert(database, "wrong-agent", 14, agent="other")
    insert(database, "wrong-session", 15, session="other")
    insert(database, "wrong-source", 16, source="other")
    insert(database, "wrong-role", 17, role="user")
    insert(database, "wrong-text", 18, text="different")
    insert(database, message_writes._message_id("agent", "session", "transcript", 19), 19)
    assert lookup(database, exclude=excluded) == expected
