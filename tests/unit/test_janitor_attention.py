"""Repeated maintenance faults use one ordinary dismissible diagnostic artifact."""
from __future__ import annotations

import pytest

from lib import agents, artifacts, backends, db, janitors, janitor_attention as attention
from lib.janitor_context import build_context_from_connection


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setattr(backends, "active_handles", lambda *a: [])
    sam = agents.create_agent(persona="Sam", voice_id="", cwd="/tmp", session="sam", backend="codex")
    hugo = agents.create_agent(persona="Hugo", voice_id="", cwd="/tmp", session="hugo", backend="codex")
    agents.record_state(hugo, "done", {"origin": "user", "trace_id": "work"})
    db.conn().execute("""INSERT INTO task_plans(plan_id,agent_id,session,title,status,created_at,updated_at)
        VALUES ('hugo-plan',?,'hugo','Improve Clarp car mode','active',1,1)""", (hugo,))
    config = janitors.create("sam", attachments=[{"trigger_id": "agent-work-completed"}])
    config = janitors.set_enabled("sam", config["revision"], True)
    return sam, hugo, config


def run(setup, outcome="error", error="Model limit reached"):
    sam, hugo, config = setup
    candidate = build_context_from_connection(db.conn(), "hugo")
    created = janitors.create_run(config["attachments"][0]["attachment_id"], config["generation"], [candidate])
    if outcome == "changed":
        janitors.review(created["run_id"], "hugo", candidate["state_id"], "changed", "Car mode UX", "Current task")
    elif outcome == "same_task":
        janitors.review(created["run_id"], "hugo", candidate["state_id"], "same_task", reason="Still appropriate")
    elif outcome in {"insufficient_context", "skipped"}:
        janitors.review(created["run_id"], "hugo", candidate["state_id"], "insufficient_context", reason="Need new evidence")
    janitors.finish_run(created["run_id"], outcome, error if outcome == "error" else "")
    return created


def fail_twice(setup):
    run(setup)
    run(setup)
    assert attention.reconcile() == 1
    return attention.pending()[0]


def test_transient_failure_and_queued_run_do_not_alert(setup):
    run(setup)
    assert attention.reconcile() == 0
    assert attention.pending() == []
    config = setup[2]
    janitors.create_run(config["attachments"][0]["attachment_id"], config["generation"],
                        [build_context_from_connection(db.conn(), "hugo")])
    assert attention.reconcile() == 0
    assert artifacts.list_artifacts(session="sam") == []


def test_repeated_failures_create_one_stable_document_and_no_chat_work(setup):
    before = {table: db.conn().execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
              for table in ("messages", "queued_turns", "decision_deliveries")}
    item = fail_twice(setup)
    assert item["type"] == "document" and item["status"] == "failed"
    assert item["failure_count"] == 2 and item["session"] == "sam"
    original = artifacts.get(item["artifact_id"])
    for _ in range(5):
        assert attention.reconcile() == 0
    assert artifacts.get(item["artifact_id"])["updated_at"] == original["updated_at"]
    assert len(artifacts.list_artifacts(session="sam")) == 1
    assert artifacts.attention(include_questions=True) == []
    assert before == {table: db.conn().execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in before}


def test_new_failure_updates_same_episode_once(setup):
    item = fail_twice(setup)
    latest = run(setup, error="Configuration unavailable")
    assert attention.reconcile() == 1
    updated = attention.pending()[0]
    assert updated["artifact_id"] == item["artifact_id"]
    assert updated["updated_at"] > item["updated_at"]
    assert updated["latest_run_id"] == latest["run_id"]
    assert updated["failure_count"] == 3
    assert "Configuration unavailable" in artifacts.get(item["artifact_id"])["content"]
    assert attention.reconcile() == 0


@pytest.mark.parametrize("dismissal", ["archive", "discard"])
def test_dismissed_episode_stays_dismissed_after_more_failures(setup, dismissal):
    item = fail_twice(setup)
    if dismissal == "archive":
        artifacts.archive(item["artifact_id"], archived=True, expected_updated_at=item["updated_at"])
    else:
        artifacts.discard(item["artifact_id"], expected_updated_at=item["updated_at"])
    run(setup)
    assert attention.reconcile() == 0
    assert attention.pending() == []
    assert db.conn().execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 1
    assert len(attention.pending(include_archived=True)) == (1 if dismissal == "archive" else 0)


@pytest.mark.parametrize("outcome", ["changed", "same_task"])
def test_actual_success_resolves_then_new_failure_episode_can_alert(setup, outcome):
    original = fail_twice(setup)
    run(setup, outcome=outcome)
    assert attention.reconcile() == 1
    assert attention.pending() == []
    resolved = artifacts.get(original["artifact_id"])
    assert resolved["status"] == "completed" and resolved["completed_at"] is not None
    new = fail_twice(setup)
    assert new["artifact_id"] != original["artifact_id"]


@pytest.mark.parametrize("action", ["pause", "remove", "configure"])
def test_intentional_configuration_change_resolves_without_model_work(setup, action):
    item = fail_twice(setup)
    config = janitors.get("sam")
    if action == "pause":
        janitors.set_enabled("sam", config["revision"], False)
    elif action == "remove":
        janitors.remove("sam", config["revision"])
    else:
        janitors.configure("sam", config["revision"], scope={"agent_ids": []})
    assert attention.pending() == []
    assert attention.reconcile() == 1
    assert artifacts.get(item["artifact_id"])["status"] == "completed"
    assert attention.reconcile() == 0


def test_skip_breaks_failure_streak_without_claiming_recovery(setup):
    run(setup)
    run(setup, outcome="skipped")
    run(setup)
    assert attention.reconcile() == 0
    run(setup)
    assert attention.reconcile() == 1
    item = attention.pending()[0]
    run(setup, outcome="insufficient_context")
    assert attention.reconcile() == 0
    assert attention.pending()[0]["artifact_id"] == item["artifact_id"]


def test_read_pending_never_creates_or_changes_artifact(setup):
    run(setup)
    run(setup)
    assert attention.pending() == []
    assert db.conn().execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0


def test_diagnostic_redacts_tokens_and_hidden_voice_blocks(setup):
    run(setup)
    run(setup, error="Authorization: Bearer secret_token_value <think>private reasoning</think>")
    attention.reconcile()
    content = artifacts.get(attention.pending()[0]["artifact_id"])["content"]
    assert "secret_token_value" not in content
    assert "private reasoning" not in content


def test_archived_recovery_completes_without_restoring_then_allows_new_episode(setup):
    original = fail_twice(setup)
    artifacts.archive(original["artifact_id"], archived=True, expected_updated_at=original["updated_at"])
    run(setup, outcome="same_task")
    attention.reconcile()
    closed = artifacts.get(original["artifact_id"])
    assert closed["archived_at"] is not None and closed["status"] == "completed"
    fresh = fail_twice(setup)
    assert fresh["artifact_id"] != original["artifact_id"]


def test_unrelated_failed_document_is_untouched(setup):
    unrelated = artifacts.create(session="sam", type="document", title="Manual diagnostic", status="failed",
                                 reference_id="janitor-failure:unrelated", payload={"content": "Keep this"})
    attention.reconcile()
    assert artifacts.get(unrelated["artifact_id"])["status"] == "failed"
    assert attention.pending() == []
