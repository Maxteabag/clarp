"""Import preparation must yield the writer and reject stale preparation."""
import sqlite3
import time

import pytest

from lib import agents, db, message_store, message_turns, message_writes


def test_large_import_preparation_allows_an_independent_writer(tmp_path, monkeypatch):
    agent = agents.create_agent(persona="Synthetic", voice_id="v", cwd=str(tmp_path), session="synthetic")
    connection = db.conn()
    path = connection.execute("PRAGMA database_list").fetchone()[2]
    other = sqlite3.connect(path, isolation_level=None, timeout=0.02)
    original = message_writes._row_phases
    admissions = []
    holds = []
    began = [None]

    def trace(sql):
        if sql == "BEGIN IMMEDIATE":
            began[0] = time.monotonic()
        elif sql in ("COMMIT", "ROLLBACK") and began[0] is not None:
            holds.append(time.monotonic() - began[0])
            began[0] = None

    def expensive_preparation(turns):
        result = original(turns)
        try:
            other.execute("BEGIN IMMEDIATE")
            admissions.append("admitted")
            other.execute("ROLLBACK")
        except sqlite3.OperationalError as exc:
            admissions.append(exc.sqlite_errorname)
        time.sleep(0.15)  # deterministic stand-in for the large prelude's CPU work
        return result

    monkeypatch.setattr(message_writes, "_row_phases", expensive_preparation)
    turns = [{"id": f"synthetic-{i}", "role": "user", "text": f"row {i}",
              "timestamp": "2026-01-01T00:00:00Z"} for i in range(1000)]
    connection.set_trace_callback(trace)
    try:
        out = message_store.store_transcript_turns(
            agent_id=agent, backend_session_id="fixture", source_file="fixture", turns=turns)
    finally:
        connection.set_trace_callback(None)
        other.close()
    assert admissions and set(admissions) == {"admitted"}
    assert [(r["id"], r["role"], r["text"], r["origin"]) for r in out] == [
        (f"synthetic-{i}", "user", f"row {i}", "user") for i in range(1000)]
    rows = connection.execute(
        "SELECT message_id,seq,role,text,origin FROM messages WHERE agent_id=? ORDER BY seq", (agent,))
    assert [tuple(r) for r in rows] == [
        (f"synthetic-{i}", i, "user", f"row {i}", "user") for i in range(1000)]
    assert holds
    print(f"rows=1000 preparation_calls={len(admissions)} write_batches={len(holds)} "
          f"max_write_ms={max(holds)*1000:.1f}")


def test_concurrent_provenance_change_is_revalidated_before_writing(tmp_path, monkeypatch):
    agent = agents.create_agent(persona="Synthetic", voice_id="v", cwd=str(tmp_path), session="synthetic")
    user = message_store.record_user_message(
        agent_id=agent, backend_session_id="fixture", client_msg_id="client", text="question")
    connection = db.conn()
    path = connection.execute("PRAGMA database_list").fetchone()[2]
    original = message_turns.TurnIndex.__init__
    changes = []

    def change_after_snapshot(self, database, agent_id):
        original(self, database, agent_id)
        if not changes:
            # The turn-index read has pinned the prelude's snapshot. This
            # commit must invalidate it, including client provenance not read yet.
            with sqlite3.connect(path, isolation_level=None, timeout=0) as other:
                other.execute("UPDATE messages SET origin='team',sender_agent_id='sender' WHERE message_id=?",
                              (user["id"],))
            changes.append(1)

    monkeypatch.setattr(message_turns.TurnIndex, "__init__", change_after_snapshot)
    out = message_store.store_transcript_turns(
        agent_id=agent, backend_session_id="fixture", source_file="fixture", turns=[
            {"role": "user", "text": "question", "timestamp": user["timestamp"]},
            {"id": "reply", "role": "assistant", "text": "answer",
             "timestamp": "2026-01-01T00:00:01Z"}])
    assert changes == [1]
    assert out[-1]["origin"] == "team"
    assert out[-1]["sender_agent_id"] == "sender"
    assert tuple(connection.execute(
        "SELECT origin,sender_agent_id FROM messages WHERE message_id='reply'").fetchone()) == ("team", "sender")


def test_repeated_prelude_changes_defer_without_partial_writes(tmp_path, monkeypatch):
    agent = agents.create_agent(persona="Synthetic", voice_id="v", cwd=str(tmp_path), session="synthetic")
    user = message_store.record_user_message(
        agent_id=agent, backend_session_id="fixture", client_msg_id="client", text="question")
    connection = db.conn()
    path = connection.execute("PRAGMA database_list").fetchone()[2]
    original = message_turns.TurnIndex.__init__
    changes = []

    def change_every_snapshot(self, database, agent_id):
        original(self, database, agent_id)
        with sqlite3.connect(path, isolation_level=None, timeout=0) as other:
            other.execute("UPDATE messages SET updated_at=updated_at+1 WHERE message_id=?", (user["id"],))
        changes.append(1)

    monkeypatch.setattr(message_turns.TurnIndex, "__init__", change_every_snapshot)
    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        message_store.store_transcript_turns(
            agent_id=agent, backend_session_id="fixture", source_file="fixture", turns=[
                {"id": "new", "role": "assistant", "text": "answer",
                 "timestamp": "2026-01-01T00:00:01Z"}])
    assert 1 < len(changes) <= 4
    assert not connection.in_transaction
    assert not getattr(db._LOCAL, "import_batch", False)
    assert connection.execute("SELECT count(*) FROM messages WHERE message_id='new'").fetchone()[0] == 0
