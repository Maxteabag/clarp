"""Import preparation must yield the writer and reject stale preparation."""
import sqlite3
import time

from lib import agents, db, message_store, message_turns, message_writes


def test_large_import_preparation_allows_an_independent_writer(tmp_path, monkeypatch):
    agent = agents.create_agent(persona="Synthetic", voice_id="v", cwd=str(tmp_path), session="synthetic")
    connection = db.conn()
    path = connection.execute("PRAGMA database_list").fetchone()[2]
    connection.executemany(
        "INSERT INTO turns(agent_id,source,trace_id,started_at,settled_at,outcome) "
        "VALUES(?,'synthetic',?,?,?,'completed')",
        [(agent, f"trace-{i}", i, i) for i in range(1000)])
    connection.executemany(
        "INSERT INTO messages(message_id,agent_id,backend_session_id,source_file,seq,role,text,updated_at,revision,origin) "
        "VALUES(?,?,'fixture','client:synthetic',?,'user',?,1,1,'user')",
        [(f"client-{i}", agent, -i-1, f"unrelated question {i}") for i in range(200)])
    other = sqlite3.connect(path, isolation_level=None, timeout=0.02)
    original = message_writes._row_phases
    original_validate = message_writes._revalidate_import_prelude
    validation_ms = []

    def timed_validation(*args, **kwargs):
        started = time.monotonic()
        result = original_validate(*args, **kwargs)
        validation_ms.append((time.monotonic() - started) * 1000)
        return result

    monkeypatch.setattr(message_writes, "_revalidate_import_prelude", timed_validation)
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
        "SELECT message_id,seq,role,text,origin FROM messages WHERE agent_id=? AND source_file='fixture' ORDER BY seq", (agent,))
    assert [tuple(r) for r in rows] == [
        (f"synthetic-{i}", i, "user", f"row {i}", "user") for i in range(1000)]
    assert holds
    assert len(validation_ms) == 1
    print(f"rows=1000 turn_inputs=1000 client_inputs=200 preparation_calls={len(admissions)} "
          f"validation_ms={validation_ms[0]:.1f} write_batches={len(holds)} "
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


def test_continuous_churn_still_succeeds_without_another_probe(tmp_path, monkeypatch):
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
    out = message_store.store_transcript_turns(
        agent_id=agent, backend_session_id="fixture", source_file="fixture", turns=[
            {"id": "new", "role": "assistant", "text": "answer",
             "timestamp": "2026-01-01T00:00:01Z"}])
    assert [(row["id"], row["text"]) for row in out] == [("new", "answer")]
    assert len(changes) == 1
    assert not connection.in_transaction
    assert not getattr(db._LOCAL, "import_batch", False)
    assert connection.execute("SELECT count(*) FROM messages WHERE message_id='new'").fetchone()[0] == 1


def test_unrelated_sse_commit_does_not_recompute_or_refuse_import(tmp_path, monkeypatch):
    agent = agents.create_agent(persona="Synthetic", voice_id="v", cwd=str(tmp_path), session="synthetic")
    path = db.conn().execute("PRAGMA database_list").fetchone()[2]
    original_store = message_writes._store_transcript_turns_txn
    original_index = message_turns.TurnIndex.__init__
    original_provenance = message_writes._client_provenance
    preparations = []
    normalizations = []

    def count_normalization(rows):
        normalizations.append(1)
        return original_provenance(rows)

    def count_preparation(self, database, agent_id):
        preparations.append(1)
        original_index(self, database, agent_id)

    def unrelated_commit(database, **kwargs):
        try:
            return original_store(database, **kwargs)
        finally:
            if not database.locked:
                with sqlite3.connect(path, isolation_level=None, timeout=0) as other:
                    other.execute("INSERT INTO sse_events(ts,type,payload) VALUES(1,'synthetic','{}')")

    monkeypatch.setattr(message_turns.TurnIndex, "__init__", count_preparation)
    monkeypatch.setattr(message_writes, "_client_provenance", count_normalization)
    monkeypatch.setattr(message_writes, "_store_transcript_turns_txn", unrelated_commit)
    out = message_store.store_transcript_turns(
        agent_id=agent, backend_session_id="fixture", source_file="fixture", turns=[
            {"id": "reply", "role": "assistant", "text": "answer",
             "timestamp": "2026-01-01T00:00:01Z"}])
    assert preparations == [1]
    assert normalizations == [1]
    assert [(r["id"], r["text"]) for r in out] == [("reply", "answer")]
    assert db.conn().execute("SELECT count(*) FROM sse_events WHERE type='synthetic'").fetchone()[0] == 1


def test_changed_turn_falls_back_once_and_matches_lock_only_import(tmp_path, monkeypatch):
    agent = agents.create_agent(persona="Synthetic", voice_id="v", cwd=str(tmp_path), session="synthetic")
    monkeypatch.setattr(message_writes, "now_ms", lambda: 1770000000000)
    connection = db.conn()
    connection.execute(
        "INSERT INTO turns(agent_id,source,trace_id,started_at,settled_at,outcome) "
        "VALUES(?,'synthetic','synthetic-trace',1767225600000,1767225602000,'completed')", (agent,))
    path = connection.execute("PRAGMA database_list").fetchone()[2]
    reference = sqlite3.connect(":memory:", isolation_level=None)
    reference.row_factory = sqlite3.Row
    original = message_turns.TurnIndex.__init__
    changes = []

    def change_after_snapshot(self, database, agent_id):
        original(self, database, agent_id)
        if not changes:
            with sqlite3.connect(path, isolation_level=None, timeout=0) as other:
                other.execute("UPDATE turns SET outcome='interrupted' WHERE agent_id=?", (agent,))
                other.backup(reference)
            changes.append(1)

    monkeypatch.setattr(message_turns.TurnIndex, "__init__", change_after_snapshot)
    turns = [{"id": "reply", "role": "assistant", "text": "answer",
              "timestamp": "2026-01-01T00:00:01Z",
              "tools": [{"name": "synthetic", "status": "running"}]}]
    try:
        out = message_store.store_transcript_turns(
            agent_id=agent, backend_session_id="fixture", source_file="fixture", turns=turns)
        assert changes == [1]
        assert out[0]["tools"][0]["status"] == "interrupted"
        # The retained importer with an ordinary locked writer is the oracle:
        # same inputs as the post-change DB, no prepared prelude supplied.
        writer = message_writes._ImportWriter(reference, locked=True)
        expected = message_writes._store_transcript_turns_txn(
            writer, agent_id=agent, backend_session_id="fixture", source_file="fixture",
            turns=turns, visible_replies=message_writes._visible_replies(turns))
        writer.commit()
        assert out == expected
        query = "SELECT * FROM messages ORDER BY message_id"
        assert [tuple(row) for row in connection.execute(query)] == [tuple(row) for row in reference.execute(query)]
    finally:
        reference.close()
