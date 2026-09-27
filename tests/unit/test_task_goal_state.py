"""Outcome commitment semantics use the actual isolated SQLite store."""

import json
import pytest
from lib import (
    agents,
    db,
    task_plans,
    task_goal_state as goals,
    task_goal_recovery as recovery,
)


def make_goal(tmp_path, **options):
    agent = agents.create_agent(
        persona="Goal", voice_id="v", cwd=str(tmp_path), session="goal-owner"
    )
    agent = agents.get_by_session("goal-owner")
    agents.start_runtime(agent["agent_id"], agent["session"])
    agents.bind_backend_session(agent["agent_id"], "native-goal-test")
    return task_plans.create(
        session=agent["session"],
        title="Ship outcome",
        items=[{"id": "method", "title": "Initial method"}],
        goal={
            "criteria": ["Outcome works", "Tests prove it"],
            "limits": "No deployment",
            "enroll": True,
            **options,
        },
    )


def act(plan, action, data):
    return goals.mutate(
        plan["plan_id"], revision=plan["revision"], action=action, data=data
    )


def test_pending_and_skipped_work_never_claims_completion(tmp_path):
    p = make_goal(tmp_path)
    with pytest.raises(ValueError, match="required work"):
        task_plans.finish(p["plan_id"], revision=0)
    with pytest.raises(ValueError, match="reason"):
        task_plans.update_item(p["items"][0]["item_id"], "skipped", revision=0)
    p = task_plans.update_item(
        p["items"][0]["item_id"], "skipped", "Need another approach", revision=0
    )
    assert p["completed_count"] == 0 and p["status"] == "active"
    assert p["goal"]["history"][-1]["detail"]["reason"] == "Need another approach"
    with pytest.raises(ValueError, match="required work"):
        task_plans.finish(p["plan_id"], revision=p["revision"])


def test_replanning_discovery_preserves_outcome_and_supersedes_wake(tmp_path):
    p = make_goal(tmp_path)
    claimed = recovery._claim(p["plan_id"], db.now_ms() + 130000)
    old_request = claimed[1]["continuation"]["request_id"]
    p = act(
        p,
        "replan",
        {
            "reason": "Probe shows a built-in path already exists",
            "steps": [{"id": "reuse", "title": "Verify and reuse existing behavior"}],
        },
    )
    p = act(
        p,
        "checkpoint",
        {
            "progress": "Existing path discovered",
            "next_work": "Inspect new probe results, choose whether to reuse",
            "continuation": {"kind": "timer", "due_at": db.now_ms() + 1000},
        },
    )
    with pytest.raises(ValueError, match="superseded"):
        recovery.validate_dispatch(agents.get_by_session("goal-owner"), old_request)
    assert p["goal"]["outcome"] == "Ship outcome"
    assert next(i for i in p["items"] if i["status"] == "removed")["detail"].startswith(
        "Probe"
    )
    item = next(i for i in p["items"] if i["status"] == "pending")
    p = task_plans.update_item(
        item["item_id"], "completed", "Actual probe passed", revision=p["revision"]
    )
    with pytest.raises(ValueError, match="evidence"):
        task_plans.finish(p["plan_id"], revision=p["revision"])
    p = act(
        p,
        "checkpoint",
        {
            "progress": "Verified outcome",
            "next_work": "Close with evidence",
            "evidence": {
                "criterion-1": "probe.json passed",
                "criterion-2": "test output passed",
            },
        },
    )
    p = task_plans.finish(p["plan_id"], revision=p["revision"])
    assert (
        p["status"] == "completed"
        and p["completed_count"] == 1
        and p["counts"]["removed"] == 1
    )


def test_concurrent_edit_rejected_and_checkpoint_atomic(tmp_path):
    p = make_goal(tmp_path)
    updated = act(
        p,
        "checkpoint",
        {
            "progress": "One result",
            "next_work": "Reassess",
            "continuation": {
                "kind": "dependency",
                "key": "job-1",
                "reason": "Build running",
                "due_at": db.now_ms() + 60000,
            },
        },
    )
    with pytest.raises(ValueError, match="changed"):
        act(p, "cancel", {"reason": "Stale request"})
    before = task_plans.get(p["plan_id"])
    with pytest.raises(ValueError, match="dependency"):
        act(
            updated,
            "checkpoint",
            {
                "progress": "Invalid",
                "next_work": "Next",
                "evidence": {"criterion-1": "must rollback"},
                "continuation": {"kind": "dependency"},
            },
        )
    after = task_plans.get(p["plan_id"])
    after.pop("server_now")
    before.pop("server_now")
    assert after == before


