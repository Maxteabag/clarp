"""A restart or uncertain native answer must not release rotation ownership."""
from __future__ import annotations

import pytest
import sqlite3
import threading
import json

from lib import agents, db, native_rotation_journal as journal, settings_store


def _agent(tmp_path):
    agent = agents.create_agent(persona="Rotation owner", voice_id="v", cwd=str(tmp_path),
                                session="rotation-owner", backend="codex")
    agents.start_runtime(agent, "rotation-owner")
    agents.bind_backend_session(agent, "old")
    return agent


def test_reservation_is_expected_binding_idempotent_and_survives_fresh_read(tmp_path):
    agent = _agent(tmp_path)
    first = journal.reserve(agent, "old", "operation-a")
    assert first["phase"] == "reserved" and journal.blocked(agent)
    assert journal.reserve(agent, "old", "operation-a") == first
    with pytest.raises(journal.RotationConflict):
        journal.reserve(agent, "old", "operation-b")
    # A separate thread-local connection reads the committed reservation.
    original = db.conn()
    import sqlite3
    other = sqlite3.connect(original.execute("PRAGMA database_list").fetchone()[2])
    try:
        raw = other.execute("SELECT value FROM settings WHERE key=?", (journal.key(agent),)).fetchone()
        assert raw is not None and '"operation-a"' in raw[0]
    finally:
        other.close()
    with pytest.raises(journal.RotationConflict):
        agents.bind_backend_session(agent, "replacement")
    assert agents.live_backend_session(agent) == "old"
    # An older/external writer can still bypass the new canonical boundary.
    # Its mutation must invalidate the existing operation, never retarget it.
    agents.conn().execute("UPDATE runtimes SET backend_session_id='replacement' WHERE runtime_id=?",
                          (first["runtime_id"],))
    with pytest.raises(journal.RotationConflict):
        journal.advance(agent, "operation-a", "reserved", "quiescing")
    assert journal.get(agent) == first and journal.blocked(agent)


def test_effect_intents_are_durable_and_cannot_be_blindly_repeated(tmp_path):
    agent = _agent(tmp_path)
    journal.reserve(agent, "old", "operation-a")
    for before, after in (("reserved", "quiescing"), ("quiescing", "quiescent"),
                          ("quiescent", "create_pending")):
        journal.advance(agent, "operation-a", before, after)
    intent = journal.get(agent)
    assert intent["phase"] == "create_pending" and journal.blocked(agent)
    with pytest.raises(journal.RotationConflict):
        journal.advance(agent, "operation-a", "quiescent", "create_pending")
    with pytest.raises(journal.RotationConflict):
        journal.reserve(agent, "old", "operation-b")
    assert journal.get(agent) == intent
    # Only a correlated result, not another create, supplies the new identity.
    journal.advance(agent, "operation-a", "create_pending", "created", new_thread_id="new")
    with pytest.raises(journal.RotationConflict):
        journal.advance(agent, "operation-a", "created", "complete")
    assert journal.blocked(agent)


def test_canonical_binding_and_phase_commit_together_or_rollback(tmp_path, monkeypatch):
    agent = _agent(tmp_path)
    journal.reserve(agent, "old", "operation-a")
    for before, after in (("reserved", "quiescing"), ("quiescing", "quiescent"),
                          ("quiescent", "create_pending"), ("create_pending", "created"),
                          ("created", "final_imported"), ("final_imported", "chats_carried"),
                          ("chats_carried", "plans_carried")):
        journal.advance(agent, "operation-a", before, after,
                        **({"new_thread_id": "new"} if after == "created" else {}))
    before = journal.get(agent)
    original = settings_store.set_text
    failure = RuntimeError("durable phase write denied")

    def denied(key, value, **kwargs):
        if key == journal.key(agent):
            raise failure
        return original(key, value, **kwargs)

    monkeypatch.setattr(settings_store, "set_text", denied)
    with pytest.raises(RuntimeError) as caught:
        journal.bind(agent, "operation-a")
    assert caught.value is failure
    assert agents.live_backend_session(agent) == "old"
    assert journal.get(agent) == before
    monkeypatch.setattr(settings_store, "set_text", original)
    bound = journal.bind(agent, "operation-a")
    assert agents.live_backend_session(agent) == "new" and bound["phase"] == "bound"
    assert journal.bind(agent, "operation-a") == bound and journal.blocked(agent)


