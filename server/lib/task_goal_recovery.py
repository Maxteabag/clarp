"""Goal continuation inside the existing scheduler, using canonical dispatch.

Only explicitly enrolled plans participate. Claims survive process loss; a retry
of uncertain admission reuses the same client receipt, never a fresh message ID.
"""

from __future__ import annotations
import json
import secrets
from contextlib import contextmanager
from . import agents, db, task_goal_state

PREFIX = "task-goal-"


def boundary(plan, goal, *, check_live=True):
    from . import (
        artifacts,
        agent_goals,
        turn_queue,
        reconcile,
        turn_dispatch,
        heartbeat,
    )

    if heartbeat.globally_disabled():
        return "host_paused", "Autonomous wakes are paused on this Host"
    agent = agents.get_by_agent_id(plan["agent_id"])
    if not agent or agent.get("deleted_at") or agent.get("archived_at"):
        return "owner_unavailable", "Owner is unavailable"
    if (
        agent["session"] != plan["session"]
        or goal["owner_agent_id"] != agent["agent_id"]
    ):
        return "owner_changed", "Owner binding changed; explicit re-enrollment required"
    native = agents.live_backend_session(agent["agent_id"])
    if not native or native != goal["native_session_id"]:
        return (
            "owner_changed",
            "Native conversation changed; explicit re-enrollment required",
        )
    queue = turn_queue.state(agent["agent_id"])
    if (
        queue["paused"]
        and goal["continuation"].get("resume_queue_revision") != queue["revision"]
    ):
        return "paused", "Stopped by user; queue is paused"
    latest = agents.latest_state(agent["agent_id"]) or {}
    detail = latest.get("detail") or {}
    if detail.get("account_recovery") == "waiting":
        return "capacity", detail.get(
            "message"
        ) or "Waiting for available account capacity"
    if (
        detail.get("reason") in {"usage_limit", "auth"}
        and (goal["continuation"].get("due_at") or 0) > db.now_ms()
        and goal["continuation"].get("last_dispatch_at")
    ):
        return "capacity", (
            detail.get("message") or "Capacity unavailable"
        ) + "; retry is backed off"
    if artifacts.has_blocking_decision(agent["agent_id"]):
        return "approval", "Waiting for an answer or approval"
    native_goal = agent_goals.get(agent["agent_id"])
    if (
        native_goal
        and native_goal.get("native")
        and native_goal["status"] != "complete"
    ):
        # Codex owns native continuation. Never run a competing Host goal loop.
        return (
            "native_owned",
            "Waiting for the agent's existing autonomous objective: "
            + native_goal["status"],
        )
    if check_live:
        live = turn_dispatch.live_work(agent["agent_id"], session=agent["session"])
        if (
            reconcile.has_live_work(agent["agent_id"], agent.get("backend"))
            or live.compacting
            or live.terminal
            or live.queued
        ):
            return "working", "Owner has live or queued work"
    return None


def validate_dispatch(agent, request_id, *, native_session_id=None):
    """Fence stale/paused/wrong-owner wakes at both admission and actual spawn."""
    if not request_id.startswith(PREFIX):
        return
    row = (
        db.conn()
        .execute(
            "SELECT * FROM task_plans WHERE recovery_enabled=1 AND status='active' "
            "AND json_extract(goal_json,'$.continuation.request_id')=?",
            (request_id,),
        )
        .fetchone()
    )
    if not row or row["agent_id"] != agent.get("agent_id"):
        raise ValueError("goal wake was superseded or owner changed")
    goal = json.loads(row["goal_json"])
    if goal["continuation"].get("plan_revision") != row["revision"]:
        raise ValueError("goal wake revision was superseded")
    if native_session_id is not None and native_session_id != goal["native_session_id"]:
        raise ValueError("goal wake native target changed before spawn")
    gate = boundary(row, goal, check_live=False)
    if gate:
        raise ValueError(gate[1])


