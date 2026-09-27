"""Stale work labels: silent jobs, idle helpers, forgotten statuses."""
from __future__ import annotations

import os

import pytest

from lib import agents, background_jobs, db, stale_work
from lib.policies import stale_work as policy
from lib.policies.helper_state import Role
from lib.snapshot import build_agent_snapshot

MIN = 60_000
LIMITS = policy.Thresholds()   # 15 + 15 min, 30 min, 2 h


def _agent(session: str = "boss", state: str = "done") -> str:
    aid = agents.create_agent(persona=session.title(), voice_id="", cwd="/srv/none", session=session)
    agents.record_state(aid, state, {})
    return aid


def _helper(parent_id: str, session: str, state: str = "done") -> str:
    aid = _agent(session, state)
    agents.set_lineage(aid, parent_agent_id=parent_id, role=Role.HELPER)
    return aid


def _row(agent_id: str) -> dict:
    return next(r for r in build_agent_snapshot(None)["agents"] if r["agent_id"] == agent_id)


def _notes(job_id: str) -> list[str]:
    return [e["note"] for e in background_jobs.timeline(job_id) if e["note"]]


# ---- policy tables -----------------------------------------------------------

@pytest.mark.parametrize("age_min,verified,expected", [
    (5, False, policy.JobVerdict.KEEP),
    (16, False, policy.JobVerdict.STALE),
    (31, False, policy.JobVerdict.LOST),
    (600, True, policy.JobVerdict.KEEP),
])
def test_job_heartbeat_verdicts(age_min, verified, expected):
    assert policy.job_heartbeat(heartbeat_age_ms=age_min * MIN, worker_verified=verified,
                                thresholds=LIMITS) == expected


def test_a_zero_threshold_turns_each_rule_off():
    off = policy.Thresholds(0, 0, 0, 0)
    idle = policy.Activity(busy=False, active_jobs=0, running_children=0, idle_since_ms=0)
    assert policy.job_heartbeat(heartbeat_age_ms=10**9, worker_verified=False,
                                thresholds=off) == policy.JobVerdict.KEEP
    assert not policy.helper_idle_due(helper_state="running", activity=idle,
                                      now_ms=10**9, thresholds=off)
    assert not policy.custom_status_due(status="x", status_at=0, janitor_owned=False,
                                        activity=idle, now_ms=10**9, thresholds=off)


@pytest.mark.parametrize("activity", [
    policy.Activity(busy=True, active_jobs=0, running_children=0, idle_since_ms=0),
    policy.Activity(busy=False, active_jobs=1, running_children=0, idle_since_ms=0),
    policy.Activity(busy=False, active_jobs=0, running_children=1, idle_since_ms=0),
])
def test_counted_work_is_never_idle(activity):
    assert not policy.helper_idle_due(helper_state="running", activity=activity,
                                      now_ms=10**9, thresholds=LIMITS)
    assert not policy.custom_status_due(status="x", status_at=0, janitor_owned=False,
                                        activity=activity, now_ms=10**9, thresholds=LIMITS)


def test_a_janitor_label_or_an_undated_status_is_left_alone():
    idle = policy.Activity(busy=False, active_jobs=0, running_children=0, idle_since_ms=0)
    assert not policy.custom_status_due(status="x", status_at=0, janitor_owned=True,
                                        activity=idle, now_ms=10**9, thresholds=LIMITS)
    assert not policy.custom_status_due(status="x", status_at=None, janitor_owned=False,
                                        activity=idle, now_ms=10**9, thresholds=LIMITS)


# ---- rule 1: job heartbeat -------------------------------------------------------

def _silent_job(job_id: str = "ci-wait", **kw) -> dict:
    # A long declared timeout: only the stale-work rule can end it early.
    return background_jobs.upsert(session="boss", job_id=job_id, kind="ci", title="Wait for CI",
                                  heartbeat_timeout_ms=24 * 60 * MIN, **kw)


def test_silent_job_is_noted_then_failed_heartbeat_lost():
    _agent()
    job = _silent_job()
    beat = job["heartbeat_at"]

    assert background_jobs.reconcile_stale(now_ms=beat + 16 * MIN, thresholds=LIMITS) == []
    assert background_jobs.reconcile_stale(now_ms=beat + 17 * MIN, thresholds=LIMITS) == []
    assert background_jobs.get("ci-wait", reconcile=False)["status"] == "running"
    assert _notes("ci-wait") == [policy.STALE_NOTE]  # once per silence

    assert background_jobs.reconcile_stale(now_ms=beat + 31 * MIN, thresholds=LIMITS) == ["ci-wait"]
    failed = background_jobs.get("ci-wait", reconcile=False)
    assert failed["status"] == "failed"
    assert failed["terminal_reason"] == policy.HEARTBEAT_LOST
    assert failed["outcome_state"] == "unknown"
    assert failed["worker_freshness"] == "stale"
    assert _notes("ci-wait") == [policy.STALE_NOTE, policy.LOST_NOTE]


