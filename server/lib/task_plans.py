"""Durable two-level agent work plans and server-owned timing."""

from __future__ import annotations

import json
import secrets
import hashlib
from typing import Any

from . import agents, db

ITEM_STATUSES = {
    "pending",
    "in_progress",
    "completed",
    "blocked",
    "skipped",
    "deferred",
    "removed",
}
TERMINAL_ITEM_STATUSES = {"completed", "skipped", "deferred", "removed"}
MAX_PLAN_ITEMS = 500


def _id(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(6)}"


def item_key(plan_id: str, stable_id: str) -> str:
    clean = stable_id.strip() or _id("item")
    if len(clean) > 60:
        clean = clean[:48] + "-" + hashlib.sha256(clean.encode()).hexdigest()[:10]
    return f"{plan_id}:{clean}"


def create(
    *,
    session: str,
    title: str,
    items: list[dict[str, Any]],
    plan_id: str = "",
    goal: dict | None = None,
) -> dict:
    agent = agents.get_by_session(session)
    if not agent:
        raise ValueError("agent session not found")
    title = title.strip()
    if not title:
        raise ValueError("plan title required")
    if not isinstance(items, list) or any(not isinstance(raw, dict) for raw in items):
        raise ValueError("plan items must be objects in a list")
    now = db.now_ms()
    stable_key = plan_id.strip() or "work"
    # Caller IDs are human-stable aliases, not global database keys. Preserve
    # history while allowing different agents and later plans to reuse "ship".
    if len(stable_key) > 60:
        stable_key = (
            stable_key[:48] + "-" + hashlib.sha256(stable_key.encode()).hexdigest()[:10]
        )
    plan_id = f"{agent['agent_id']}:{stable_key}:{secrets.token_hex(4)}"
    con = db.conn()
    con.execute("BEGIN IMMEDIATE")
    try:
        con.execute(
            "INSERT INTO task_plans(plan_id,agent_id,session,title,status,created_at,updated_at) "
            "VALUES(?,?,?,?, 'active',?,?)",
            (plan_id, agent["agent_id"], session, title[:240], now, now),
        )
        position = 0
        item_count = 0
        for raw in items:
            item_count += 1
            if item_count > MAX_PLAN_ITEMS:
                raise ValueError(f"task plan exceeds {MAX_PLAN_ITEMS} items")
            item_id = item_key(plan_id, str(raw.get("id") or _id("task")))
            _insert_item(con, plan_id, item_id, None, position, raw, now)
            for child_position, child in enumerate(raw.get("subtasks") or []):
                if not isinstance(child, dict) or child.get("subtasks"):
                    raise ValueError(
                        "subtasks must be objects with at most two plan levels"
                    )
                item_count += 1
                if item_count > MAX_PLAN_ITEMS:
                    raise ValueError(f"task plan exceeds {MAX_PLAN_ITEMS} items")
                child_id = item_key(plan_id, str(child.get("id") or _id("subtask")))
                _insert_item(
                    con, plan_id, child_id, item_id, child_position, child, now
                )
            position += 1
        from . import artifacts

        if goal is not None:
            from . import task_goal_state

            task_goal_state.initialize(con, plan_id, agent, title, goal, now)
        artifacts.ensure_plan(plan_id=plan_id, session=session, title=title)
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return get(plan_id) or {}


def _insert_item(
    con,
    plan_id: str,
    item_id: str,
    parent_id: str | None,
    position: int,
    raw: dict,
    now: int,
) -> None:
    title = str(raw.get("title") or "").strip()
    if not title:
        raise ValueError("task title required")
    con.execute(
        "INSERT INTO task_items(item_id,plan_id,parent_id,position,title,detail,status,created_at,required) "
        "VALUES(?,?,?,?,?,?, 'pending',?,?)",
        (
            item_id,
            plan_id,
            parent_id,
            position,
            title[:500],
            str(raw.get("detail") or "")[:2000],
            now,
            int(raw.get("required", True)),
        ),
    )


