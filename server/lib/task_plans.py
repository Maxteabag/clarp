"""Durable two-level agent work plans and server-owned timing."""
from __future__ import annotations

import secrets
import hashlib
from typing import Any

from . import agents, db

ITEM_STATUSES = {"pending", "in_progress", "completed", "blocked", "skipped"}
TERMINAL_ITEM_STATUSES = {"completed", "skipped"}
MAX_PLAN_ITEMS = 500


def _id(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(6)}"


def item_key(plan_id: str, stable_id: str) -> str:
    clean = stable_id.strip() or _id("item")
    if len(clean) > 60:
        clean = clean[:48] + "-" + hashlib.sha256(clean.encode()).hexdigest()[:10]
    return f"{plan_id}:{clean}"


def create(*, session: str, title: str, items: list[dict[str, Any]],
           plan_id: str = "") -> dict:
    agent = agents.get_by_session(session)
    if not agent:
        raise ValueError("agent session not found")
    title = title.strip()
    if not title:
        raise ValueError("plan title required")
    now = db.now_ms()
    stable_key = plan_id.strip() or "work"
    # Caller IDs are human-stable aliases, not global database keys. Preserve
    # history while allowing different agents and later plans to reuse "ship".
    if len(stable_key) > 60:
        stable_key = (stable_key[:48] + "-"
                      + hashlib.sha256(stable_key.encode()).hexdigest()[:10])
    plan_id = f"{agent['agent_id']}:{stable_key}:{secrets.token_hex(4)}"
    con = db.conn()
    con.execute("BEGIN IMMEDIATE")
    try:
        old_plans = con.execute(
            "SELECT plan_id FROM task_plans WHERE agent_id=? AND status='active'",
            (agent["agent_id"],)).fetchall()
        for old in old_plans:
            _close_running_items(con, str(old["plan_id"]), "cancelled", now)
        con.execute(
            "UPDATE task_plans SET status='cancelled', completed_at=?, updated_at=? "
            "WHERE agent_id=? AND status='active'", (now, now, agent["agent_id"]))
        con.execute(
            "INSERT INTO task_plans(plan_id,agent_id,session,title,status,created_at,updated_at) "
            "VALUES(?,?,?,?, 'active',?,?)",
            (plan_id, agent["agent_id"], session, title[:240], now, now))
        position = 0; item_count = 0
        for raw in items[:100]:
            item_count += 1
            if item_count > MAX_PLAN_ITEMS:
                raise ValueError(f"task plan exceeds {MAX_PLAN_ITEMS} items")
            item_id = item_key(plan_id, str(raw.get("id") or _id("task")))
            _insert_item(con, plan_id, item_id, None, position, raw, now)
            for child_position, child in enumerate((raw.get("subtasks") or [])[:100]):
                item_count += 1
                if item_count > MAX_PLAN_ITEMS:
                    raise ValueError(f"task plan exceeds {MAX_PLAN_ITEMS} items")
                child_id = item_key(plan_id, str(child.get("id") or _id("subtask")))
                _insert_item(con, plan_id, child_id, item_id, child_position, child, now)
            position += 1
        from . import artifacts
        for old in old_plans:
            artifacts.sync_plan(str(old["plan_id"]))
        artifacts.ensure_plan(plan_id=plan_id, session=session, title=title)
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return get(plan_id) or {}


def _insert_item(con, plan_id: str, item_id: str, parent_id: str | None,
                 position: int, raw: dict, now: int) -> None:
    title = str(raw.get("title") or "").strip()
    if not title:
        raise ValueError("task title required")
    con.execute(
        "INSERT INTO task_items(item_id,plan_id,parent_id,position,title,detail,status,created_at) "
        "VALUES(?,?,?,?,?,?, 'pending',?)",
        (item_id, plan_id, parent_id, position, title[:500],
         str(raw.get("detail") or "")[:2000], now))