def _prompt(plan, goal):
    from . import task_plans

    return (
        "Continue your durable outcome commitment. Reassess current conditions and fresh results; "
        "choose your own next actions, collaborators and wake timing within the original authority. "
        "Earlier next-work text is provisional context, not a fixed script. Answering a side question "
        "or ending a turn does not complete this goal. Record a checkpoint with evidence and a "
        "continuation or explicit blocker before yielding. Never self-approve pending permissions.\n"
        + json.dumps(
            dict(
                plan_id=plan["plan_id"],
                revision=plan["revision"],
                outcome=goal["outcome"],
                limits=goal["limits"],
                criteria=goal["criteria"],
                checkpoint=goal.get("checkpoint"),
                continuation=goal["continuation"],
                documents=task_plans.goal_documents(plan["plan_id"]),
            ),
            ensure_ascii=False,
        )
    )


def tick(dispatch, *, now=None, cursor=None):
    now = db.now_ms() if now is None else now
    count = 0
    after = cursor[0] if cursor else ""
    # Keyset batches visit every enrolled commitment, including when the first
    # batch is permanently waiting. A fixed oldest-100 slice would starve others.
    query = (
        "SELECT plan_id FROM task_plans WHERE recovery_enabled=1 AND status='active' "
        "AND plan_id>? ORDER BY plan_id LIMIT 100"
    )
    ids = [r[0] for r in db.conn().execute(query, (after,))]
    if not ids and after:
        ids = [r[0] for r in db.conn().execute(query, ("",))]
    if cursor is not None:
        cursor[:] = [ids[-1] if ids else ""]
    for plan_id in ids:
        claim = _claim(plan_id, now)
        if not claim:
            continue
        plan, goal = claim
        request = goal["continuation"]["request_id"]
        try:
            result = dispatch(plan["session"], _prompt(plan, goal), request)
            if result is None:
                raise RuntimeError("dispatcher returned no admission receipt")
            queued = (
                bool(result.get("queued"))
                if isinstance(result, dict)
                else bool(result.queued)
            )
            _result(
                plan_id,
                request,
                now,
                "queued" if queued else "admitted",
                "Canonical dispatch accepted the wake",
            )
            count += 1
        except Exception as exc:
            # Keep request ID: response loss may have occurred after admission.
            _result(plan_id, request, now, "retry", str(exc)[:500])
    return count