def update_item(
    item_id: str, status: str, detail: str | None = None, *, revision: int | None = None
) -> dict:
    if status not in ITEM_STATUSES:
        raise ValueError("invalid task status")
    if status in {"skipped", "deferred", "removed"} and not (detail or "").strip():
        raise ValueError("omitted work requires a reason")
    con = db.conn()
    now = db.now_ms()
    con.execute("BEGIN IMMEDIATE")
    try:
        row = con.execute(
            "SELECT * FROM task_items WHERE item_id=?", (item_id,)
        ).fetchone()
        if not row:
            raise ValueError("task item not found")
        plan = con.execute(
            "SELECT * FROM task_plans WHERE plan_id=?", (row["plan_id"],)
        ).fetchone()
        if not plan or plan["status"] != "active":
            raise ValueError("task plan is no longer active")
        from . import task_goal_state

        task_goal_state.check_revision(plan, revision)
        task_goal_state.record(
            con,
            plan,
            "step",
            {
                "item_id": item_id,
                "from": row["status"],
                "status": status,
                "reason": detail or "",
            },
            now,
        )
        active_ms = int(row["active_ms"] or 0)
        started_at = row["started_at"]
        if row["status"] == "in_progress" and started_at:
            active_ms += max(0, now - int(started_at))
        next_started = now if status == "in_progress" else None
        completed_at = now if status in TERMINAL_ITEM_STATUSES else None
        con.execute(
            "UPDATE task_items SET status=?, detail=COALESCE(?,detail), started_at=?, "
            "completed_at=?, active_ms=? WHERE item_id=?",
            (
                status,
                detail[:2000] if detail is not None else None,
                next_started,
                completed_at,
                active_ms,
                item_id,
            ),
        )
        con.execute(
            "UPDATE task_plans SET updated_at=?,revision=revision+1 WHERE plan_id=?",
            (now, row["plan_id"]),
        )
        _auto_finish(str(row["plan_id"]), now)
        from . import artifacts

        artifacts.sync_plan(str(row["plan_id"]))
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return get(str(row["plan_id"])) or {}


def finish(
    plan_id: str,
    status: str = "completed",
    *,
    reason: str = "",
    revision: int | None = None,
) -> dict:
    if status not in {"completed", "blocked", "cancelled"}:
        raise ValueError("invalid plan status")
    now = db.now_ms()
    con = db.conn()
    con.execute("BEGIN IMMEDIATE")
    try:
        plan = con.execute(
            "SELECT * FROM task_plans WHERE plan_id=?", (plan_id,)
        ).fetchone()
        if not plan or plan["status"] != "active":
            raise ValueError("task plan is no longer active")
        from . import task_goal_state

        task_goal_state.check_revision(plan, revision)
        if status == "completed":
            task_goal_state.require_completion(con, plan)
        task_goal_state.record(con, plan, status, {"reason": reason}, now)
        _close_running_items(con, plan_id, status, now)
        changed = con.execute(
            "UPDATE task_plans SET status=?,updated_at=?,completed_at=?,revision=revision+1 "
            "WHERE plan_id=? AND status='active'",
            (status, now, now, plan_id),
        )
        if changed.rowcount != 1:
            raise ValueError("task plan is no longer active")
        from . import artifacts

        artifacts.sync_plan(plan_id)
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return get(plan_id) or {}


def _close_running_items(con, plan_id: str, plan_status: str, now: int) -> None:
    terminal = {"cancelled": "pending", "blocked": "blocked", "completed": "completed"}[
        plan_status
    ]
    rows = con.execute(
        "SELECT item_id,active_ms,started_at FROM task_items "
        "WHERE plan_id=? AND status='in_progress'",
        (plan_id,),
    ).fetchall()
    for row in rows:
        elapsed = int(row["active_ms"] or 0)
        if row["started_at"]:
            elapsed += max(0, now - int(row["started_at"]))
        con.execute(
            "UPDATE task_items SET status=?,active_ms=?,started_at=NULL,completed_at=? "
            "WHERE item_id=?",
            (
                terminal,
                elapsed,
                now if terminal in TERMINAL_ITEM_STATUSES else None,
                row["item_id"],
            ),
        )


