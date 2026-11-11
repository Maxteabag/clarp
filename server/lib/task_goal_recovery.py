"""Goal continuation inside the existing scheduler, using canonical dispatch.

Only explicitly enrolled plans participate. Claims survive process loss; a retry
of uncertain admission reuses the same client receipt, never a fresh message ID.
"""

from __future__ import annotations
import json
import secrets
import shlex
from contextlib import contextmanager
from . import agents, db, task_goal_state
from .log import log, log_exception

PREFIX = "task-goal-"


class WakeRejected(ValueError):
    """A goal fence, distinguished from a temporary execution boundary."""

    def __init__(self, code, reason, *, permanent=False):
        self.code = code
        self.permanent = permanent
        super().__init__(reason)


def boundary(plan, goal, *, check_live=True, wake_request=""):
    """Why the goal may not be woken now, or None. ``wake_request`` is the
    wake being validated at its own spawn (validate_dispatch): the state that
    wake wrote while it waited for an account is not a reason to refuse it."""
    from . import (
        artifacts,
        agent_goals,
        backends,
        turn_queue,
        reconcile,
        turn_dispatch,
        heartbeat,
    )

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
    if heartbeat.globally_disabled():
        return "host_paused", "Autonomous wakes are paused on this Host"
    # A user Stop fences goals by pausing every active one
    # (task_plans.pause_recovery_for_agent). An active goal was therefore
    # resumed or created after the latest Stop, and the queue pause the Stop
    # left behind is no fence for it. Comparing queue revisions here instead
    # made the wake fence itself: its own durable receipt bumps the revision
    # (Omar, 2026-10-06 07:35 and 16:35: "Stopped by user; queue is paused").
    queue = turn_queue.state(agent["agent_id"])
    latest = agents.latest_state(agent["agent_id"]) or {}
    detail = latest.get("detail") or {}
    # A turn parked for an account holds every wake, except the parked wake
    # itself when the park ends: refused here, it was settled as a runner
    # that exited though no process ever ran (2026-11-10, seven agents).
    if detail.get("account_recovery") == "waiting" and not (
        wake_request and detail.get("trace_id") == wake_request
    ):
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
        # A switch away never clears the row (AUT-20). The backend the agent
        # left owns it only while its goal is active: the shared Codex
        # app-server may still continue it, and two drivers must never run.
        and (backends.normalize(native_goal.get("backend"))
             == backends.normalize(agent.get("backend"))
             or native_goal["status"] == agent_goals.ACTIVE)
    ):
        # Codex owns native continuation. Never run a competing Host goal loop.
        return (
            "native_owned",
            "Waiting for the agent's existing autonomous objective: "
            + native_goal["status"],
        )
    if check_live:
        live = turn_dispatch.live_work(agent["agent_id"], session=agent["session"])
        queued = queue["count"]
        if queued:
            # A row nothing will run again is not work the owner still has:
            # counted, it held every wake of the agent for as long as it sat.
            queued -= len(turn_queue.dead_rows(agent["agent_id"]))
        if (
            reconcile.has_live_work(agent["agent_id"], agent.get("backend"))
            or live.compacting
            or live.terminal
            or live.queued
            or queued > 0
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
        raise WakeRejected("superseded", "goal wake was superseded or owner changed", permanent=True)
    goal = json.loads(row["goal_json"])
    if goal["continuation"].get("plan_revision") != row["revision"]:
        raise WakeRejected("revision", "goal wake revision was superseded", permanent=True)
    if native_session_id is not None and native_session_id != goal["native_session_id"]:
        raise WakeRejected("native", "goal wake native target changed before spawn", permanent=True)
    gate = boundary(row, goal, check_live=False, wake_request=request_id)
    if gate:
        raise WakeRejected(gate[0], gate[1], permanent=gate[0] in {"owner_changed", "owner_unavailable"})


def _prompt(plan, goal):
    from . import task_plans

    def preview(value, limit):
        text = str(value or "")
        return text if len(text) <= limit else text[:limit] + " [preview truncated; read the current goal]"

    checkpoint = goal.get("checkpoint") or {}
    wake = goal["continuation"]
    documents = task_plans.goal_documents(plan["plan_id"])
    return (
        "Continue your durable outcome commitment. Reassess current conditions and fresh results; "
        "choose your own next actions, collaborators and wake timing within the original authority. "
        "Earlier next-work text is provisional context, not a fixed script. Answering a side question "
        "or ending a turn does not complete this goal. Record a checkpoint with evidence and a "
        "continuation or explicit blocker before yielding. Never self-approve pending permissions.\n\n"
        f"Goal: {plan['plan_id']} (plan revision {plan['revision']}).\n"
        "Before acting, read the authoritative outcome, complete limits, criteria and checkpoint: "
        f"clarp-goal get {shlex.quote(plan['plan_id'])}\n"
        "The following previews are provisional context; they do not replace the full goal.\n"
        f"Outcome: {preview(goal['outcome'], 1024)}\n"
        f"Limits: {preview(goal['limits'], 1024)}\n"
        f"Checkpoint at: {checkpoint.get('at', 'none')}\n"
        f"Progress: {preview(checkpoint.get('progress'), 1536)}\n"
        f"Provisional next work: {preview(checkpoint.get('next_work'), 1536)}\n"
        f"Continuation: {preview(wake.get('reason'), 512)}\n"
        f"Dependency result: {preview(json.dumps(wake.get('dependency_result'), ensure_ascii=False), 512)}\n"
        "Working documents (name, revision; retrieve with clarp-goal read): "
        + json.dumps([(preview(d['name'], 128), d['revision']) for d in documents[:8]], ensure_ascii=False)
        + (" [index truncated; read the current goal]" if len(documents) > 8 else "")
    )


# (key..., error type) -> when it was last logged, ms. A failure that repeats
# every tick is logged with its traceback once, then at most hourly (AUT-09).
_FAILURE_LOGGED: dict[tuple, int] = {}
_FAILURE_LOG_INTERVAL_MS = 3_600_000
_FAILURE_LOG_KEYS = 1024
_OVERFLOW = ("<overflow>",)


def log_failure_throttled(event: str, key: tuple, exc: BaseException, now: int,
                          detail: str = "") -> None:
    """Log a repeating failure: the first per key and error type with its
    traceback, then one line an hour while it keeps happening."""
    slot = (*key, type(exc).__name__)
    last = _FAILURE_LOGGED.get(slot)
    # One of the _FAILURE_LOG_KEYS slots is kept for the overflow marker.
    if last is None and len(_FAILURE_LOGGED) >= _FAILURE_LOG_KEYS - 1:
        # A failure still repeating refreshes its entry at least hourly; only
        # entries quiet for two intervals are dropped, so a live one is never
        # forgotten and traced again. Still full: one aggregate line an hour.
        for stale in [k for k, at in _FAILURE_LOGGED.items()
                      if k != _OVERFLOW and now - at >= 2 * _FAILURE_LOG_INTERVAL_MS]:
            del _FAILURE_LOGGED[stale]
        if len(_FAILURE_LOGGED) >= _FAILURE_LOG_KEYS - 1:
            if now - _FAILURE_LOGGED.get(_OVERFLOW, -_FAILURE_LOG_INTERVAL_MS) >= _FAILURE_LOG_INTERVAL_MS:
                _FAILURE_LOGGED[_OVERFLOW] = now
                log(event, f"{detail} failing ({type(exc).__name__}: {str(exc)[:200]}); "
                           f"more failures than the {_FAILURE_LOG_KEYS - 1} tracked are not tracked")
            return
    if last is None:
        log_exception(event, exc, detail)
    elif now - last >= _FAILURE_LOG_INTERVAL_MS:
        log(event, f"{detail} still failing: {type(exc).__name__}: {str(exc)[:200]}")
    else:
        return
    _FAILURE_LOGGED[slot] = now


def _retire_dead_wakes(now, retired=None):
    """Retire every goal wake row that nothing will run again.

    A wake whose turn ended before its receipt started (parked for an
    account, then refused or failed at the resume) stayed ``queued`` under
    its launch-unproven evidence: never settled, never dispatched, and counted
    as the owner's queued work by every autonomous wake. The wake is the
    scheduler's own and is regenerated from the goal, so its row is cancelled
    here, by rows and not by goals: one whose goal was since paused or
    completed heals too. This pass is also what heals a Host that was updated
    with such rows in place. ``retired(row)`` tells the apps."""
    from . import turn_queue

    for row in turn_queue.dead_rows(prefix=PREFIX):
        queue_id = str(row["queue_id"])
        try:
            if not turn_queue.cancel(queue_id):
                continue
        except Exception as exc:  # noqa: BLE001 - one row never stops the pass
            log_failure_throttled("goalQueueRetireFail", (queue_id,), exc, now, detail=queue_id)
            continue
        log("goalQueueRetired",
            f"session={row['session']} queue={queue_id} reason=undeliverable")
        if retired is not None:
            try:
                retired(row)
            except Exception as exc:  # noqa: BLE001 - the row is retired either way
                log_exception("goalQueueRetiredNotifyFail", exc, detail=queue_id)


def _undelivered_wake(plan, state):
    """How the accepted wake ended without running to its end, or None while
    it waits, runs, completed, or was already noted. Read from the wake's own
    turn and queue row, never from the agent's newest state: that may belong
    to a later turn."""
    from . import turn_queue

    request = state.get("request_id") or ""
    if (
        state.get("state") not in {"admitted", "queued"}
        or not request
        or state.get("undelivered_request_id") == request
    ):
        return None
    turn = (
        db.conn()
        .execute(
            "SELECT settled_at,outcome FROM turns WHERE agent_id=? AND trace_id=? "
            "ORDER BY turn_id DESC LIMIT 1",
            (plan["agent_id"], request),
        )
        .fetchone()
    )
    failed = bool(
        turn and turn["settled_at"] is not None and turn["outcome"] in {"failed", "interrupted"}
    )
    retired = turn_queue.status(request) == "cancelled"
    if not failed and not retired:
        return None
    if failed:
        # The terminal state written with that turn's settlement says why.
        ended = (
            db.conn()
            .execute(
                "SELECT detail FROM state_log WHERE agent_id=? AND ts=? AND kind='interrupted' "
                "ORDER BY state_id DESC LIMIT 1",
                (plan["agent_id"], turn["settled_at"]),
            )
            .fetchone()
        )
        try:
            message = json.loads(ended["detail"] or "{}").get("message") if ended else ""
        except ValueError:
            message = ""
        reason = str(message or "Its turn " + turn["outcome"])
    else:
        reason = "Its queued row was retired before it started"
    return {
        "request_id": request,
        "outcome": turn["outcome"] if failed else "retired",
        "reason": reason,
    }


def tick(dispatch, *, now=None, cursor=None, retired=None):
    now = db.now_ms() if now is None else now
    count = 0
    try:
        _retire_dead_wakes(now, retired)
    except Exception as exc:  # noqa: BLE001 - the goals are still visited (AUT-09)
        log_failure_throttled("goalQueueRetireFail", ("pass",), exc, now)
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
        try:
            claim = _claim(plan_id, now)
        except Exception as exc:  # noqa: BLE001 - one plan never stops the others (AUT-09)
            log_failure_throttled("goalRecoveryClaimFail", (plan_id,), exc, now, detail=plan_id)
            continue
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


def _changed_beyond_observation(stored, goal):
    """Whether goal differs from its stored form in more than the stamp of
    the latest observation (observed_at, observed_reason)."""
    def without_stamp(value):
        continuation = {
            k: v
            for k, v in value.get("continuation", {}).items()
            if k not in {"observed_at", "observed_reason"}
        }
        return {**value, "continuation": continuation}

    return without_stamp(stored) != without_stamp(goal)


def _commit(plan, text, stored, goal, *, enrolled=True):
    """Store goal if the plan still holds text. The JSON is built here, before
    the writer lock; the locked part only compares and writes (a goal can be
    megabytes, and parsing it under the lock stalled every other writer)."""
    new_text = task_goal_state.serialize(goal)
    with _transaction() as con:
        return task_goal_state.save_if_unchanged(
            con, plan, text, stored, goal, new_text, enrolled=enrolled
        )


def _claim(plan_id, now):
    plan = (
        db.conn()
        .execute("SELECT * FROM task_plans WHERE plan_id=?", (plan_id,))
        .fetchone()
    )
    if not plan or plan["status"] != "active" or not plan["recovery_enabled"]:
        return None
    text = plan["goal_json"]
    stored = json.loads(text)
    if (stored["continuation"].get("lease_until") or 0) > now:
        return None
    # Runtime/process inspection must not hold SQLite's global writer lock.
    live_gate = boundary(plan, stored)
    # Everything below works on this snapshot outside the writer lock. Each
    # write is a compare-and-swap on the snapshot (text, revision, status,
    # enrolment): a plan that changed meanwhile, including a lease another
    # scheduler took, is left for the next tick, as a revision change was.
    goal = json.loads(text)
    state = goal["continuation"]
    if state.get("state") in {"blocked", "attention", "not_enrolled"}:
        return None
    changed = False
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
        changed = True
    # The ledger and the apps are told once per wake that it did not run:
    # until this, a goal whose wake failed read as active and admitted.
    undelivered = _undelivered_wake(plan, state)
    if undelivered:
        state.update(
            reason="Wake not delivered: "
            + undelivered["reason"]
            + f"; attempt {state.get('attempts', 0)} of {goal['recovery_limit']}, "
            "the next follows its backoff",
            undelivered_request_id=undelivered["request_id"],
            undelivered_reason=undelivered["reason"],
        )
        goal["history"].append(
            dict(
                at=now,
                kind="wake_undelivered",
                revision=plan["revision"],
                detail=undelivered,
            )
        )
        changed = True
    gate = boundary(plan, goal, check_live=False) or live_gate
    if gate:
        execution, reason = gate
        if state.get("observed_state") != execution:
            state.update(
                observed_state=execution, observed_at=now, observed_reason=reason
            )
            changed = True
        if changed:
            _commit(plan, text, stored, goal)
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
                # How the job ended is not what became of the work: a
                # worker that vanished leaves the outcome unknown.
                "outcome_state": job.get("outcome_state") or "unknown",
                "terminal_reason": job.get("terminal_reason") or "",
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
        # Every tick re-observes an idle owner; rewriting a goal (it can be
        # megabytes) only to restamp that holds the writer lock for nothing.
        if _changed_beyond_observation(stored, goal):
            _commit(plan, text, stored, goal)
        return None
    if state.get("state") == "waiting":
        state.update(
            state="ready",
            reason="External dependency deadline passed; inspect its actual outcome",
        )
    if state.get("attempts", 0) >= goal["recovery_limit"]:
        last = (
            f"; the last wake was not delivered ({state.get('undelivered_reason')})"
            if state.get("request_id")
            and state.get("undelivered_request_id") == state.get("request_id")
            else ""
        )
        state.update(
            state="attention",
            reason=f"Recovery attempt limit reached{last}; checkpoint or resume to continue",
        )
        _commit(plan, text, stored, goal)
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
    if not _commit(plan, text, stored, goal):
        return None
    return dict(plan), goal


def _apply_result(plan, goal, request, now, state, reason):
    """The dispatch outcome on goal; False when it no longer owns that wake."""
    wake = goal["continuation"]
    if wake.get("request_id") != request:
        return False
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
    return True


def _result(plan_id, request, now, state, reason):
    # The outcome of an admitted dispatch must not be lost to a concurrent
    # write: a few compare-and-swap attempts outside the lock, then the
    # locked read-modify-write as before.
    for _attempt in range(3):
        plan = (
            db.conn()
            .execute("SELECT * FROM task_plans WHERE plan_id=?", (plan_id,))
            .fetchone()
        )
        if not plan or plan["status"] != "active":
            return
        text = plan["goal_json"]
        stored = json.loads(text)
        goal = json.loads(text)
        if not _apply_result(plan, goal, request, now, state, reason):
            return
        if _commit(plan, text, stored, goal, enrolled=False):
            return
    with _transaction() as con:
        plan = con.execute(
            "SELECT * FROM task_plans WHERE plan_id=?", (plan_id,)
        ).fetchone()
        if not plan or plan["status"] != "active":
            return
        goal = json.loads(plan["goal_json"])
        if _apply_result(plan, goal, request, now, state, reason):
            task_goal_state._save(con, plan_id, goal)


def allows_paused_queue(request_id):
    """The current wake of an active goal passes a Stop's leftover queue pause.

    Only this wake: other held messages stay paused until the user resumes them.
    """
    row = (
        db.conn()
        .execute(
            "SELECT 1 FROM task_plans WHERE status='active' AND recovery_enabled=1 AND json_extract(goal_json,'$.continuation.request_id')=?",
            (request_id,),
        )
        .fetchone()
    )
    return row is not None


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
