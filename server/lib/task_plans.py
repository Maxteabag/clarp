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
        from . import goal_ledger
        goal_ledger.guard_principal_plan(con, agent["agent_id"])   # any plan, goal or not
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
        if not plan or not _accepts_step_updates(plan):
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
        # A paused goal's work is not running: its clock stays stopped.
        next_started = now if status == "in_progress" and plan["status"] == "active" else None
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


def _accepts_step_updates(plan) -> bool:
    """Active plans, and paused durable goals: pausing stops the continuation,
    not the bookkeeping of what already happened."""
    if plan["status"] == "active":
        return True
    return plan["status"] == "paused" and bool(json.loads(plan["goal_json"] or "{}"))


def _freeze_running_items(con, plan_id: str, now: int) -> None:
    """Pausing keeps a step in progress but stops its clock (``paused`` in
    reads). Resetting it to pending lost the record that work had started;
    leaving the clock running showed it as active for days (2026-11-13)."""
    for row in con.execute(
        "SELECT item_id,active_ms,started_at FROM task_items "
        "WHERE plan_id=? AND status='in_progress' AND started_at IS NOT NULL",
        (plan_id,),
    ).fetchall():
        elapsed = int(row["active_ms"] or 0) + max(0, now - int(row["started_at"]))
        con.execute(
            "UPDATE task_items SET active_ms=?,started_at=NULL WHERE item_id=?",
            (elapsed, row["item_id"]),
        )


def _thaw_running_items(con, plan_id: str, now: int) -> None:
    con.execute(
        "UPDATE task_items SET started_at=? WHERE plan_id=? AND status='in_progress' "
        "AND started_at IS NULL",
        (now, plan_id),
    )


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
    from . import goal_ledger

    for row in rows:
        plan_id = str(row["plan_id"])
        _close_running_items(con, plan_id, "cancelled", now)
        con.execute(
            "UPDATE task_plans SET status='cancelled',updated_at=?,completed_at=? "
            "WHERE plan_id=?",
            (now, now, plan_id),
        )
        if con.execute("SELECT goal_json FROM task_plans WHERE plan_id=?", (plan_id,)).fetchone()[0] not in ("", "{}"):
            goal_ledger.record_status_change(con, plan_id, kind="cancelled", status="cancelled",
                                             reason="Owner agent deleted")


def get(plan_id: str) -> dict | None:
    return get_with_goal_text(plan_id)[0]


def get_with_goal_text(plan_id: str) -> tuple[dict | None, str | None]:
    plan, goal_text, _bound = get_with_goal_text_and_bound(plan_id)
    return plan, goal_text


def _utf8_bound(text: str) -> int:
    return len(text) if text.isascii() else 4 * len(text)


def get_with_goal_text_and_bound(plan_id: str) -> tuple[dict | None, str | None, int]:
    """The plan as ``get`` returns it, and the exact ``goal_json`` text its goal
    was decoded from, and an upper bound on the UTF-8 size of the goal's
    history as read. The artifact list's summary-v1 projection keys its plan
    history digest on that text (``_goal_save`` rewrites it without a revision
    bump, so only the text itself proves the history unchanged) and checks the
    bound before it serializes the history: with ledger references the stored
    text is small while the expanded history is not."""
    plan = (
        db.conn()
        .execute("SELECT * FROM task_plans WHERE plan_id=?", (plan_id,))
        .fetchone()
    )
    if not plan:
        return None, None, 0
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
    paused = plan["status"] == "paused"
    for item in items:
        # The record stays in_progress; while the goal is paused it is not running.
        item["paused"] = paused and item["status"] == "in_progress"
        if item["parent_id"]:
            by_parent.setdefault(item["parent_id"], []).append(item)
        else:
            roots.append(item)
    for root in roots:
        root["subtasks"] = by_parent.get(root["item_id"], [])
    completed = sum(1 for item in items if item["status"] == "completed")
    data = dict(plan)
    goal_text = data.pop("goal_json")
    data["goal"] = json.loads(goal_text or "{}") or None
    bound = _utf8_bound(goal_text or "")
    if data["goal"] is not None:
        bound += _goal_expand(db.conn(), plan_id, data["goal"])
    data["history"] = json.loads(data.pop("history_json") or "[]")
    if data["goal"] is not None:
        from . import goal_keeper, goal_ledger

        data["goal"]["documents"] = goal_documents(plan_id)
        data.update(goal_ledger.summary(data))
        data["updater"] = goal_keeper.public_state(plan_id)
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
    }, goal_text, bound


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