def _auto_finish(plan_id: str, now: int) -> None:
    rows = (
        db.conn()
        .execute("SELECT status FROM task_items WHERE plan_id=?", (plan_id,))
        .fetchall()
    )
    plan = (
        db.conn()
        .execute("SELECT goal_json FROM task_plans WHERE plan_id=?", (plan_id,))
        .fetchone()
    )
    if json.loads(plan["goal_json"] or "{}"):
        return  # Durable goals require explicit criteria evidence and completion.
    if rows and all(row["status"] == "completed" for row in rows):
        db.conn().execute(
            "UPDATE task_plans SET status='completed',updated_at=?,completed_at=? "
            "WHERE plan_id=? AND status='active'",
            (now, now, plan_id),
        )


def active_for_session(session: str) -> dict | None:
    row = (
        db.conn()
        .execute(
            "SELECT plan_id FROM task_plans WHERE session=? AND status='active' "
            "ORDER BY updated_at DESC,rowid DESC LIMIT 1",
            (session,),
        )
        .fetchone()
    )
    return get(row["plan_id"]) if row else None


def cancel_for_agent(agent_id: str) -> None:
    con = db.conn()
    now = db.now_ms()
    rows = con.execute(
        "SELECT plan_id FROM task_plans WHERE agent_id=? AND status='active'",
        (agent_id,),
    ).fetchall()
    for row in rows:
        plan_id = str(row["plan_id"])
        _close_running_items(con, plan_id, "cancelled", now)
        con.execute(
            "UPDATE task_plans SET status='cancelled',updated_at=?,completed_at=? "
            "WHERE plan_id=?",
            (now, now, plan_id),
        )


def get(plan_id: str) -> dict | None:
    plan = (
        db.conn()
        .execute("SELECT * FROM task_plans WHERE plan_id=?", (plan_id,))
        .fetchone()
    )
    if not plan:
        return None
    now = db.now_ms()
    items = []
    for row in db.conn().execute(
        "SELECT * FROM task_items WHERE plan_id=? ORDER BY parent_id IS NOT NULL,parent_id,position",
        (plan_id,),
    ):
        item = dict(row)
        item["subtasks"] = []
        elapsed = int(item.get("active_ms") or 0)
        if item["status"] == "in_progress" and item.get("started_at"):
            elapsed += max(0, now - int(item["started_at"]))
        item["elapsed_ms"] = elapsed
        items.append(item)
    roots = []
    by_parent: dict[str, list] = {}
    for item in items:
        if item["parent_id"]:
            by_parent.setdefault(item["parent_id"], []).append(item)
        else:
            roots.append(item)
    for root in roots:
        root["subtasks"] = by_parent.get(root["item_id"], [])
    completed = sum(1 for item in items if item["status"] == "completed")
    data = dict(plan)
    data["goal"] = json.loads(data.pop("goal_json") or "{}") or None
    data["history"] = json.loads(data.pop("history_json") or "[]")
    if data["goal"] is not None:
        data["goal"]["documents"] = goal_documents(plan_id)
    data["legacy"] = data["goal"] is None
    data["completion_verified"] = bool(data["goal"] and data["status"] == "completed")
    counts = {
        status: sum(i["status"] == status for i in items) for status in ITEM_STATUSES
    }
    data["counts"] = counts
    return {
        **data,
        "items": roots,
        "completed_count": completed,
        "total_count": len(items),
        "server_now": now,
    }


def list_for_session(session: str) -> list[dict]:
    return [
        get(r["plan_id"])
        for r in db.conn().execute(
            "SELECT plan_id FROM task_plans WHERE session=? ORDER BY updated_at DESC,rowid DESC LIMIT 100",
            (session,),
        )
    ]


# Outcome commitments share the task plan store and its write transactions.
def _goal_check_revision(plan, revision):
    if revision is None and json.loads(plan["goal_json"] or "{}"):
        raise ValueError("revision required for durable goal")
    if revision is not None and revision != plan["revision"]:
        raise ValueError("plan changed; reload before editing")