@pytest.mark.parametrize("outcome", ["succeeded", "failed"])
def test_external_result_is_durable_and_idempotent_by_revision(tmp_path, outcome):
    p = make_goal(tmp_path)
    p = act(
        p,
        "checkpoint",
        {
            "progress": "Launched job",
            "next_work": "Inspect job result",
            "continuation": {
                "kind": "dependency",
                "key": "job-1",
                "reason": "Build running",
                "due_at": db.now_ms() + 60000,
            },
        },
    )
    old = p
    p = act(
        p, "dependency", {"key": "job-1", "outcome": outcome, "evidence": "build.log"}
    )
    assert p["goal"]["continuation"]["dependency_result"]["outcome"] == outcome
    with pytest.raises(ValueError, match="changed"):
        act(
            old,
            "dependency",
            {"key": "job-1", "outcome": outcome, "evidence": "build.log"},
        )


@pytest.mark.parametrize("action", ["pause", "cancel", "block"])
def test_goal_control_fences_existing_wake(tmp_path, action):
    p = make_goal(tmp_path)
    claim = recovery._claim(p["plan_id"], db.now_ms() + 130000)
    request = claim[1]["continuation"]["request_id"]
    act(p, action, {"reason": "Owner requested this"})
    assert (
        recovery.tick(
            lambda *args: pytest.fail("must not dispatch"), now=db.now_ms() + 999999
        )
        == 0
    )
    with pytest.raises(ValueError):
        recovery.validate_dispatch(agents.get_by_session("goal-owner"), request)


def test_duplicate_claim_and_restart_reuse_same_receipt(tmp_path):
    p = make_goal(tmp_path)
    now = db.now_ms() + 130000
    first = recovery._claim(p["plan_id"], now)
    assert recovery._claim(p["plan_id"], now) is None
    # Expired lease models process loss after claiming, before receiving dispatch result.
    second = recovery._claim(p["plan_id"], now + 61000)
    assert (
        second[1]["continuation"]["request_id"]
        == first[1]["continuation"]["request_id"]
    )
    request = first[1]["continuation"]["request_id"]
    recovery._result(p["plan_id"], request, now + 61000, "admitted", "receipt")
    third = recovery._claim(p["plan_id"], now + 999999)
    assert third[1]["continuation"]["request_id"] != request


def test_wrong_native_owner_suppresses_wake(tmp_path):
    p = make_goal(tmp_path)
    agents.bind_backend_session(p["agent_id"], "different-native")
    assert (
        recovery.tick(
            lambda *args: pytest.fail("wrong target"), now=db.now_ms() + 999999
        )
        == 0
    )
    assert (
        task_plans.get(p["plan_id"])["goal"]["continuation"]["observed_state"]
        == "owner_changed"
    )


def test_native_provider_loop_never_competes(tmp_path):
    from lib import agent_goals

    p = make_goal(tmp_path)
    agent_goals.upsert(
        p["agent_id"],
        session=p["session"],
        backend="codex",
        goal={"objective": "Native objective", "status": "active"},
    )
    assert (
        recovery.tick(
            lambda *args: pytest.fail("competing scheduler"), now=db.now_ms() + 999999
        )
        == 0
    )
    assert (
        task_plans.get(p["plan_id"])["goal"]["continuation"]["observed_state"]
        == "native_owned"
    )