# The goal keeps its newest history entries inline; the append-only ledger keeps
# every entry. Without a bound, routine wake entries grew single goals to tens of
# megabytes, and every save of such a goal held SQLite's writer lock for seconds.
GOAL_HISTORY_LIMIT = 200


def _goal_bounded(goal):
    """The goal as stored: at most GOAL_HISTORY_LIMIT history entries, and
    ``history_offset`` counting the older ones that live only in the ledger."""
    history = goal.get("history") or []
    excess = len(history) - GOAL_HISTORY_LIMIT
    if excess <= 0:
        return goal
    return {
        **goal,
        "history": history[excess:],
        "history_offset": int(goal.get("history_offset") or 0) + excess,
    }


def _goal_serialize(goal):
    """The stored text of a goal; callers can build it outside a transaction."""
    return json.dumps(_goal_bounded(goal), ensure_ascii=False)


def _goal_adopt_bound(goal):
    """Trim the caller's goal in place to what was stored."""
    bounded = _goal_bounded(goal)
    if bounded is not goal:
        goal["history"] = bounded["history"]
        goal["history_offset"] = bounded["history_offset"]


# A goal history entry's large detail may be stored as a reference to the
# append-only goal ledger, which mirrors every entry under "history:<index>":
# {"ledger": key, "shape": key order}. The ledger keeps keys sorted, so the
# shape records the original order wherever it differs. Reading expands every
# reference back, byte for byte (AUT-N1b-reader). This build writes none;
# the writer ships one release later, so a rollback to this build reads them.
_LEDGER_REF = "ledger"
_SHAPE = "shape"


def _reshape(value, shape):
    if shape is None:
        return value
    if "k" in shape and isinstance(value, dict):
        kids = shape.get("c") or {}
        ordered = {key: value[key] for key in shape["k"] if key in value}
        ordered.update((key, item) for key, item in value.items() if key not in ordered)
        return {key: _reshape(item, kids.get(key)) for key, item in ordered.items()}
    if "a" in shape and isinstance(value, list):
        return [_reshape(item, shape["a"]) for item in value]
    if "l" in shape and isinstance(value, list):
        return [_reshape(item, sub) for item, sub in zip(value, shape["l"])] + value[len(shape["l"]):]
    return value


def _goal_expand(con, plan_id, goal):
    """The goal as clients read it: each spilled detail back from the ledger,
    in its original key order. Returns an upper bound on the UTF-8 bytes the
    expansion added."""
    added = 0
    for position, entry in enumerate(goal.get("history") or []):
        detail = entry.get("detail")
        if not isinstance(detail, dict) or not (set(detail) <= {_LEDGER_REF, _SHAPE}) \
                or _LEDGER_REF not in detail:
            continue
        row = con.execute(
            "SELECT new_json FROM goal_events WHERE plan_id=? AND idempotency_key=?",
            (plan_id, detail[_LEDGER_REF])).fetchone()
        recorded = (json.loads(row[0] or "{}").get("detail") or {}) if row else {}
        goal["history"][position] = {**entry, "detail": _reshape(recorded, detail.get(_SHAPE))}
        # The ledger row's text (spaced separators) is longer than the
        # detail's compact form, so it bounds what this entry adds.
        added += _utf8_bound(row[0] or "") if row else 0
    return added


def _goal_save(con, plan_id, goal):
    from . import goal_ledger

    row = con.execute("SELECT goal_json FROM task_plans WHERE plan_id=?", (plan_id,)).fetchone()
    old = json.loads(row[0] or "{}") if row else {}
    # Every appended history entry also lands in the append-only ledger, from
    # the full history, before the stored copy drops its oldest entries.
    goal_ledger.record_goal_save(con, plan_id, old, goal)
    con.execute(
        "UPDATE task_plans SET goal_json=? WHERE plan_id=?",
        (_goal_serialize(goal), plan_id),
    )
    _goal_adopt_bound(goal)