def _goal_save(con, plan_id, goal):
    con.execute(
        "UPDATE task_plans SET goal_json=? WHERE plan_id=?",
        (json.dumps(goal, ensure_ascii=False), plan_id),
    )


def _goal_initialize(con, plan_id, agent, title, raw, now):
    outcome = str(raw.get("outcome") or title).strip()
    criteria = raw.get("criteria")
    if not isinstance(criteria, list) or not criteria or len(criteria) > 100:
        raise ValueError("goal requires 1–100 completion criteria")
    if not isinstance(raw.get("limits"), str) or not raw["limits"].strip():
        raise ValueError(
            'explicit goal limits required (use "No additional limits" if applicable)'
        )
    texts = [str(c).strip() for c in criteria]
    if any(not c for c in texts):
        raise ValueError("completion criteria must not be empty")
    native = agents.live_backend_session(agent["agent_id"]) or ""
    enroll = raw.get("enroll", False)
    if not isinstance(enroll, bool):
        raise ValueError("enroll must be a boolean")
    if enroll and not native:
        raise ValueError("recovery enrollment requires a bound native conversation")
    goal = dict(
        outcome=outcome,
        limits=raw["limits"],
        owner_agent_id=agent["agent_id"],
        native_session_id=native,
        criteria=[
            dict(id=f"criterion-{i + 1}", text=c, evidence="")
            for i, c in enumerate(texts)
        ],
        history=[dict(at=now, kind="created", revision=0, detail={"outcome": outcome})],
        checkpoint=None,
        continuation=dict(
            generation=0,
            state="ready" if enroll else "not_enrolled",
            reason="Awaiting owner checkpoint" if enroll else "Recovery not enrolled",
            due_at=now + 120000 if enroll else None,
            attempts=0,
            lease_until=None,
            request_id="",
        ),
        recovery_limit=max(1, min(20, int(raw.get("recovery_limit", 5)))),
    )
    _goal_save(con, plan_id, goal)
    con.execute(
        "UPDATE task_plans SET recovery_enabled=? WHERE plan_id=?",
        (int(enroll), plan_id),
    )


def _goal_record(con, plan, kind, detail, now):
    goal = json.loads(plan["goal_json"] or "{}")
    if not goal:
        # Preserve observed changes, without inventing historical evidence.
        history = json.loads(plan["history_json"] or "[]")
        history.append(
            dict(at=now, kind=kind, revision=plan["revision"] + 1, detail=detail)
        )
        con.execute(
            "UPDATE task_plans SET history_json=? WHERE plan_id=?",
            (json.dumps(history), plan["plan_id"]),
        )
        return
    if kind in {"completed", "cancelled", "blocked"}:
        wake = goal["continuation"]
        reason = detail.get("reason") or (
            "Completion criteria have recorded evidence"
            if kind == "completed"
            else "Goal " + kind
        )
        wake.update(
            state=kind,
            reason=reason,
            due_at=None,
            request_id="",
            lease_until=None,
            generation=wake["generation"] + 1,
            observed_state=kind,
            observed_reason=reason,
            observed_at=now,
        )
    goal["history"].append(
        dict(at=now, kind=kind, revision=plan["revision"] + 1, detail=detail)
    )
    _goal_save(con, plan["plan_id"], goal)


def _goal_require_completion(con, plan):
    goal = json.loads(plan["goal_json"] or "{}")
    rows = con.execute(
        "SELECT status,required FROM task_items WHERE plan_id=?", (plan["plan_id"],)
    ).fetchall()
    if any(r["required"] and r["status"] not in {"completed", "removed"} for r in rows):
        raise ValueError("required work remains unresolved")
    if any(r["status"] == "in_progress" for r in rows):
        raise ValueError("running work must be resolved or deferred before completion")
    if goal and any(not c["evidence"].strip() for c in goal["criteria"]):
        raise ValueError("completion requires evidence for every acceptance criterion")
    if not goal and any(r["status"] != "completed" for r in rows):
        raise ValueError("unfinished legacy work cannot be completed")