def test_malformed_or_replaced_runtime_cannot_be_treated_as_no_rotation(tmp_path):
    agent = _agent(tmp_path)
    journal.reserve(agent, "old", "operation-a")
    # Native-ID equality is insufficient after an owning runtime replacement.
    original_runtime = agents.current_runtime_id(agent)
    agents.conn().execute("UPDATE runtimes SET ended_at=1 WHERE runtime_id=?", (original_runtime,))
    agents.conn().execute("INSERT INTO runtimes(agent_id,session,backend_session_id,started_at) "
                          "VALUES (?,?,'old',?)", (agent, "rotation-owner", db.now_ms()))
    with pytest.raises(journal.RotationConflict):
        journal.advance(agent, "operation-a", "reserved", "quiescing")
    settings_store.set_text(journal.key(agent), "not-json")
    with pytest.raises(journal.RotationConflict):
        journal.blocked(agent)
    with pytest.raises(journal.RotationConflict):
        journal.reserve(agent, "old", "operation-b")


@pytest.mark.parametrize("operation_id", ["", "ü", "x" * 129, "bad\nintent", "bad/intent"])
def test_operation_id_is_bounded_ascii_and_cannot_open_an_alias_intent(tmp_path, operation_id):
    agent = _agent(tmp_path)
    with pytest.raises(journal.RotationConflict):
        journal.reserve(agent, "old", operation_id)
    assert journal.get(agent) is None


@pytest.mark.parametrize("mutation", ["bind", "runtime", "delete"])
@pytest.mark.parametrize("joined", [False, True])
def test_reserved_owner_refuses_canonical_mutation_before_any_durable_effect(tmp_path, mutation, joined):
    agent = _agent(tmp_path)
    reserved = journal.reserve(agent, "old", "operation-a")
    con = db.conn()
    runtimes = [dict(row) for row in con.execute("SELECT * FROM runtimes WHERE agent_id=?", (agent,))]
    owner = agents.get_by_agent_id(agent)
    if joined:
        con.execute("BEGIN IMMEDIATE")
        settings_store.set_text("caller-unit", "keep")
    with pytest.raises(journal.RotationConflict):
        if mutation == "bind":
            agents.bind_backend_session(agent, "foreign")
        elif mutation == "runtime":
            agents.start_runtime(agent, "replacement")
        else:
            agents.soft_delete(agent)
    assert con.in_transaction is joined
    if joined:
        con.execute("COMMIT")
        assert settings_store.get("caller-unit") == "keep"
    assert agents.get_by_agent_id(agent) == owner
    assert [dict(row) for row in con.execute("SELECT * FROM runtimes WHERE agent_id=?", (agent,))] == runtimes
    assert journal.get(agent) == reserved


def test_old_native_resource_remains_reserved_after_binding_cutover(tmp_path):
    agent = _agent(tmp_path)
    journal.reserve(agent, "old", "operation-a")
    for before, after in (("reserved", "quiescing"), ("quiescing", "quiescent"),
                          ("quiescent", "create_pending"), ("create_pending", "created"),
                          ("created", "final_imported"), ("final_imported", "chats_carried"),
                          ("chats_carried", "plans_carried")):
        journal.advance(agent, "operation-a", before, after,
                        **({"new_thread_id": "new"} if after == "created" else {}))
    journal.bind(agent, "operation-a")
    other = agents.create_agent(persona="Other", voice_id="v", cwd=str(tmp_path),
                                 session="other", backend="codex")
    # No live row owns old anymore, so the ordinary live-UUID unique index
    # cannot provide this protection. Refuse before implicit runtime creation.
    assert agents.get_by_backend_session("old") is None
    with pytest.raises(journal.RotationConflict):
        agents.bind_backend_session(other, "old")
    assert agents.current_runtime_id(other) is None
    # An unrelated native identity continues using the normal route.
    agents.bind_backend_session(other, "unrelated")
    assert agents.live_backend_session(other) == "unrelated"


def test_new_native_collision_keeps_the_create_intent_and_old_reservation(tmp_path):
    agent = _agent(tmp_path)
    other = agents.create_agent(persona="Other", voice_id="v", cwd=str(tmp_path),
                                 session="other", backend="codex")
    agents.bind_backend_session(other, "foreign")
    journal.reserve(agent, "old", "operation-a")
    for before, after in (("reserved", "quiescing"), ("quiescing", "quiescent"),
                          ("quiescent", "create_pending")):
        journal.advance(agent, "operation-a", before, after)
    intent = journal.get(agent)
    with pytest.raises(journal.RotationConflict):
        journal.advance(agent, "operation-a", "create_pending", "created", new_thread_id="foreign")
    assert journal.get(agent) == intent
    assert agents.live_backend_session(agent) == "old"
    assert agents.live_backend_session(other) == "foreign"