def _claim(plan_id, now):
    snapshot = (
        db.conn()
        .execute("SELECT * FROM task_plans WHERE plan_id=?", (plan_id,))
        .fetchone()
    )
    if (
        not snapshot
        or snapshot["status"] != "active"
        or not snapshot["recovery_enabled"]
    ):
        return None
    previous = json.loads(snapshot["goal_json"])
    if (previous["continuation"].get("lease_until") or 0) > now:
        return None
    # Runtime/process inspection must not hold SQLite's global writer lock.
    live_gate = boundary(snapshot, previous)
    with _transaction() as con:
        plan = con.execute(
            "SELECT * FROM task_plans WHERE plan_id=?", (plan_id,)
        ).fetchone()
        if not plan or plan["status"] != "active" or not plan["recovery_enabled"]:
            return None
        goal = json.loads(plan["goal_json"])
        state = goal["continuation"]
        if state.get("state") in {"blocked", "attention", "not_enrolled"}:
            return None
        # A still-live lease belongs to another scheduler, including after a restart.
        if (state.get("lease_until") or 0) > now:
            return None
        if plan["revision"] != snapshot["revision"]:
            return None
        latest = agents.latest_state(plan["agent_id"]) or {}
        if (
            state.get("last_dispatch_at")
            and latest.get("ts", 0) >= state["last_dispatch_at"]
            and latest.get("kind") in {"done", "idle", "interrupted"}
            and latest.get("ts") != state.get("execution_observed_at")
        ):
            detail = latest.get("detail") or {}
            state["last_execution"] = {
                "kind": latest["kind"],
                "reason": detail.get("reason", ""),
                "message": detail.get(
                    "message", "Owner turn ended; outcome remains unfinished"
                ),
            }
            state["execution_observed_at"] = latest["ts"]
            goal["history"].append(
                dict(
                    at=latest["ts"],
                    kind="execution_observed",
                    revision=plan["revision"],
                    detail=state["last_execution"],
                )
            )
            task_goal_state._save(con, plan_id, goal)
        gate = boundary(plan, goal, check_live=False) or live_gate
        if gate:
            execution, reason = gate
            if state.get("observed_state") != execution:
                state.update(
                    observed_state=execution, observed_at=now, observed_reason=reason
                )
                task_goal_state._save(con, plan_id, goal)
            return None
        state.update(
            observed_state="idle", observed_at=now, observed_reason="Owner is idle"
        )
        if state.get("state") == "waiting" and state.get("job_handle"):
            from . import background_jobs

            job_id, generation = background_jobs.parse_job_handle(state["job_handle"])
            job = background_jobs.get(job_id, reconcile=False)
            if (
                not job
                or job["agent_id"] != plan["agent_id"]
                or job["generation"] != generation
            ):
                state.update(
                    state="ready",
                    due_at=now,
                    reason="Background dependency disappeared or was superseded; inspect current work",
                )
            elif job["status"] in background_jobs.TERMINAL_STATUSES:
                result = {
                    "key": state["dependency_key"],
                    "outcome": job["status"],
                    "evidence": job.get("log_path")
                    or job.get("terminal_reason")
                    or job.get("detail")
                    or "Recorded job terminal status",
                    "job_handle": state["job_handle"],
                }
                state.update(
                    state="ready",
                    due_at=now,
                    reason="Background job " + job["status"],
                    dependency_result=result,
                )
                goal["history"].append(
                    dict(
                        at=now,
                        kind="dependency_result",
                        revision=plan["revision"],
                        detail=result,
                    )
                )
        if (state.get("due_at") or 0) > now:
            task_goal_state._save(con, plan_id, goal)
            return None
        if state.get("state") == "waiting":
            state.update(
                state="ready",
                reason="External dependency deadline passed; inspect its actual outcome",
            )
        if state.get("attempts", 0) >= goal["recovery_limit"]:
            state.update(
                state="attention",
                reason="Recovery attempt limit reached; checkpoint or resume to continue",
            )
            task_goal_state._save(con, plan_id, goal)
            return None
        # Successful admission followed by an idle owner is an unfinished turn,
        # not a completed goal. A new generation gets one new receipt.
        if (
            state.get("state") in {"admitted", "queued"}
            or state.get("plan_revision", plan["revision"]) != plan["revision"]
        ):
            state.update(generation=state["generation"] + 1, request_id="")
        if not state.get("request_id"):
            state["request_id"] = PREFIX + secrets.token_hex(16)
        state.update(
            state="dispatching",
            plan_revision=plan["revision"],
            lease_until=now + 60000,
            attempts=state.get("attempts", 0) + 1,
        )
        goal["history"].append(
            dict(
                at=now,
                kind="wake_claim",
                revision=plan["revision"],
                detail={
                    "request_id": state["request_id"],
                    "generation": state["generation"],
                },
            )
        )
        task_goal_state._save(con, plan_id, goal)
        return dict(plan), goal


def _result(plan_id, request, now, state, reason):
    with _transaction() as con:
        plan = con.execute(
            "SELECT * FROM task_plans WHERE plan_id=?", (plan_id,)
        ).fetchone()
        if not plan or plan["status"] != "active":
            return
        goal = json.loads(plan["goal_json"])
        wake = goal["continuation"]
        if wake.get("request_id") != request:
            return
        backoff = min(3600000, 120000 * 2 ** min(wake.get("attempts", 1) - 1, 5))
        wake.update(
            state=state,
            reason=reason,
            lease_until=None,
            due_at=now + backoff,
            last_dispatch_at=now,
        )
        goal["history"].append(
            dict(
                at=now,
                kind="dispatch_" + state,
                revision=plan["revision"],
                detail={"request_id": request, "reason": reason},
            )
        )
        task_goal_state._save(con, plan_id, goal)


def allows_paused_queue(request_id):
    from . import turn_queue

    row = (
        db.conn()
        .execute(
            "SELECT agent_id,goal_json FROM task_plans WHERE status='active' AND recovery_enabled=1 AND json_extract(goal_json,'$.continuation.request_id')=?",
            (request_id,),
        )
        .fetchone()
    )
    if not row:
        return False
    state = json.loads(row["goal_json"])["continuation"]
    return (
        state.get("resume_queue_revision")
        == turn_queue.state(row["agent_id"])["revision"]
    )


@contextmanager
def _transaction():
    con = db.conn()
    con.execute("BEGIN IMMEDIATE")
    try:
        yield con
    except BaseException:
        con.execute("ROLLBACK")
        raise
    else:
        con.execute("COMMIT")
