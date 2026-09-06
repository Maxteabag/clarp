"""Demand jobs have quiet, dismissible failure episodes with durable recovery."""
from __future__ import annotations

import json
import uuid

import pytest

from lib import artifacts, backends, db, janitor_attention as attention, janitor_builtins, janitors


@pytest.fixture(params=["message-delegator", "tool-explainer"])
def demand(request, monkeypatch):
    monkeypatch.setattr(backends, "active_handles", lambda *args: [])
    role = request.param
    return janitor_builtins.ensure_builtins(cwd="/tmp", initial={role: {"enabled": True}})[role]


def admit(config):
    run = janitor_builtins.begin_run(config["template_id"], uuid.uuid4().hex,
                                    context={"input_hash": "bounded-input"})
    assert run is not None and janitor_builtins.claim_run(run["run_id"])
    return run


def finish(config, *, failed=False):
    run = admit(config)
    assert janitor_builtins.complete_run(
        run["run_id"], outcome="failed" if failed else "completed",
        result={"summary": "Request failed" if failed else "Request completed",
                "status": "failed" if failed else "route" if config["template_id"] == "message-delegator" else "ready"},
        error="Provider unavailable" if failed else "",
    )
    return run


def fail_twice(config):
    finish(config, failed=True)
    finish(config, failed=True)
    assert attention.reconcile() == 1
    return attention.pending()[0]


def test_two_failures_create_one_quiet_alert_using_the_actual_job_name(demand):
    tables = ("messages", "queued_turns", "decision_deliveries", "janitor_effects")
    before = {table: db.conn().execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in tables}
    finish(demand, failed=True)
    assert attention.reconcile() == 0 and attention.pending() == []
    finish(demand, failed=True)
    assert attention.reconcile() == 1
    alert = attention.pending()[0]
    artifact = artifacts.get(alert["artifact_id"])
    assert alert["failure_count"] == 2 and alert["session"] == demand["session"]
    assert janitors.template(demand["template_id"])["name"] in artifact["summary"]
    assert "Task labels" not in artifact["summary"] and "failed reviews" not in artifact["summary"]
    assert "Provider unavailable" in artifact["content"]
    assert artifact["payload"]["suggested_action"] == "review_configuration"
    assert artifacts.attention(include_questions=True) == []
    for _ in range(3):
        assert attention.reconcile() == 0
    assert artifacts.get(alert["artifact_id"])["updated_at"] == artifact["updated_at"]
    assert before == {table: db.conn().execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in tables}


def test_real_completed_receipt_closes_failure_episode_and_allows_a_new_one(demand):
    old = fail_twice(demand)
    recovered = finish(demand)
    assert not db.conn().execute("SELECT 1 FROM janitor_effects").fetchone()
    assert attention.reconcile() == 1 and attention.pending() == []
    assert artifacts.get(old["artifact_id"])["status"] == "completed"
    assert attention.reconcile() == 0
    fresh = fail_twice(demand)
    assert fresh["artifact_id"] != old["artifact_id"]
    assert fresh["reference_id"].endswith(recovered["run_id"])


@pytest.mark.parametrize("dismissal", ["archive", "discard"])
def test_dismissed_demand_episode_stays_dismissed_until_verified_recovery(demand, dismissal):
    old = fail_twice(demand)
    if dismissal == "archive":
        artifacts.archive(old["artifact_id"], archived=True, expected_updated_at=old["updated_at"])
    else:
        artifacts.discard(old["artifact_id"], expected_updated_at=old["updated_at"])
    finish(demand, failed=True)
    assert attention.reconcile() == 0 and attention.pending() == []
    assert db.conn().execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 1
    finish(demand)
    attention.reconcile()
    assert attention.pending() == []
    old_row = db.conn().execute("SELECT * FROM artifacts WHERE artifact_id=?", (old["artifact_id"],)).fetchone()
    assert old_row["archived_at" if dismissal == "archive" else "deleted_at"] is not None
    fresh = fail_twice(demand)
    assert fresh["artifact_id"] != old["artifact_id"]


@pytest.mark.parametrize("damage", [
    "missing_receipt", "malformed_json", "list", "empty", "missing_summary",
    "long_summary", "raw_prompt", "wrong_executor", "wrong_template", "unfinished",
    "running", "failed_outcome",
])
def test_incomplete_or_malformed_demand_result_cannot_claim_recovery(demand, damage):
    old = fail_twice(demand)
    candidate = admit(demand)
    c = db.conn()
    c.execute("UPDATE janitor_runs SET status='completed',outcome='completed',finished_at=? WHERE run_id=?",
              (db.now_ms(), candidate["run_id"]))
    payload = {"summary": "Request completed", "status": "ready"}
    if damage == "list":
        payload = []
    elif damage == "empty":
        payload = {}
    elif damage == "missing_summary":
        payload = {"status": "ready"}
    elif damage == "long_summary":
        payload["summary"] = "x" * 501
    elif damage == "raw_prompt":
        payload["prompt"] = "Raw provider response is not a receipt"
    if damage != "missing_receipt":
        c.execute("INSERT INTO janitor_demand_results(run_id,result_json,created_at) VALUES (?,?,?)",
                  (candidate["run_id"], "{broken" if damage == "malformed_json" else json.dumps(payload), db.now_ms()))
    if damage in {"wrong_executor", "wrong_template"}:
        frozen = dict(candidate["configuration"])
        frozen["executor" if damage == "wrong_executor" else "template_id"] = "wrong"
        c.execute("UPDATE janitor_runs SET configuration_json=? WHERE run_id=?", (json.dumps(frozen), candidate["run_id"]))
    elif damage == "unfinished":
        c.execute("UPDATE janitor_runs SET finished_at=NULL WHERE run_id=?", (candidate["run_id"],))
    elif damage == "running":
        c.execute("UPDATE janitor_runs SET status='running' WHERE run_id=?", (candidate["run_id"],))
    elif damage == "failed_outcome":
        c.execute("UPDATE janitor_runs SET outcome='failed' WHERE run_id=?", (candidate["run_id"],))
    assert attention.reconcile() == 0
    assert attention.pending()[0]["artifact_id"] == old["artifact_id"]
    assert artifacts.get(old["artifact_id"])["status"] == "failed"