def _goal_save_if_unchanged(con, plan, expected_text, old, goal, text, *, enrolled):
    """Store ``text`` (``_goal_serialize(goal)``, built before the transaction)
    only if the plan still holds ``expected_text`` at the same revision and
    status. The writer lock covers this comparison and write, not the JSON work.
    Returns whether it was stored; a changed plan is left for its next reader."""
    from . import goal_ledger

    sql = (
        "UPDATE task_plans SET goal_json=? WHERE plan_id=? AND revision=? "
        "AND status='active' AND goal_json=?"
    )
    if enrolled:
        sql += " AND recovery_enabled=1"
    cursor = con.execute(sql, (text, plan["plan_id"], plan["revision"], expected_text))
    if cursor.rowcount != 1:
        return False
    goal_ledger.record_goal_save(con, plan["plan_id"], old, goal)
    _goal_adopt_bound(goal)
    return True


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
        from . import goal_ledger
        goal_ledger.guard_principal_plan(con, plan["agent_id"])
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
    if goal and any(not c["evidence"].strip() and not c.get("waived")
                    for c in goal["criteria"]):
        raise ValueError(
            "completion requires evidence for every acceptance criterion "
            "(or the owner's waiver of it, with a reason)")
    if not goal and any(r["status"] != "completed" for r in rows):
        raise ValueError("unfinished legacy work cannot be completed")


# Internal callers that act on the plan as it is (Stop's pause) read the
# revision inside _goal_mutate's own transaction instead of passing one.
_REVISION_IN_TRANSACTION = object()


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
        if revision is _REVISION_IN_TRANSACTION:
            revision = plan["revision"]
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

                # Recorded for the goal ledger; no longer a wake fence (see
                # task_goal_recovery.boundary).
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
            if action == "pause":
                task_plans._freeze_running_items(con, plan_id, now)
            elif action == "resume":
                task_plans._thaw_running_items(con, plan_id, now)
            else:
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
            paused = plan["status"] == "paused"
            if plan["status"] not in {"active", "paused"}:
                raise ValueError("resume goal before checkpointing")
            if paused and data.get("continuation"):
                raise ValueError(
                    "Nothing was saved: the goal is paused, so a checkpoint records progress "
                    "and evidence only. Send it again without continuation, or resume the goal first")
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
            # Paused: the continuation is the pause's (rejected above if sent).
            wake = {} if paused else (data.get("continuation") or {})
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
            # A dependency the goal still waits for is never dropped as a
            # side effect. A routine progress checkpoint, with a timer or with
            # no continuation at all, used to replace it with a two-minute
            # timer: the job ended and woke nobody (2026-11-10).
            # Also one a blocker holds for the resume.
            pending = bool(state.get("dependency_key")) and (
                state.get("state") == "waiting"
                or (state.get("state") == "blocked" and "paused_dependency_due_at" in state)
            )
            awaited = {
                "key": str(state.get("dependency_key") or ""),
                "job_handle": str(state.get("job_handle") or ""),
            }
            named = awaited["key"] + (
                f" (job {awaited['job_handle']})" if awaited["job_handle"] else ""
            )
            replaces = kind == "dependency" or wake.get("replace_dependency") is True
            if paused:
                pass  # the pause owns the continuation; resume decides the next wake
            elif pending and not wake:
                # No continuation: the checkpoint is saved and the wake stands.
                state.update(
                    generation=state["generation"] + 1,
                    request_id="",
                    lease_until=None,
                    attempts=0,
                )
                data = {**data, "dependency_kept": {**awaited, "due_at": state.get("due_at")}}
            elif pending and kind == "timer" and not replaces:
                raise ValueError(
                    f"Nothing was saved: goal is waiting on dependency {named}, and a timer "
                    "would replace that wake. Send the checkpoint again: omit continuation to "
                    "keep waiting for it; to be woken earlier, send the dependency "
                    "continuation again with an earlier due_at; to stop waiting for it "
                    '(also when it has already ended), add "replace_dependency": true'
                )
            elif pending and kind == "blocked":
                # Held as a pause holds it: the resume awaits it again and a
                # result that arrived meanwhile is delivered then.
                state.setdefault("paused_dependency_due_at", state.get("due_at"))
                state.update(
                    generation=state["generation"] + 1,
                    state="blocked",
                    reason=str(wake["reason"]),
                    due_at=None,
                    request_id="",
                    lease_until=None,
                    attempts=0,
                )
            else:
                if pending and str(wake.get("key") or "") != awaited["key"]:
                    by = "dependency " + str(wake["key"]) if kind == "dependency" else kind
                    goal["history"].append(
                        dict(
                            at=now,
                            kind="dependency_replaced",
                            revision=revision + 1,
                            detail={
                                **awaited,
                                "by": by,
                                "reason": "Checkpoint replaced the pending dependency on "
                                + awaited["key"],
                            },
                        )
                    )
                state.pop("paused_dependency_due_at", None)
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
                    title = str(raw.get("title") or "").strip()[:500]
                    if existing["status"] == "completed" and title != existing["title"]:
                        raise ValueError(
                            "completed step meaning is immutable; use a new step id for different work"
                        )
                    if not title:
                        raise ValueError("step title required")
                    con.execute(
                        "UPDATE task_items SET title=?,position=?,parent_id=NULL WHERE item_id=?",
                        (title, position, key),
                    )
                    if existing["status"] == "removed":
                        # Proposing a retired step again puts it back in the
                        # plan; left removed it would count as resolved.
                        con.execute(
                            "UPDATE task_items SET status='pending',detail=?,started_at=NULL,"
                            "completed_at=NULL WHERE item_id=?",
                            (str(raw.get("detail") or "")[:2000], key),
                        )
                else:
                    task_plans._insert_item(con, plan_id, key, None, position, raw, now)
            for item in old:
                if item["item_id"] not in keys and item["status"] not in {"completed", "removed"}:
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
        elif action in {"waive", "unwaive"}:
            # A criterion that will not be met as written (a target the work
            # fell short of, a requirement that changed) can be set aside by
            # the owner, with a reason, so the goal can complete honestly
            # instead of being cancelled. The waiver is shown, never hidden.
            # Only the user may set a criterion aside: an agent that could waive its
            # own criteria could complete any goal without evidence (review #311,
            # finding 2), also one the user paused.
            from . import goal_ledger
            if goal_ledger.acting_kind() != "user":
                raise ValueError("only the user can waive or restore a criterion")
            cid = str(data.get("criterion_id") or "")
            criterion = next((c for c in goal["criteria"] if c["id"] == cid), None)
            if criterion is None:
                raise ValueError("unknown criterion")
            if action == "waive":
                if not reason:
                    raise ValueError("waiving a criterion requires a reason")
                criterion["waived"] = dict(at=now, reason=reason[:2000], by="user")
            else:
                if not criterion.get("waived"):
                    raise ValueError("criterion is not waived")
                criterion.pop("waived", None)
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