def test_pending_approval_and_explicit_stop_do_not_restart(tmp_path):
    from lib import artifacts, turn_queue

    p = make_goal(tmp_path)
    artifacts.create_decision(
        session=p["session"], title="Permission", question="Publish it?"
    )
    assert (
        recovery.tick(
            lambda *args: pytest.fail("approval bypass"), now=db.now_ms() + 999999
        )
        == 0
    )
    assert (
        task_plans.get(p["plan_id"])["goal"]["continuation"]["observed_state"]
        == "approval"
    )
    task_plans.pause_recovery_for_agent(p["agent_id"], "User stopped this agent")
    turn_queue.set_paused(p["agent_id"], False)  # unrelated fresh conversation later
    assert task_plans.get(p["plan_id"])["status"] == "paused"


def test_unavailable_dispatch_retries_are_bounded_and_keep_receipt(tmp_path):
    p = make_goal(tmp_path, recovery_limit=2)
    attempts = []

    def unavailable(session, prompt, request):
        attempts.append(request)
        raise RuntimeError("All accounts lack capacity")

    now = db.now_ms() + 130000
    recovery.tick(unavailable, now=now)
    recovery.tick(unavailable, now=now + 999999)
    recovery.tick(unavailable, now=now + 9999999)
    assert len(attempts) == 2 and len(set(attempts)) == 1
    p = task_plans.get(p["plan_id"])
    assert p["goal"]["continuation"]["state"] == "attention"
    assert "capacity" in p["goal"]["history"][-1]["detail"]["reason"]


def test_legacy_omission_reason_retained_after_later_resolution(tmp_path):
    agents.create_agent(
        persona="Legacy", voice_id="", cwd=str(tmp_path), session="legacy"
    )
    p = task_plans.create(
        session="legacy", title="Old checklist", items=[{"id": "a", "title": "Work"}]
    )
    task_plans.update_item(
        p["items"][0]["item_id"], "skipped", "Superseded by new evidence"
    )
    p = task_plans.update_item(
        p["items"][0]["item_id"], "completed", "Actual result now verified"
    )
    assert p["history"][0]["detail"]["reason"] == "Superseded by new evidence"
    assert p["legacy"] and not p["completion_verified"]


def test_explicit_goal_resume_does_not_unpause_other_queued_work(tmp_path):
    from lib import turn_queue

    p = make_goal(tmp_path)
    turn_queue.set_paused(p["agent_id"], True)
    task_plans.pause_recovery_for_agent(p["agent_id"], "User stopped")
    p = task_plans.get(p["plan_id"])
    p = act(p, "resume", {"reason": "User resumed this outcome"})
    claim = recovery._claim(p["plan_id"], db.now_ms() + 20000)
    assert claim is not None
    request = claim[1]["continuation"]["request_id"]
    assert recovery.allows_paused_queue(request)
    assert turn_queue.is_paused(p["agent_id"])


def test_historical_migration_preserves_ambiguity_and_enrolls_nothing(tmp_path):
    import sqlite3
    from lib import db_schema

    con = sqlite3.connect(tmp_path / "legacy.sqlite")
    con.row_factory = sqlite3.Row
    # Actual previous shape, including its one-active-plan constraint.
    schema = (
        db_schema._SCHEMA_SQL.replace(
            "    history_json TEXT NOT NULL DEFAULT '[]',\n", ""
        )
        .replace(
            "    revision INTEGER NOT NULL DEFAULT 0,\n    goal_json TEXT NOT NULL DEFAULT '{}',\n    recovery_enabled INTEGER NOT NULL DEFAULT 0,\n",
            "",
        )
        .replace("    required INTEGER NOT NULL DEFAULT 1,\n", "")
    )
    schema = schema.replace(
        "CREATE INDEX idx_task_plans_recovery ON task_plans(recovery_enabled,status,updated_at);",
        "",
    )
    con.executescript(schema)
    con.execute(
        "CREATE UNIQUE INDEX idx_task_plans_one_active ON task_plans(agent_id) WHERE status='active'"
    )
    con.execute(
        "INSERT INTO task_plans(plan_id,agent_id,session,title,status,created_at,updated_at) VALUES('old','old-agent','old','Historical completion','completed',1,1)"
    )
    con.execute(
        "INSERT INTO task_items(item_id,plan_id,title,status,created_at) VALUES('old-step','old','Unknown result','pending',1)"
    )
    con.execute("PRAGMA user_version=96")
    con.commit()
    db._migrate(con)
    row = con.execute("SELECT * FROM task_plans WHERE plan_id='old'").fetchone()
    assert (
        row["status"] == "completed"
        and row["recovery_enabled"] == 0
        and row["goal_json"] == "{}"
    )
    assert (
        con.execute(
            "SELECT status FROM task_items WHERE item_id='old-step'"
        ).fetchone()[0]
        == "pending"
    )
    assert not con.execute(
        "SELECT 1 FROM sqlite_master WHERE name='idx_task_plans_one_active'"
    ).fetchone()


