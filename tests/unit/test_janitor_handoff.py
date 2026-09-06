"""Explicit status-maintenance handoff preserves receipt provenance atomically."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager

import pytest

from lib import agents, backends, db, janitors, turn_queue
from lib.janitor_context import build_context_from_connection


@pytest.fixture(autouse=True)
def no_processes(monkeypatch):
    monkeypatch.setattr(backends, "active_handles", lambda *args: [])


def setup_handoff():
    identities = {}
    for session in ("sam", "rivet", "worker", "edited", "manual"):
        identities[session] = agents.create_agent(persona=session.title(), voice_id="original-voice", cwd="/tmp",
            session=session, backend="codex", model="gpt-5.3-codex-spark", effort="low")
        agents.record_state(identities[session], "done", {"origin": "user"})
        if session not in {"sam", "rivet"}:
            db.conn().execute("""INSERT INTO task_plans(plan_id,agent_id,session,title,status,created_at,updated_at)
                VALUES (?,?,?,'Improve current feature','active',1,1)""", ("plan-" + session, identities[session], session))
    source = janitors.create("sam")
    source = janitors.set_enabled("sam", source["revision"], True)
    candidates = [build_context_from_connection(db.conn(), s) for s in ("worker", "edited", "manual")]
    run = janitors.create_run(source["attachments"][0]["attachment_id"], source["generation"], candidates)
    for candidate in candidates:
        janitors.review(run["run_id"], candidate["session"], candidate["state_id"], "changed", label="Current feature")
    janitors.finish_run(run["run_id"])
    source = janitors.set_enabled("sam", source["revision"], False)
    successor = janitors.create("rivet")
    db.conn().execute("UPDATE agents SET custom_status='User changed text' WHERE agent_id=?", (identities["edited"],))
    agents.set_custom_status(identities["manual"], "Current feature")
    return identities, source, successor, run["run_id"]


def runtime_status_that_writes(*_):
    """The split runtime may persist liveness while answering a status query."""
    with sqlite3.connect(db.DB_PATH, timeout=0.01, isolation_level=None) as remote:
        remote.execute("BEGIN IMMEDIATE")
        remote.execute("ROLLBACK")
    return []


def test_handoff_does_not_hold_writer_lock_during_runtime_status(monkeypatch):
    _, source, successor, _ = setup_handoff()
    monkeypatch.setattr(backends, "active_handles", runtime_status_that_writes)
    result = janitors.release("sam", source["revision"], successor_session="rivet", successor_revision=successor["revision"])
    assert not result["is_janitor"]
    assert result["ownership_handoff"]["transferred_count"] == 1


def test_conversion_does_not_hold_writer_lock_during_runtime_status(monkeypatch):
    agents.create_agent(persona="Sam", voice_id="", cwd="/tmp", session="sam")
    monkeypatch.setattr(backends, "active_handles", runtime_status_that_writes)
    assert janitors.create("sam")["is_janitor"]


def test_label_review_does_not_hold_writer_lock_during_runtime_status(monkeypatch):
    _, source, _, _ = setup_handoff()
    source = janitors.set_enabled("sam", source["revision"], True)
    context = build_context_from_connection(db.conn(), "worker")
    run = janitors.create_run(source["attachments"][0]["attachment_id"], source["generation"], [context])
    monkeypatch.setattr(backends, "active_handles", runtime_status_that_writes)
    result = janitors.review(run["run_id"], "worker", context["state_id"], "same_task")
    assert result["outcome"] == "same_task"


def snapshot():
    c = db.conn()
    return {table: [tuple(row) for row in c.execute(f"SELECT * FROM {table} ORDER BY rowid")]
            for table in ("agents", "janitor_configs", "janitor_attachments", "janitor_runs", "janitor_effects", "janitor_label_ownership", "state_log")}


def test_handoff_transfers_only_matching_owned_labels_and_preserves_receipts():
    identities, source, successor, run_id = setup_handoff()
    original = snapshot()
    owned = dict(db.conn().execute("SELECT * FROM janitor_label_ownership WHERE target_agent_id=?", (identities["worker"],)).fetchone())
    released = janitors.release("sam", source["revision"], successor_session="rivet", successor_revision=successor["revision"])
    assert released["ownership_handoff"] == {"successor_agent_id": identities["rivet"], "successor_session": "rivet", "transferred_count": 1}
    assert not released["is_janitor"] and not released["enabled"]
    assert agents.get_by_agent_id(identities["sam"])["archived_at"] is None
    current = janitors.get("rivet")
    assert current["is_janitor"] and not current["enabled"]
    assert current["revision"] == successor["revision"] + 1
    assert current["generation"] == successor["generation"] + 1
    transferred = dict(db.conn().execute("SELECT * FROM janitor_label_ownership WHERE target_agent_id=?", (identities["worker"],)).fetchone())
    assert transferred["owner_agent_id"] == identities["rivet"]
    assert transferred["run_id"] == owned["run_id"] == run_id
    for key in ("label", "task_signature", "valid_until"):
        assert transferred[key] == owned[key]
    assert janitors.get_run(run_id)["agent_id"] == identities["sam"]
    assert snapshot()["janitor_runs"] == original["janitor_runs"]
    assert snapshot()["janitor_effects"] == original["janitor_effects"]
    assert db.conn().execute("SELECT owner_agent_id FROM janitor_label_ownership WHERE target_agent_id=?", (identities["edited"],)).fetchone()[0] == identities["sam"]
    assert not db.conn().execute("SELECT 1 FROM janitor_label_ownership WHERE target_agent_id=?", (identities["manual"],)).fetchone()
    assert agents.get_by_agent_id(identities["edited"])["custom_status"] == "User changed text"
    audits = [json.loads(row[0]) for row in db.conn().execute("SELECT detail FROM state_log WHERE json_extract(detail,'$.event')='janitor_ownership_handoff'")]
    assert audits == [{"origin": "janitor", "event": "janitor_ownership_handoff", "source_agent_id": identities["sam"],
                       "successor_agent_id": identities["rivet"], "transferred_count": 1}]
    assert agents.latest_state(identities["sam"])["kind"] == "done"
    # Rivet can now maintain the transferred text through its own new receipt.
    current = janitors.set_enabled("rivet", current["revision"], True)
    candidate = build_context_from_connection(db.conn(), "worker")
    new_run = janitors.create_run(current["attachments"][0]["attachment_id"], current["generation"], [candidate])
    janitors.review(new_run["run_id"], "worker", candidate["state_id"], "changed", label="Fresh feature")
    assert agents.get_by_agent_id(identities["worker"])["custom_status"] == "Fresh feature"
    assert janitors.get_run(run_id)["results"][0]["after"] == "Current feature"


@pytest.mark.parametrize("failure", ["source_revision", "successor_revision", "source_enabled", "successor_enabled", "source_busy", "successor_busy", "successor_handles", "successor_queued", "scope", "template", "same_agent", "archived", "released", "missing_revision"])
def test_invalid_handoff_has_no_partial_changes(failure, monkeypatch):
    identities, source, successor, _ = setup_handoff()
    source_revision, successor_revision, successor_session = source["revision"], successor["revision"], "rivet"
    if failure == "source_revision":
        source_revision -= 1
    elif failure == "successor_revision":
        successor_revision -= 1
    elif failure.endswith("_enabled"):
        session = "sam" if failure.startswith("source") else "rivet"
        config = janitors.get(session)
        config = janitors.set_enabled(session, config["revision"], True)
        if session == "sam": source_revision = config["revision"]
        else: successor_revision = config["revision"]
    elif failure.endswith("_busy"):
        agents.record_state(identities["sam" if failure.startswith("source") else "rivet"], "thinking")
    elif failure == "successor_handles":
        monkeypatch.setattr(backends, "active_handles", lambda backend, aid: ["active"] if aid == identities["rivet"] else [])
    elif failure == "successor_queued":
        turn_queue.enqueue(queue_id="q", agent_id=identities["rivet"], session="rivet", text="Pending work", trace_id="q",
            client_msg_id="q", synthesize_audio=False, origin="janitor", sender_agent_id="")
    elif failure in {"scope", "template"}:
        changes = {"scope": {"agent_ids": [identities["worker"]]}} if failure == "scope" else {"template_id": "tool-explainer"}
        successor_revision = janitors.configure("rivet", successor_revision, **changes)["revision"]
    elif failure == "same_agent":
        successor_session, successor_revision = "sam", source_revision
    elif failure == "archived":
        agents.set_archived(identities["rivet"], True)
    elif failure == "released":
        successor_revision = janitors.release("rivet", successor_revision)["revision"]
    elif failure == "missing_revision":
        successor_revision = None
    before = snapshot()
    with pytest.raises(janitors.JanitorError):
        janitors.release("sam", source_revision, successor_session=successor_session, successor_revision=successor_revision)
    assert snapshot() == before


def test_transfer_and_release_roll_back_when_audit_cannot_be_written():
    _, source, successor, _ = setup_handoff()
    db.conn().execute("""CREATE TRIGGER reject_handoff_audit BEFORE INSERT ON state_log
        WHEN json_extract(NEW.detail,'$.event')='janitor_ownership_handoff'
        BEGIN SELECT RAISE(ABORT,'audit unavailable'); END""")
    before = snapshot()
    with pytest.raises(Exception, match="audit unavailable"):
        janitors.release("sam", source["revision"], successor_session="rivet", successor_revision=successor["revision"])
    assert snapshot() == before


def test_equivalent_scope_shapes_allow_transfer_but_out_of_scope_ownership_stays():
    identities, source, successor, _ = setup_handoff()
    scope = {"agent_ids": [], "exclude_agent_ids": [identities["worker"]]}
    source = janitors.configure("sam", source["revision"], scope=scope)
    successor = janitors.configure("rivet", successor["revision"], scope={"exclude_agent_ids": [identities["worker"]]})
    released = janitors.release("sam", source["revision"], successor_session="rivet", successor_revision=successor["revision"])
    assert released["ownership_handoff"]["transferred_count"] == 0
    assert db.conn().execute("SELECT owner_agent_id FROM janitor_label_ownership WHERE target_agent_id=?", (identities["worker"],)).fetchone()[0] == identities["sam"]


def test_handoff_rechecks_source_identity_after_acquiring_write_transaction(monkeypatch):
    identities, source, successor, _ = setup_handoff()
    original_write = janitors._write
    @contextmanager
    def archive_before_transaction():
        agents.set_archived(identities["sam"], True)
        with original_write() as c:
            yield c
    monkeypatch.setattr(janitors, "_write", archive_before_transaction)
    with pytest.raises(janitors.JanitorError, match="unarchived"):
        janitors.release("sam", source["revision"], successor_session="rivet", successor_revision=successor["revision"])
    assert agents.get_by_agent_id(identities["sam"])["is_janitor"]
    assert db.conn().execute("SELECT owner_agent_id FROM janitor_label_ownership WHERE target_agent_id=?", (identities["worker"],)).fetchone()[0] == identities["sam"]