def pause_recovery_for_agent(agent_id: str, reason: str) -> int:
    """Persist explicit Stop beyond a later unrelated user turn unpausing queues.

    Returns how many active goals this call paused (each one committed)."""
    rows = (
        db.conn()
        .execute(
            "SELECT plan_id FROM task_plans WHERE agent_id=? AND goal_json!='{}' AND status='active'",
            (agent_id,),
        )
        .fetchall()
    )
    paused = 0
    for row in rows:
        # The agent may checkpoint while Stop runs; one goal's failure must not
        # leave the others active.
        try:
            _goal_mutate(
                row["plan_id"],
                revision=_REVISION_IN_TRANSACTION,
                action="pause",
                data={"reason": reason},
            )
        except Exception as exc:  # noqa: BLE001 - attempt every goal
            from .log import log_exception

            log_exception("goalPauseOnStopFail", exc, detail=row["plan_id"])
            continue
        paused += 1
    return paused


def carry_native_session(agent_id: str, from_session_id: str, to_session_id: str,
                         reason: str) -> list[str]:
    """Follow a native conversation the Host itself continued in a new one.

    A goal is bound to its owner's native conversation and stops when that
    changes, because a silent swap means the agent lost its context. When the
    Host carries the conversation over (agy prunes old ones, and Clarp opens
    the new one with its record of the old), the goal follows it instead, and
    a wake already running stays valid. Returns the plans that moved.
    """
    from . import artifacts, goal_ledger

    if not agent_id or not from_session_id or not to_session_id:
        return []
    con = db.conn()
    now = db.now_ms()
    moved = []
    with goal_ledger.acting_as("system"):
        con.execute("BEGIN IMMEDIATE")
        try:
            for plan in con.execute(
                    "SELECT * FROM task_plans WHERE agent_id=? AND goal_json!='{}' "
                    "AND status NOT IN ('completed','cancelled','superseded')",
                    (agent_id,)).fetchall():
                goal = json.loads(plan["goal_json"])
                if goal.get("native_session_id") != from_session_id:
                    continue
                goal["native_session_id"] = to_session_id
                state = goal.setdefault("continuation", {})
                if state.get("plan_revision") == plan["revision"]:
                    state["plan_revision"] = plan["revision"] + 1
                goal.setdefault("history", []).append(dict(
                    at=now, kind="rebind", revision=plan["revision"] + 1,
                    detail={"reason": reason, "from_native_session_id": from_session_id,
                            "native_session_id": to_session_id}))
                _goal_save(con, plan["plan_id"], goal)
                con.execute(
                    "UPDATE task_plans SET revision=revision+1,updated_at=? WHERE plan_id=?",
                    (now, plan["plan_id"]))
                artifacts.sync_plan(plan["plan_id"])
                moved.append(plan["plan_id"])
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise
    return moved


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


