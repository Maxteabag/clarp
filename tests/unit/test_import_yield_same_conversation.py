"""A foreign commit across an import's yield is counted as this conversation's own or
another writer's (the journal's same_conversation=yes/no). Diagnostic only: the
publication is refused either way, as before.

Two connections to one SQLite file stand for the import and the other writer."""
from __future__ import annotations

import sqlite3

import pytest

from lib import message_store
from lib.message_import_writer import _ImportWriter


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(message_store, "IMPORT_WRITER_YIELD_SECONDS", 0)   # the writer's facade
    monkeypatch.setattr(message_store, "IMPORT_FAIR_YIELD_SECONDS", 0)
    path = tmp_path / "store.sqlite"
    with sqlite3.connect(path) as setup:
        setup.execute("PRAGMA journal_mode=WAL")
        setup.execute("CREATE TABLE conversation_heads (agent_id TEXT, backend_session_id TEXT, "
                      "revision INTEGER, replace_revision INTEGER, PRIMARY KEY (agent_id, backend_session_id))")
        setup.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
        setup.execute("INSERT INTO conversation_heads VALUES ('agent', 'session', 1, 0)")
        setup.execute("INSERT INTO conversation_heads VALUES ('agent', 'other', 1, 0)")
    return path


def _yield_across(store, foreign_sql, *, head_key=("agent", "session")):
    own = sqlite3.connect(store, isolation_level=None)
    writer = _ImportWriter(own, locked=True)
    writer.head_key = head_key
    writer.execute("UPDATE conversation_heads SET revision = 2 WHERE agent_id = 'agent' "
                   "AND backend_session_id = 'session'")
    writer.commit()
    writer.yield_writer()
    with sqlite3.connect(store) as other:
        other.execute(foreign_sql)
    writer.execute("SELECT 1")                      # reacquires: the fence sees the commit
    status = {}
    assert writer.checkpoint_allowed(status) is False
    own.execute("ROLLBACK") if own.in_transaction else None
    own.close()
    return status


def test_a_commit_that_moved_this_conversations_head_is_its_own(store):
    status = _yield_across(store, "UPDATE conversation_heads SET revision = 3 WHERE agent_id = 'agent' "
                                  "AND backend_session_id = 'session'")
    assert status == {"refused": ("foreign_commit_yield",), "same_conversation": True}


def test_another_sessions_commit_is_another_writers(store):
    status = _yield_across(store, "UPDATE conversation_heads SET revision = 3 WHERE agent_id = 'agent' "
                                  "AND backend_session_id = 'other'")
    assert status == {"refused": ("foreign_commit_yield",), "same_conversation": False}


def test_without_a_conversation_nothing_is_said(store):
    status = _yield_across(store, "INSERT INTO settings VALUES ('k', 'v')", head_key=None)
    assert status == {"refused": ("foreign_commit_yield",)}


def test_the_journal_line_carries_it(monkeypatch):
    from lib import log as host_log
    from lib import transcript_incremental as inc
    seen = []
    monkeypatch.setattr(host_log, "log", lambda event, detail="": seen.append((event, detail)))
    inc._checkpoint_refused({"refused": ("foreign_commit_yield",), "same_conversation": True})
    inc._checkpoint_refused({"refused": ("foreign_commit_yield",), "same_conversation": False})
    inc._checkpoint_refused({"refused": ("foreign_commit_yield",)})
    assert seen == [
        ("transcriptTailRetention", "code=checkpoint_foreign_commit_yield count=1 same_conversation=yes"),
        ("transcriptTailRetention", "code=checkpoint_foreign_commit_yield count=1 same_conversation=no"),
        ("transcriptTailRetention", "code=checkpoint_foreign_commit_yield count=1")]