def test_canonical_writer_waits_for_reservation_then_observes_its_committed_owner(tmp_path, monkeypatch):
    agent = _agent(tmp_path)
    admitted, release = threading.Event(), threading.Event()
    errors = []
    actual_write = settings_store.set_text

    def held_write(key, value, **kwargs):
        if key == journal.key(agent):
            admitted.set()
            if not release.wait(5):
                raise RuntimeError("controlled reservation writer did not release")
        return actual_write(key, value, **kwargs)

    monkeypatch.setattr(settings_store, "set_text", held_write)

    def reserve():
        try:
            journal.reserve(agent, "old", "operation-a")
        except BaseException as exc:
            errors.append(exc)
        finally:
            db.close_local()

    worker = threading.Thread(target=reserve, name="rotation-reservation")
    worker.start()
    con = db.conn()
    timeout = con.execute("PRAGMA busy_timeout").fetchone()[0]
    try:
        assert admitted.wait(5), "real reservation writer was not admitted"
        con.execute("PRAGMA busy_timeout=1")
        with pytest.raises(sqlite3.OperationalError) as caught:
            agents.start_runtime(agent, "replacement")
        assert caught.value.sqlite_errorcode == sqlite3.SQLITE_BUSY
        release.set()
        worker.join(5)
        assert not worker.is_alive() and errors == []
        with pytest.raises(journal.RotationConflict):
            agents.start_runtime(agent, "replacement")
        assert agents.live_backend_session(agent) == "old"
    finally:
        release.set()
        worker.join(5)
        con.execute(f"PRAGMA busy_timeout={int(timeout)}")


@pytest.mark.parametrize("joined", [False, True])
def test_resource_cleanup_and_completion_roll_back_together_on_second_delete_failure(tmp_path, joined):
    agent = _agent(tmp_path)
    journal.reserve(agent, "old", "operation-a")
    phases = ("reserved", "quiescing", "quiescent", "create_pending", "created",
              "final_imported", "chats_carried", "plans_carried")
    for before, after in zip(phases, phases[1:]):
        journal.advance(agent, "operation-a", before, after,
                        **({"new_thread_id": "new"} if after == "created" else {}))
    journal.bind(agent, "operation-a")
    phases = ("bound", "archive_pending", "archived", "goal_pending", "ready")
    for before, after in zip(phases, phases[1:]):
        journal.advance(agent, "operation-a", before, after)
    con = db.conn()
    before = [tuple(row) for row in con.execute("SELECT key,value FROM settings ORDER BY key")]
    resources = [(key, json.loads(raw)) for key, raw in settings_store.items_in_range(
        "codex.rotation.v1.thread.", "codex.rotation.v1.thread/", limit=9)]
    assert len(resources) == 2
    new_resource = next(key for key, value in resources if value["native_id"] == "new")
    # Actual statement failure AFTER the old resource delete succeeds. An
    # outer owner must not be able to commit that first delete by catching it.
    con.execute("CREATE TEMP TRIGGER deny_new_resource BEFORE DELETE ON settings "
                "WHEN OLD.key='" + new_resource + "' "
                "BEGIN SELECT RAISE(ABORT,'new resource delete denied'); END")
    if joined:
        con.execute("BEGIN IMMEDIATE")
        settings_store.set_text("caller-unit", "keep")
    with pytest.raises(sqlite3.IntegrityError, match="new resource delete denied"):
        journal.advance(agent, "operation-a", "ready", "complete")
    assert con.in_transaction is joined
    if joined:
        con.execute("COMMIT")
        assert settings_store.get("caller-unit") == "keep"
    assert [tuple(row) for row in con.execute(
        "SELECT key,value FROM settings WHERE key<>'caller-unit' ORDER BY key")] == before
    con.execute("DROP TRIGGER deny_new_resource")
    completed = journal.advance(agent, "operation-a", "ready", "complete")
    assert not journal.blocked(agent)
    assert journal.reserve(agent, "old", "operation-a") == completed
    # Both reservations were released by the same completion, including old
    # which no longer had a live runtime owner after cutover.
    other = agents.create_agent(persona="Other", voice_id="v", cwd=str(tmp_path),
                                 session="other", backend="codex")
    agents.bind_backend_session(other, "old")
    assert agents.live_backend_session(other) == "old"


def test_rotation_capacity_refuses_an_extra_owner_without_partial_reservation(tmp_path):
    for index in range(5):
        agent = agents.create_agent(persona=f"Owner {index}", voice_id="v", cwd=str(tmp_path),
                                     session=f"owner-{index}", backend="codex")
        agents.bind_backend_session(agent, f"native-{index}")
        if index < 4:
            journal.reserve(agent, f"native-{index}", f"operation-{index}")
        else:
            with pytest.raises(journal.RotationConflict):
                journal.reserve(agent, f"native-{index}", f"operation-{index}")
            assert journal.get(agent) is None
            assert agents.live_backend_session(agent) == f"native-{index}"