def test_fresh_heartbeat_is_left_running():
    _agent()
    job = _silent_job()
    assert background_jobs.reconcile_stale(now_ms=job["heartbeat_at"] + 5 * MIN,
                                           thresholds=LIMITS) == []
    assert background_jobs.get("ci-wait", reconcile=False)["status"] == "running"
    assert _notes("ci-wait") == []


def test_a_progress_line_counts_as_a_sign_of_life():
    _agent()
    job = _silent_job()
    db.conn().execute("UPDATE background_jobs SET progress_at=? WHERE job_id='ci-wait'",
                      (job["heartbeat_at"] + 20 * MIN,))
    assert background_jobs.reconcile_stale(now_ms=job["heartbeat_at"] + 40 * MIN,
                                           thresholds=LIMITS) == []
    assert background_jobs.get("ci-wait", reconcile=False)["status"] == "running"


def test_verified_live_worker_pid_is_never_touched():
    _agent()
    job = _silent_job(worker_pid=os.getpid(),
                      worker_start_token=background_jobs.process_start_token(os.getpid()))
    assert background_jobs.reconcile_stale(now_ms=job["heartbeat_at"] + 5 * 60 * MIN,
                                           thresholds=LIMITS) == []
    assert background_jobs.get("ci-wait", reconcile=False)["status"] == "running"
    assert _notes("ci-wait") == []


def test_computer_owned_jobs_keep_their_own_timeout():
    job = background_jobs.upsert_computer(
        computer_id="host", job_id="update", kind="update", title="Update", heartbeat_timeout_ms=24 * 60 * MIN)
    assert background_jobs.reconcile_stale(now_ms=job["heartbeat_at"] + 60 * MIN,
                                           thresholds=LIMITS) == []
    assert background_jobs.get("update", reconcile=False)["status"] == "running"


# ---- rule 2: helper idle -------------------------------------------------------

def test_idle_helper_moves_to_reported_and_notes_its_mirror_job():
    boss = _agent()
    helper = _helper(boss, "slice-helper")
    background_jobs.upsert(session="boss", job_id="sub-agent-slice", kind="sub-agent",
                           title="slice", detail="slice-helper")
    now = db.now_ms() + 31 * MIN

    counts = stale_work.sweep(now_ms=now, limits=LIMITS)

    assert counts["helpers_reported"] == 1
    assert agents.get_by_agent_id(helper)["helper_state"] == "reported"
    assert any("slice-helper idle" in n for n in _notes("sub-agent-slice"))
    assert _row(boss)["running_children"] == 0


@pytest.mark.parametrize("setup", ["active_turn", "own_job", "running_child", "recent"])
def test_helper_with_work_or_recent_activity_stays_running(setup):
    boss = _agent()
    helper = _helper(boss, "slice-helper", state="thinking" if setup == "active_turn" else "done")
    if setup == "own_job":
        background_jobs.upsert(session="slice-helper", job_id="build", kind="worker", title="Build")
    if setup == "running_child":
        _helper(helper, "grandchild")
    now = db.now_ms() + (5 if setup == "recent" else 31) * MIN

    stale_work.sweep(now_ms=now, limits=LIMITS)

    assert agents.get_by_agent_id(helper)["helper_state"] == "running"


# ---- rule 3: custom status -------------------------------------------------------

def test_stale_status_is_cleared_and_background_state_settles():
    aid = _agent("lena", state="background")
    agents.set_custom_status(aid, "Building")
    assert _row(aid)["latest_state"] == "background"

    counts = stale_work.sweep(now_ms=db.now_ms() + 3 * 60 * MIN, limits=LIMITS)

    assert counts["statuses_cleared"] == 1
    row = agents.get_by_agent_id(aid)
    assert row["custom_status"] == "" and row["custom_status_at"] is None
    snapshot = _row(aid)
    assert snapshot["status_text"] is None
    assert snapshot["latest_state"] == "idle"
    assert agents.latest_state(aid)["detail"]["cleared_status"] == "Building"


@pytest.mark.parametrize("setup", ["recent", "active_turn", "own_job", "janitor_label"])
def test_status_with_work_or_recent_write_is_kept(setup, monkeypatch):
    aid = _agent("lena", state="thinking" if setup == "active_turn" else "done")
    agents.set_custom_status(aid, "Building")
    if setup == "own_job":
        background_jobs.upsert(session="lena", job_id="build", kind="worker", title="Build")
    if setup == "janitor_label":
        monkeypatch.setattr(stale_work, "_janitor_owned", lambda: {aid})
    later = (30 if setup == "recent" else 3 * 60) * MIN

    stale_work.sweep(now_ms=db.now_ms() + later, limits=LIMITS)

    assert agents.get_by_agent_id(aid)["custom_status"] == "Building"


def test_a_rewritten_status_wins_the_race():
    aid = _agent("lena")
    agents.set_custom_status(aid, "Building")
    stamp = agents.get_by_agent_id(aid)["custom_status_at"]
    db.conn().execute("UPDATE agents SET custom_status_at=? WHERE agent_id=?", (stamp + 1, aid))
    assert not agents.clear_stale_custom_status(aid, status="Building", status_at=stamp)
    assert agents.get_by_agent_id(aid)["custom_status"] == "Building"