# ---- the Goal keeper Janitor's effect, goal_update (lib/goal_keeper.py) ----

UPDATER_STEP_STATUSES = {"pending", "in_progress", "completed", "blocked"}
# Steps the owner closed stay closed: the updater records progress, it does not
# reopen decisions (a completed, skipped, deferred or removed step).
_UPDATER_FROZEN = {"completed", "skipped", "deferred", "removed"}


def updater_apply(
    plan_id: str,
    *,
    seen_revision: int,
    steps: dict[str, dict],
    progress: str,
    next_work: str,
    model: str,
    source_refs: list[str],
    through_ms: int = 0,
    through_event: int = 0,
    janitor_agent_id: str = "",
) -> dict:
    """Record what the Goal keeper observed, beside the owner's own record.

    ``steps`` maps item_id -> {"status", "note"}. Only step statuses and the
    updater's own checkpoint (``goal["updater"]``) change: never the plan's
    status, its continuation, criteria or evidence, and never the owner's
    checkpoint. The plan revision is not bumped, so the owner's next
    revision-checked command is never refused because of the updater; instead
    the write is refused when the owner changed the plan since the updater
    read it (``seen_revision``), and the updater reads again.
    Returns {"changed_steps": [...], "stored": bool}.
    """
    from . import goal_ledger

    progress = str(progress or "").strip()[:2000]
    next_work = str(next_work or "").strip()[:1000]
    if not progress:
        raise ValueError("updater checkpoint requires progress")
    con = db.conn()
    now = db.now_ms()
    con.execute("BEGIN IMMEDIATE")
    try:
        plan = con.execute("SELECT * FROM task_plans WHERE plan_id=?", (plan_id,)).fetchone()
        if not plan or plan["status"] not in {"active", "paused"}:
            raise ValueError("goal is closed")
        if plan["revision"] != seen_revision:
            raise ValueError("plan changed; reload before editing")
        goal = json.loads(plan["goal_json"] or "{}")
        if not goal:
            raise ValueError("not a durable goal")
        running = plan["status"] == "active"
        rows = {r["item_id"]: r for r in con.execute(
            "SELECT * FROM task_items WHERE plan_id=?", (plan_id,)).fetchall()}
        changed = []
        for item_id, wanted in steps.items():
            row = rows.get(item_id)
            status = str((wanted or {}).get("status") or "")
            if not row or status not in UPDATER_STEP_STATUSES:
                continue
            if row["status"] in _UPDATER_FROZEN or row["status"] == status:
                continue
            active_ms = int(row["active_ms"] or 0)
            if row["status"] == "in_progress" and row["started_at"]:
                active_ms += max(0, now - int(row["started_at"]))
            con.execute(
                "UPDATE task_items SET status=?,started_at=?,completed_at=?,active_ms=? "
                "WHERE item_id=?",
                (status, now if status == "in_progress" and running else None,
                 now if status == "completed" else None, active_ms, item_id))
            changed.append({"item_id": item_id, "from": row["status"], "status": status,
                            "note": str((wanted or {}).get("note") or "")[:500]})
        # through_*: where the next update starts reading (lib/goal_keeper.py).
        goal["updater"] = dict(at=now, progress=progress, next_work=next_work, model=model,
                               source_refs=list(source_refs)[:40],
                               changed_steps=[c["item_id"] for c in changed],
                               through_ms=int(through_ms or now),
                               through_event=int(through_event or 0),
                               by="goal-keeper", janitor_agent_id=janitor_agent_id)
        goal["history"].append(dict(
            at=now, kind="updater", revision=plan["revision"],
            detail={"reason": "Goal keeper: " + progress[:200], "by": "goal-keeper",
                    "model": model, "steps": changed, "source_refs": list(source_refs)[:40]}))
        with goal_ledger.acting_as("system", janitor_agent_id, delegation_id="goal-keeper"):
            _goal_save(con, plan_id, goal)
        con.execute("UPDATE task_plans SET updated_at=? WHERE plan_id=?", (now, plan_id))
        from . import artifacts

        artifacts.sync_plan(plan_id)
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise
    return {"changed_steps": changed, "stored": True}