def _goal_mutate(plan_id, *, revision, action, data=None):
    """Single transaction for checkpoint/evidence/next work and wake registration."""
    from . import task_plans, artifacts

    data = data or {}
    con = db.conn()
    now = db.now_ms()
    con.execute("BEGIN IMMEDIATE")
    try:
        plan = con.execute(
            "SELECT * FROM task_plans WHERE plan_id=?", (plan_id,)
        ).fetchone()
        if not plan:
            raise ValueError("task plan not found")
        _goal_check_revision(plan, revision)
        goal = json.loads(plan["goal_json"] or "{}")
        was_legacy = not goal
        if action == "enroll" and not goal:
            if plan["status"] != "active":
                raise ValueError("only active legacy work can enroll")
            agent = agents.get_by_agent_id(plan["agent_id"])
            _goal_initialize(con, plan_id, agent, plan["title"], data, now)
            goal = json.loads(
                con.execute(
                    "SELECT goal_json FROM task_plans WHERE plan_id=?", (plan_id,)
                ).fetchone()[0]
            )
        if not goal:
            raise ValueError("legacy plan must be explicitly enrolled as a goal")
        if plan["status"] in {"completed", "cancelled", "superseded"}:
            raise ValueError("goal is closed")
        reason = str(data.get("reason") or "").strip()
        state = goal["continuation"]
        if action == "enroll" and not was_legacy:
            if data.get("enroll") is not True or not reason:
                raise ValueError(
                    "explicit enrollment requires enroll:true and a reason"
                )
            native = agents.live_backend_session(plan["agent_id"])
            if not native or goal["native_session_id"] not in {"", native}:
                raise ValueError(
                    "verify and rebind the owner conversation before enrollment"
                )
            goal["native_session_id"] = native
            con.execute(
                "UPDATE task_plans SET recovery_enabled=1 WHERE plan_id=?", (plan_id,)
            )
            state.update(
                generation=state["generation"] + 1, request_id="", lease_until=None
            )
            if plan["status"] == "active":
                state.update(
                    state="ready",
                    due_at=now + 15000,
                    reason=reason,
                    observed_state="ready",
                    observed_reason=reason,
                    observed_at=now,
                )
        elif action == "enroll":
            pass
        elif action in {"pause", "cancel", "supersede", "block", "resume"}:
            if not reason:
                raise ValueError("goal control requires a reason")
            if action == "supersede":
                other = con.execute(
                    "SELECT agent_id FROM task_plans WHERE plan_id=?",
                    (data.get("replacement_id", ""),),
                ).fetchone()
                if (
                    not other
                    or other["agent_id"] != plan["agent_id"]
                    or data["replacement_id"] == plan_id
                ):
                    raise ValueError(
                        "replacement must be another plan owned by the same agent"
                    )
                goal["superseded_by"] = data["replacement_id"]
            if action == "resume":
                from . import turn_queue

                state["resume_queue_revision"] = turn_queue.state(plan["agent_id"])[
                    "revision"
                ]
            status = {
                "pause": "paused",
                "cancel": "cancelled",
                "supersede": "superseded",
                "block": "blocked",
                "resume": "active",
            }[action]
            # A dependency still awaited when the goal is paused or blocked is
            # awaited again on resume, with its deadline: its result (a job
            # that ended meanwhile, say) is then recorded and delivered,
            # instead of a bare resume wake that never reads it.
            if action in {"pause", "block"} and state.get("state") == "waiting":
                state["paused_dependency_due_at"] = state.get("due_at")
            awaited = (action == "resume" and state.get("dependency_key")
                       and "paused_dependency_due_at" in state
                       and not state.get("dependency_result"))
            paused_due = (state.pop("paused_dependency_due_at", None)
                          if action in {"resume", "cancel", "supersede"} else None)
            resumed = ("waiting" if awaited else "ready")
            # A result held through the pause is delivered now, not after the
            # usual resume delay.
            held_result = (action == "resume" and paused_due is not None
                           and bool(state.get("dependency_result")))
            state.update(
                generation=state["generation"] + 1,
                state=resumed if action == "resume" else status,
                reason=reason,
                observed_state=resumed if action == "resume" else status,
                observed_reason=reason,
                observed_at=now,
                due_at=(max(paused_due or 0, now + 15000) if awaited
                        else now if held_result else now + 15000)
                if action == "resume" else None,
                lease_until=None,
                request_id="",
                attempts=0,
            )
            con.execute(
                "UPDATE task_plans SET status=?,completed_at=? WHERE plan_id=?",
                (status, now if action in {"cancel", "supersede"} else None, plan_id),
            )
            if action != "resume":
                task_plans._close_running_items(
                    con, plan_id, "blocked" if action == "block" else "cancelled", now
                )
        elif action == "rebind":
            agent = agents.get_by_agent_id(plan["agent_id"])
            native = agents.live_backend_session(plan["agent_id"])
            if (
                plan["status"] != "paused"
                or not reason
                or not native
                or data.get("native_session_id") != native
            ):
                raise ValueError(
                    "pause first, then explicitly name the verified current native conversation and reason"
                )
            goal["native_session_id"] = native
            con.execute(
                "UPDATE task_plans SET session=? WHERE plan_id=?",
                (agent["session"], plan_id),
            )
            state.update(
                generation=state["generation"] + 1,
                request_id="",
                lease_until=None,
                reason="Owner conversation rebound; explicitly resume when authorized",
            )
        elif action == "document":
            _write_goal_documents(con, plan_id, [data], now)
        elif action == "checkpoint":
            if plan["status"] != "active":
                raise ValueError("resume goal before checkpointing")
            progress = str(data.get("progress") or "").strip()
            next_work = str(data.get("next_work") or "").strip()
            if not progress or not next_work:
                raise ValueError("checkpoint requires progress and next work")
            for cid, evidence in (data.get("evidence") or {}).items():
                criterion = next((c for c in goal["criteria"] if c["id"] == cid), None)
                if criterion is None or not str(evidence).strip():
                    raise ValueError("unknown criterion or empty evidence")
                criterion["evidence"] = str(evidence)
            _write_goal_documents(con, plan_id, data.get("documents") or [], now)
            goal["checkpoint"] = dict(
                at=now,
                progress=progress,
                next_work=next_work,
                evidence=data.get("evidence") or {},
            )
            wake = data.get("continuation") or {}
            kind = wake.get("kind", "timer")
            if kind not in {"timer", "dependency", "blocked"}:
                raise ValueError("invalid continuation kind")
            due = int(wake.get("due_at") or now + 120000)
            if kind == "dependency" and (
                not wake.get("key") or not wake.get("reason") or due <= now
            ):
                raise ValueError(
                    "dependency requires key, wait reason and future timeout"
                )
            if kind == "blocked" and not wake.get("reason"):
                raise ValueError("blocker requires a reason")
            job_handle = str(wake.get("job_handle") or "")
            if job_handle:
                from . import background_jobs

                job_id, generation = background_jobs.parse_job_handle(job_handle)
                job = background_jobs.get(job_id, reconcile=False)
                if (
                    kind != "dependency"
                    or not job
                    or job["agent_id"] != plan["agent_id"]
                    or job["generation"] != generation
                ):
                    raise ValueError(
                        "background dependency must match this owner and job generation"
                    )
            state.update(job_handle=job_handle)
            state.update(
                generation=state["generation"] + 1,
                state="waiting"
                if kind == "dependency"
                else ("blocked" if kind == "blocked" else "ready"),
                reason=str(wake.get("reason") or "Scheduled continuation"),
                due_at=None if kind == "blocked" else max(now, due),
                dependency_key=str(wake.get("key") or ""),
                dependency_result=None,
                request_id="",
                lease_until=None,
                attempts=0,
            )
        elif action == "dependency":
            # A result that arrives while the user has the goal paused or
            # blocked is kept for the resume, which then delivers it.
            held = (state.get("state") in {"paused", "blocked"}
                    and "paused_dependency_due_at" in state
                    and not state.get("dependency_result"))
            if (state.get("state") != "waiting" and not held) or data.get("key") != state.get(
                "dependency_key"
            ):
                raise ValueError("dependency is stale or does not belong to this goal")
            if data.get("outcome") not in {"succeeded", "failed"} or not data.get(
                "evidence"
            ):
                raise ValueError("external result requires outcome and evidence")
            if held:
                state["dependency_result"] = data
            else:
                state.update(
                    state="ready",
                    due_at=now,
                    reason="External work " + data["outcome"],
                    dependency_result=data,
                    request_id="",
                )
        elif action == "replan":
            if not reason:
                raise ValueError("replanning requires a discovery or reason")
            proposed = data.get("steps")
            if (
                not isinstance(proposed, list)
                or len(proposed) > task_plans.MAX_PLAN_ITEMS
            ):
                raise ValueError("replan requires a list of at most 500 steps")
            old = [
                dict(r)
                for r in con.execute(
                    "SELECT * FROM task_items WHERE plan_id=?", (plan_id,)
                )
            ]
            goal["history"].append(
                dict(
                    at=now,
                    kind="prior_steps",
                    revision=revision + 1,
                    detail={"items": old},
                )
            )
            keys = set()
            for position, raw in enumerate(proposed):
                key = task_plans.item_key(plan_id, str(raw.get("id") or ""))
                if key in keys:
                    raise ValueError("duplicate step id")
                keys.add(key)
                existing = next((r for r in old if r["item_id"] == key), None)
                if existing:
                    if (
                        existing["status"] == "completed"
                        and str(raw.get("title") or "").strip() != existing["title"]
                    ):
                        raise ValueError(
                            "completed step meaning is immutable; use a new step id for different work"
                        )
                    if not str(raw.get("title") or "").strip():
                        raise ValueError("step title required")
                    con.execute(
                        "UPDATE task_items SET title=?,position=?,parent_id=NULL WHERE item_id=?",
                        (raw["title"], position, key),
                    )
                else:
                    task_plans._insert_item(con, plan_id, key, None, position, raw, now)
            for item in old:
                if item["item_id"] not in keys and item["status"] != "completed":
                    elapsed = item["active_ms"] + (
                        max(0, now - item["started_at"]) if item["started_at"] else 0
                    )
                    con.execute(
                        "UPDATE task_items SET status='removed',detail=?,started_at=NULL,active_ms=?,completed_at=? WHERE item_id=?",
                        (reason, elapsed, now, item["item_id"]),
                    )
            state.update(
                generation=state["generation"] + 1, request_id="", lease_until=None
            )
            if state["state"] not in {"blocked", "waiting"}:
                state.update(
                    state="ready",
                    due_at=now + 120000,
                    reason="Plan revised; reassess current results",
                )
        elif action == "add_step":
            if not reason:
                raise ValueError("plan adaptation requires a reason")
            key = task_plans.item_key(
                plan_id, str(data.get("id") or secrets.token_hex(4))
            )
            position = con.execute(
                "SELECT COUNT(*) FROM task_items WHERE plan_id=?", (plan_id,)
            ).fetchone()[0]
            if position >= task_plans.MAX_PLAN_ITEMS:
                raise ValueError("too many steps")
            task_plans._insert_item(con, plan_id, key, None, position, data, now)
        else:
            raise ValueError("unknown goal action")
        enabled = con.execute(
            "SELECT recovery_enabled FROM task_plans WHERE plan_id=?", (plan_id,)
        ).fetchone()[0]
        if not enabled:
            state["due_at"] = None
            if state["state"] in {
                "ready",
                "dispatching",
                "admitted",
                "queued",
                "retry",
            }:
                state.update(
                    state="not_enrolled",
                    reason="Automatic continuation is not enrolled",
                    observed_state="not_enrolled",
                    observed_reason="Automatic continuation is not enrolled",
                )
        # Content lives in its versioned document rows, not duplicated in the event log.
        detail = {k: v for k, v in data.items() if k not in {"content", "documents"}}
        if data.get("documents"):
            detail["documents"] = [d["name"] for d in data["documents"]]
        goal["history"].append(
            dict(at=now, kind=action, revision=revision + 1, detail=detail)
        )
        _goal_save(con, plan_id, goal)
        con.execute(
            "UPDATE task_plans SET revision=revision+1,updated_at=? WHERE plan_id=?",
            (now, plan_id),
        )
        artifacts.sync_plan(plan_id)
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise
    return task_plans.get(plan_id)


