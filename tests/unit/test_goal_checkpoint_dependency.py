"""A checkpoint must not silently drop the job a goal is waiting for.

2026-11-10: a goal waited on a background job (the durable launcher's
dependency continuation). The owner then wrote a routine progress checkpoint,
with a timer or with no continuation at all. Either one replaced the
dependency with a two-minute timer: the job ended and woke nobody. Three
missed or late wakes in one day, one of them a spot-VM preemption.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from lib import agents as agents_db
from lib import background_jobs, db, task_goal_recovery, task_goal_state, task_plans

HOUR_MS = 3_600_000


@pytest.fixture
def owner(tmp_path):
    agent_id = agents_db.create_agent(
        persona="Mike", voice_id="V", cwd=str(tmp_path), session="mike", backend="claude")
    agents_db.start_runtime(agent_id, "mike")
    agents_db.bind_backend_session(agent_id, "native-goal-test")
    return agent_id


def _goal():
    return task_plans.create(
        session="mike", title="Finish the outcome", items=[{"id": "probe", "title": "Probe"}],
        goal={"criteria": ["Verified result"], "limits": "No deployment", "enroll": True})


def _checkpoint(plan, **data):
    current = task_plans.get(plan["plan_id"])
    body = {"progress": "Answered a side question", "next_work": "Read the build result",
            **data}
    return task_goal_state.mutate(
        current["plan_id"], revision=current["revision"], action="checkpoint", data=body)


def _waiting_on_job(key="build-1111", *, timeout_ms=6 * HOUR_MS):
    """What run_detached_job.sh --goal stores before the command starts."""
    plan = _goal()
    job = background_jobs.upsert(session="mike", job_id=key, title="Long build")
    handle = background_jobs.job_handle(job)
    due = db.now_ms() + timeout_ms
    plan = _checkpoint(
        plan, progress="Build launched", next_work="Inspect its result",
        continuation={"kind": "dependency", "key": key, "job_handle": handle,
                      "reason": f"Waiting for background job {key}", "due_at": due})
    wake = plan["goal"]["continuation"]
    assert (wake["state"], wake["dependency_key"], wake["job_handle"], wake["due_at"]) == (
        "waiting", key, handle, due)
    return plan, job, handle, due


def _continuation(plan):
    return task_plans.get(plan["plan_id"])["goal"]["continuation"]


def _stored(plan):
    row = db.conn().execute(
        "SELECT revision, status, goal_json FROM task_plans WHERE plan_id=?",
        (plan["plan_id"],)).fetchone()
    return tuple(row)


def _history(plan, kind):
    return [entry for entry in task_plans.get(plan["plan_id"])["goal"]["history"]
            if entry["kind"] == kind]


def _tick(offset_ms=0):
    """The scheduler's goal pass; returns the wake prompts it dispatched."""
    sent = []

    def accepted(session, text, request):
        sent.append(text)
        return SimpleNamespace(queued=False)

    real = db.now_ms
    with pytest.MonkeyPatch.context() as later:
        later.setattr(db, "now_ms", lambda: real() + offset_ms)
        task_goal_recovery.tick(accepted, now=db.now_ms())
    return sent


def _job_ends(job, status="succeeded"):
    assert background_jobs.finish(job["job_id"], generation=job["generation"], status=status)


def test_a_checkpoint_without_a_continuation_keeps_the_pending_job_wake(owner):
    plan, job, handle, due = _waiting_on_job()
    before = _continuation(plan)

    after = _checkpoint(plan)   # {"progress": ..., "next_work": ...} and nothing else

    wake = after["goal"]["continuation"]
    assert after["goal"]["checkpoint"]["progress"] == "Answered a side question"
    assert (wake["state"], wake["dependency_key"], wake["job_handle"], wake["due_at"],
            wake["reason"]) == ("waiting", "build-1111", handle, due, before["reason"])
    assert _history(plan, "checkpoint")[-1]["detail"]["dependency_kept"] == {
        "key": "build-1111", "job_handle": handle, "due_at": due}
    assert _history(plan, "dependency_replaced") == []
    assert _tick() == []            # the job is still running: no wake yet

    _job_ends(job)
    prompts = _tick()               # the job's end wakes the owner
    assert len(prompts) == 1
    assert '"key": "build-1111"' in prompts[0] and '"outcome": "succeeded"' in prompts[0]
    assert _history(plan, "dependency_result")[0]["detail"]["job_handle"] == handle


@pytest.mark.parametrize("continuation", [None, {}], ids=["null", "empty"])
def test_an_empty_continuation_is_no_continuation(owner, continuation):
    plan, _job, handle, due = _waiting_on_job()

    wake = _checkpoint(plan, continuation=continuation)["goal"]["continuation"]

    assert (wake["state"], wake["dependency_key"], wake["job_handle"], wake["due_at"]) == (
        "waiting", "build-1111", handle, due)


