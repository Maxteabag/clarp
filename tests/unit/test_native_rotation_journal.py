"""A restart or uncertain native answer must not release rotation ownership."""
from __future__ import annotations

import pytest

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
    agents.bind_backend_session(agent, "replacement")
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
    agents.start_runtime(agent, "rotation-owner")
    agents.bind_backend_session(agent, "old")
    with pytest.raises(journal.RotationConflict):
        journal.advance(agent, "operation-a", "reserved", "quiescing")
    settings_store.set_text(journal.key(agent), "not-json")
    with pytest.raises(journal.RotationConflict):
        journal.blocked(agent)
    with pytest.raises(journal.RotationConflict):
        journal.reserve(agent, "old", "operation-b")