def update_item(item_id: str, status: str, detail: str | None = None) -> dict:
    if status not in ITEM_STATUSES:
        raise ValueError("invalid task status")
    con = db.conn(); now = db.now_ms()
    con.execute("BEGIN IMMEDIATE")
    try:
        row = con.execute("SELECT * FROM task_items WHERE item_id=?", (item_id,)).fetchone()
        if not row:
            raise ValueError("task item not found")
        plan = con.execute(
            "SELECT status FROM task_plans WHERE plan_id=?", (row["plan_id"],)).fetchone()
        if not plan or plan["status"] != "active":
            raise ValueError("task plan is no longer active")
        active_ms = int(row["active_ms"] or 0)
        started_at = row["started_at"]
        if row["status"] == "in_progress" and started_at:
            active_ms += max(0, now - int(started_at))
        next_started = now if status == "in_progress" else None
        completed_at = now if status in TERMINAL_ITEM_STATUSES else None
        con.execute(
            "UPDATE task_items SET status=?, detail=COALESCE(?,detail), started_at=?, "
            "completed_at=?, active_ms=? WHERE item_id=?",
            (status, detail[:2000] if detail is not None else None, next_started,
             completed_at, active_ms, item_id))
        con.execute("UPDATE task_plans SET updated_at=? WHERE plan_id=?",
                    (now, row["plan_id"]))
        _auto_finish(str(row["plan_id"]), now)
        from . import artifacts
        artifacts.sync_plan(str(row["plan_id"]))
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return get(str(row["plan_id"])) or {}


def finish(plan_id: str, status: str = "completed") -> dict:
    if status not in {"completed", "blocked", "cancelled"}:
        raise ValueError("invalid plan status")
    now = db.now_ms(); con = db.conn()
    con.execute("BEGIN IMMEDIATE")
    try:
        plan = con.execute(
            "SELECT status FROM task_plans WHERE plan_id=?", (plan_id,)).fetchone()
        if not plan or plan["status"] != "active":
            raise ValueError("task plan is no longer active")
        _close_running_items(con, plan_id, status, now)
        changed = con.execute(
            "UPDATE task_plans SET status=?,updated_at=?,completed_at=? "
            "WHERE plan_id=? AND status='active'", (status, now, now, plan_id))
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
    terminal = {"cancelled": "skipped", "blocked": "blocked", "completed": "completed"}[
        plan_status]
    rows = con.execute(
        "SELECT item_id,active_ms,started_at FROM task_items "
        "WHERE plan_id=? AND status='in_progress'", (plan_id,)).fetchall()
    for row in rows:
        elapsed = int(row["active_ms"] or 0)
        if row["started_at"]:
            elapsed += max(0, now - int(row["started_at"]))
        con.execute(
            "UPDATE task_items SET status=?,active_ms=?,started_at=NULL,completed_at=? "
            "WHERE item_id=?", (terminal, elapsed, now, row["item_id"]))


def _auto_finish(plan_id: str, now: int) -> None:
    rows = db.conn().execute(
        "SELECT status FROM task_items WHERE plan_id=?", (plan_id,)).fetchall()
    if rows and all(row["status"] in TERMINAL_ITEM_STATUSES for row in rows):
        db.conn().execute(
            "UPDATE task_plans SET status='completed',updated_at=?,completed_at=? "
            "WHERE plan_id=? AND status='active'", (now, now, plan_id))


def active_for_session(session: str) -> dict | None:
    row = db.conn().execute(
        "SELECT plan_id FROM task_plans WHERE session=? AND status='active' "
        "ORDER BY updated_at DESC LIMIT 1", (session,)).fetchone()
    return get(row["plan_id"]) if row else None


def cancel_for_agent(agent_id: str) -> None:
    con = db.conn(); now = db.now_ms()
    rows = con.execute(
        "SELECT plan_id FROM task_plans WHERE agent_id=? AND status='active'",
        (agent_id,)).fetchall()
    for row in rows:
        plan_id = str(row["plan_id"])
        _close_running_items(con, plan_id, "cancelled", now)
        con.execute(
            "UPDATE task_plans SET status='cancelled',updated_at=?,completed_at=? "
            "WHERE plan_id=?", (now, now, plan_id))


def get(plan_id: str) -> dict | None:
    plan = db.conn().execute("SELECT * FROM task_plans WHERE plan_id=?", (plan_id,)).fetchone()
    if not plan:
        return None
    now = db.now_ms()
    items = []
    for row in db.conn().execute(
        "SELECT * FROM task_items WHERE plan_id=? ORDER BY parent_id IS NOT NULL,parent_id,position",
        (plan_id,)):
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
    completed = sum(1 for item in items if item["status"] in TERMINAL_ITEM_STATUSES)
    return {**dict(plan), "items": roots, "completed_count": completed,
            "total_count": len(items), "server_now": now}