def pause_recovery_for_agent(agent_id: str, reason: str) -> None:
    """Persist explicit Stop beyond a later unrelated user turn unpausing queues."""
    rows = (
        db.conn()
        .execute(
            "SELECT plan_id,revision FROM task_plans WHERE agent_id=? AND goal_json!='{}' AND status='active'",
            (agent_id,),
        )
        .fetchall()
    )
    for row in rows:
        _goal_mutate(
            row["plan_id"],
            revision=row["revision"],
            action="pause",
            data={"reason": reason},
        )


def _write_goal_documents(con, plan_id, documents, now):
    if not isinstance(documents, list) or len(documents) > 32:
        raise ValueError("checkpoint supports at most 32 document changes")
    names = set()
    for document in documents:
        name = str(document.get("name") or "").strip()
        if not name or len(name) > 160 or name in names:
            raise ValueError("unique document name required, at most 160 characters")
        names.add(name)
        format = document.get("format")
        content = document.get("content")
        reason = str(document.get("reason") or "").strip()
        if (
            format not in {"markdown", "json"}
            or (format == "markdown" and not isinstance(content, str))
            or not reason
        ):
            raise ValueError(
                "document requires markdown/json format, valid content and change reason"
            )
        encoded = json.dumps(content, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode()) > 65536:
            raise ValueError(
                "document exceeds 64 KiB; reference a managed evidence file instead"
            )
        row = con.execute(
            "SELECT MAX(revision) FROM task_goal_documents WHERE plan_id=? AND name=?",
            (plan_id, name),
        ).fetchone()
        current = row[0] or 0
        if document.get("document_revision") != current:
            raise ValueError("document changed; reload its current revision")
        con.execute(
            "INSERT INTO task_goal_documents(plan_id,name,revision,format,content_json,reason,updated_at) VALUES(?,?,?,?,?,?,?)",
            (plan_id, name, current + 1, format, encoded, reason, now),
        )


