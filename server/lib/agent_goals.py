"""One goal per agent: the objective an agent keeps working toward on its own.

The Codex backend owns its goal natively (``thread/goal/*`` in the app-server
protocol) and starts continuation turns itself; this table is Clarp's mirror
of that state so the app can show it, and the record of who asked for it.
Other backends do not carry a goal yet: ``backends.goal`` refuses them.
"""
from __future__ import annotations

from typing import Any

from .protocol import SSEType

SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_goals (
    agent_id TEXT PRIMARY KEY,
    session TEXT NOT NULL,
    backend TEXT NOT NULL,
    objective TEXT NOT NULL,
    status TEXT NOT NULL,
    token_budget INTEGER,
    tokens_used INTEGER NOT NULL DEFAULT 0,
    time_used_seconds INTEGER NOT NULL DEFAULT 0,
    native INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
"""

ACTIVE = "active"
PAUSED = "paused"
BLOCKED = "blocked"
USAGE_LIMITED = "usage_limited"
BUDGET_LIMITED = "budget_limited"
COMPLETE = "complete"
STATUSES = (ACTIVE, PAUSED, BLOCKED, USAGE_LIMITED, BUDGET_LIMITED, COMPLETE)

# Codex spells its states in camelCase; the wire and the table use snake_case.
_FROM_CODEX = {"usageLimited": USAGE_LIMITED, "budgetLimited": BUDGET_LIMITED}
_TO_CODEX = {value: key for key, value in _FROM_CODEX.items()}

_COLUMNS = ("agent_id", "session", "backend", "objective", "status", "token_budget",
            "tokens_used", "time_used_seconds", "native", "created_at", "updated_at")


def normalize_status(value: Any) -> str:
    text = str(value or "").strip()
    return _FROM_CODEX.get(text, text)


def codex_status(value: str) -> str:
    return _TO_CODEX.get(value, value)


def from_codex(goal: dict[str, Any]) -> dict[str, Any]:
    """The ``ThreadGoal`` shape the app-server reports, as a table row."""
    return {
        "objective": str(goal.get("objective") or ""),
        "status": normalize_status(goal.get("status")),
        "token_budget": goal.get("tokenBudget"),
        "tokens_used": int(goal.get("tokensUsed") or 0),
        "time_used_seconds": int(goal.get("timeUsedSeconds") or 0),
        "created_at": int(goal.get("createdAt") or 0) or None,
        "updated_at": int(goal.get("updatedAt") or 0) or None,
    }


def _db():
    # db imports this module's SCHEMA at load time; import it lazily here.
    from . import db
    return db


def _row(record) -> dict[str, Any] | None:
    if record is None:
        return None
    return dict(zip(_COLUMNS, record))


def get(agent_id: str) -> dict[str, Any] | None:
    if not agent_id:
        return None
    record = _db().conn().execute(
        f"SELECT {', '.join(_COLUMNS)} FROM agent_goals WHERE agent_id = ?",
        (agent_id,)).fetchone()
    return _row(record)


def by_agent() -> dict[str, dict[str, Any]]:
    rows = _db().conn().execute(f"SELECT {', '.join(_COLUMNS)} FROM agent_goals").fetchall()
    return {str(record[0]): dict(zip(_COLUMNS, record)) for record in rows}


def upsert(agent_id: str, *, session: str, backend: str, goal: dict[str, Any],
           native: bool = True) -> dict[str, Any]:
    """Store the current goal for an agent, replacing any earlier one.

    ``goal`` carries the ``from_codex`` keys; missing timestamps fall back to
    what the table already holds, then to now.
    """
    status = normalize_status(goal.get("status")) or ACTIVE
    if status not in STATUSES:
        raise ValueError(f"unknown goal status: {status}")
    now = _db().now_ms()
    previous = get(agent_id) or {}
    created_at = int(goal.get("created_at") or previous.get("created_at") or now)
    updated_at = int(goal.get("updated_at") or now)
    _db().conn().execute(
        """INSERT INTO agent_goals (agent_id, session, backend, objective, status,
                token_budget, tokens_used, time_used_seconds, native, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(agent_id) DO UPDATE SET
                session = excluded.session, backend = excluded.backend,
                objective = excluded.objective, status = excluded.status,
                token_budget = excluded.token_budget, tokens_used = excluded.tokens_used,
                time_used_seconds = excluded.time_used_seconds, native = excluded.native,
                created_at = excluded.created_at, updated_at = excluded.updated_at""",
        (agent_id, session, backend, str(goal.get("objective") or ""), status,
         goal.get("token_budget"), int(goal.get("tokens_used") or 0),
         int(goal.get("time_used_seconds") or 0), 1 if native else 0,
         created_at, updated_at))
    return get(agent_id) or {}


def clear(agent_id: str) -> bool:
    cursor = _db().conn().execute("DELETE FROM agent_goals WHERE agent_id = ?", (agent_id,))
    return cursor.rowcount > 0


def public(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """What ``/agents/snapshot`` and ``/agent-goal`` return for an agent."""
    if not row:
        return None
    return {
        "objective": row["objective"],
        "status": row["status"],
        "token_budget": row["token_budget"],
        "tokens_used": int(row["tokens_used"] or 0),
        "time_used_seconds": int(row["time_used_seconds"] or 0),
        "native": bool(row["native"]),
        "backend": row["backend"],
        "created_at": int(row["created_at"] or 0),
        "updated_at": int(row["updated_at"] or 0),
    }


def event(agent: dict[str, Any], row: dict[str, Any] | None) -> dict[str, Any]:
    """The SSE event clients use to refresh one agent's goal without a full snapshot."""
    return {
        "type": SSEType.GOAL_UPDATED,
        "agent_id": agent["agent_id"],
        "session": agent["session"],
        "goal": public(row),
    }