def test_document_versions_flexible_json_and_atomic_checkpoint_conflict(tmp_path):
    p = make_goal(tmp_path)
    p = act(
        p,
        "document",
        {
            "name": "state",
            "format": "json",
            "content": {"arbitrary": ["shape", {"nested": True}]},
            "document_revision": 0,
            "reason": "Initial findings",
        },
    )
    assert p["goal"]["documents"][0]["revision"] == 1
    first = task_plans.goal_document(p["plan_id"], "state")
    p = act(
        p,
        "checkpoint",
        {
            "progress": "New result",
            "next_work": "Reassess",
            "documents": [
                {
                    "name": "state",
                    "format": "json",
                    "content": ["Entire structure changed", 42],
                    "document_revision": 1,
                    "reason": "A better representation",
                },
                {
                    "name": "notes.md",
                    "format": "markdown",
                    "content": "# Do not redo\nThe probe already passed.",
                    "document_revision": 0,
                    "reason": "Keep useful context",
                },
            ],
            "continuation": {"kind": "blocked", "reason": "Review new result"},
        },
    )
    assert task_plans.goal_document(p["plan_id"], "state")["content"] == [
        "Entire structure changed",
        42,
    ]
    assert (
        task_plans.goal_document(p["plan_id"], "state", 1)["content"]
        == first["content"]
    )
    with pytest.raises(ValueError, match="document changed"):
        act(
            p,
            "checkpoint",
            {
                "progress": "Must roll back",
                "next_work": "No dispatch",
                "documents": [
                    {
                        "name": "notes.md",
                        "format": "markdown",
                        "content": "Must roll back",
                        "document_revision": 1,
                        "reason": "First write",
                    },
                    {
                        "name": "state",
                        "format": "json",
                        "content": None,
                        "document_revision": 1,
                        "reason": "Stale second write",
                    },
                ],
            },
        )
    assert task_plans.goal_document(p["plan_id"], "notes.md")["revision"] == 1
    assert (
        task_plans.get(p["plan_id"])["goal"]["checkpoint"]["progress"] == "New result"
    )


@pytest.mark.parametrize("outcome", ["succeeded", "failed"])
def test_registered_background_job_completion_reaches_same_goal(tmp_path, outcome):
    from lib import background_jobs

    p = make_goal(tmp_path)
    job = background_jobs.upsert(
        session=p["session"], title="External proof", job_id="proof-worker"
    )
    p = act(
        p,
        "checkpoint",
        {
            "progress": "Worker registered",
            "next_work": "Inspect its real result",
            "continuation": {
                "kind": "dependency",
                "key": "proof",
                "job_handle": background_jobs.job_handle(job),
                "reason": "Worker running",
                "due_at": db.now_ms() + 600000,
            },
        },
    )
    background_jobs.finish(
        job["job_id"],
        generation=job["generation"],
        status=outcome,
        reason="Recorded actual exit",
    )
    claims = []
    recovery.tick(lambda *args: claims.append(args) or {"queued": False})
    assert len(claims) == 1
    result = task_plans.get(p["plan_id"])["goal"]["continuation"]["dependency_result"]
    assert result["outcome"] == outcome and result[
        "job_handle"
    ] == background_jobs.job_handle(job)


def test_enrolled_goal_is_not_also_woken_by_generic_heartbeat(tmp_path):
    from lib import heartbeat

    p = make_goal(tmp_path)
    agent = agents.get_by_session(p["session"])
    agent["heartbeat_enabled"] = True
    assert not heartbeat.heartbeat_enabled(agent)
    assert all(
        a["agent_id"] != p["agent_id"] for a in heartbeat.restart_heartbeat_agents()
    )