def goal_documents(plan_id):
    return [
        dict(r)
        for r in db.conn().execute(
            "SELECT name,MAX(revision) AS revision,format,reason,updated_at FROM task_goal_documents WHERE plan_id=? GROUP BY name ORDER BY name",
            (plan_id,),
        )
    ]


def goal_document(plan_id, name, revision=None):
    sql = "SELECT * FROM task_goal_documents WHERE plan_id=? AND name=?"
    params = [plan_id, name]
    if revision is not None:
        sql += " AND revision=?"
        params.append(revision)
    row = db.conn().execute(sql + " ORDER BY revision DESC LIMIT 1", params).fetchone()
    if not row:
        raise ValueError("goal document not found")
    result = dict(row)
    result["content"] = json.loads(result.pop("content_json"))
    result["text"] = (
        result["content"]
        if result["format"] == "markdown"
        else json.dumps(result["content"], ensure_ascii=False, indent=2)
    )
    result["history"] = [
        dict(r)
        for r in db.conn().execute(
            "SELECT revision,reason,updated_at FROM task_goal_documents WHERE plan_id=? AND name=? ORDER BY revision DESC",
            (plan_id, name),
        )
    ]
    return result


def recovery_owns_agent(agent_id):
    return (
        db.conn()
        .execute(
            "SELECT 1 FROM task_plans WHERE agent_id=? AND recovery_enabled=1 AND status IN ('active','paused','blocked') LIMIT 1",
            (agent_id,),
        )
        .fetchone()
        is not None
    )