@pytest.mark.parametrize("continuation", [
    {"kind": "timer", "due_at": 10 * 60_000},
    {"due_at": 10 * 60_000},            # the kind defaults to a timer
    {"kind": "timer"},
], ids=["timer", "bare-due", "default-due"])
def test_a_timer_checkpoint_is_refused_and_names_the_pending_job(owner, continuation):
    plan, job, handle, due = _waiting_on_job()
    if "due_at" in continuation:
        continuation = {**continuation, "due_at": db.now_ms() + continuation["due_at"]}
    before = _stored(plan)

    with pytest.raises(ValueError) as refused:
        _checkpoint(plan, continuation=continuation)

    message = str(refused.value)
    assert "build-1111" in message and handle in message
    assert "Omit continuation" in message and "replace_dependency" in message
    # Nothing was written: the wake stands and so does the last checkpoint.
    assert _stored(plan) == before
    _job_ends(job, "failed")
    prompts = _tick()
    assert len(prompts) == 1 and '"outcome": "failed"' in prompts[0]


def test_an_earlier_deadline_on_the_same_dependency_wakes_the_owner_and_says_so(owner):
    """The way to be woken before the job ends: the dependency again, with an
    earlier deadline. That wake says the job is no longer awaited."""
    plan, _job, handle, _due = _waiting_on_job()
    _checkpoint(plan, continuation={
        "kind": "dependency", "key": "build-1111", "job_handle": handle,
        "reason": "Waiting for the build", "due_at": db.now_ms() + 10 * 60_000})

    assert _tick(offset_ms=5 * 60_000) == []
    prompts = _tick(offset_ms=11 * 60_000)

    assert len(prompts) == 1
    assert ("Background job build-1111 was still running when its deadline passed and is "
            "no longer awaited") in prompts[0]


def test_replacing_the_dependency_takes_saying_so_and_is_recorded(owner):
    plan, _job, handle, _due = _waiting_on_job()
    later = db.now_ms() + 10 * 60_000

    wake = _checkpoint(plan, continuation={
        "kind": "timer", "due_at": later, "replace_dependency": True})["goal"]["continuation"]

    assert (wake["state"], wake["dependency_key"], wake["job_handle"], wake["due_at"]) == (
        "ready", "", "", later)
    replaced = _history(plan, "dependency_replaced")
    assert [entry["detail"] for entry in replaced] == [{
        "key": "build-1111", "job_handle": handle, "by": "timer",
        "reason": "Checkpoint replaced the pending dependency on build-1111"}]


def test_another_dependency_replaces_the_pending_one_and_is_recorded(owner):
    plan, _job, handle, _due = _waiting_on_job()
    other = background_jobs.upsert(session="mike", job_id="deploy-2", title="Deploy")
    due = db.now_ms() + HOUR_MS

    wake = _checkpoint(plan, continuation={
        "kind": "dependency", "key": "deploy-2", "job_handle": background_jobs.job_handle(other),
        "reason": "Waiting for the deploy", "due_at": due})["goal"]["continuation"]

    assert (wake["state"], wake["dependency_key"]) == ("waiting", "deploy-2")
    assert [entry["detail"]["key"] for entry in _history(plan, "dependency_replaced")] == [
        "build-1111"]
    assert _history(plan, "dependency_replaced")[0]["detail"]["by"] == "dependency deploy-2"


def test_declaring_the_same_dependency_again_updates_it_without_a_replacement(owner):
    plan, _job, handle, due = _waiting_on_job()

    wake = _checkpoint(plan, continuation={
        "kind": "dependency", "key": "build-1111", "job_handle": handle,
        "reason": "Still waiting for the build", "due_at": due + HOUR_MS})[
            "goal"]["continuation"]

    assert (wake["state"], wake["due_at"], wake["reason"]) == (
        "waiting", due + HOUR_MS, "Still waiting for the build")
    assert _history(plan, "dependency_replaced") == []


def test_a_blocker_holds_the_dependency_for_the_resume(owner):
    plan, job, handle, due = _waiting_on_job()

    blocked = _checkpoint(plan, continuation={"kind": "blocked", "reason": "Needs Peter"})
    wake = blocked["goal"]["continuation"]
    assert (wake["state"], wake["dependency_key"], wake["job_handle"]) == (
        "blocked", "build-1111", handle)

    # Still held through a later checkpoint, and still not droppable by a timer.
    again = _checkpoint(plan, progress="Asked Peter")["goal"]["continuation"]
    assert (again["state"], again["dependency_key"], again["job_handle"]) == (
        "blocked", "build-1111", handle)
    with pytest.raises(ValueError, match="build-1111"):
        _checkpoint(plan, continuation={"kind": "timer", "due_at": db.now_ms() + 60_000})

    _job_ends(job)
    assert _tick() == []                      # blocked: no wake
    current = task_plans.get(plan["plan_id"])
    task_goal_state.mutate(current["plan_id"], revision=current["revision"], action="resume",
                           data={"reason": "Peter answered"})
    assert _continuation(plan)["state"] == "waiting"
    assert _continuation(plan)["due_at"] == due
    prompts = _tick(offset_ms=60_000)
    assert len(prompts) == 1 and '"outcome": "succeeded"' in prompts[0]


def test_without_a_pending_dependency_a_checkpoint_schedules_as_before(owner):
    plan = _goal()
    before = db.now_ms()

    wake = _checkpoint(plan)["goal"]["continuation"]

    assert (wake["state"], wake["reason"], wake["dependency_key"]) == (
        "ready", "Scheduled continuation", "")
    assert before + 120_000 <= wake["due_at"] <= db.now_ms() + 120_000
    assert "dependency_kept" not in _history(plan, "checkpoint")[-1]["detail"]
