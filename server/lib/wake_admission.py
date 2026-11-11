"""Shared idle-safety and quiet-period primitives for autonomous agent wakes.

Autonomous wake sources (`heartbeat`, `dreaming`, `team_leader`,
`janitor_autonomy`, `dispatch_adapters`) share common execution-busy and
recent-real-activity checks, while `turn_queue.holds_autonomous_wake` owns the
durable queue-pause / queued-or-claimed hold check.

Intentional per-domain differences preserved at callers until a unified `wakes`
ledger replaces the separate schedulers:
- `dreaming_store` ignores routine self-activity busy/handle state unless the
  session is compacting (`ignore_busy_handles=True`).
- `task_goal_recovery` ignores leftover Stop queue pauses for explicitly resumed
  active goals while checking `reconcile.has_live_work` and `live_work`.
- `bookkeeping_listener` scopes queue checks to its own `WAKE_PREFIX` receipts
  and bounds open-turn staleness by `BUSY_TURN_MS`.
"""
from __future__ import annotations

from collections.abc import Callable

from . import agents as agents_db
from . import backends, message_store, turn_dispatch
from .log import log_exception
from .protocol import AgentState


def execution_busy_state(agent: dict) -> tuple[bool, bool, bool]:
    """Return `(is_busy, has_active_handles, is_compacting)` for `agent`."""
    agent_id = agent["agent_id"]
    session = agent.get("session") or ""
    busy = bool(agents_db.is_busy(agent_id))
    active = bool(backends.active_handles(agent.get("backend"), agent_id))
    compacting = bool(session) and turn_dispatch.live_work(agent_id, session=session).compacting
    return busy, active, compacting


def execution_busy_reason(
    agent: dict,
    *,
    latest: dict | None = None,
    ignore_busy_handles: bool = False,
) -> str:
    """Return `'compacting'`, `'busy'`, `'active'`, or `''` if execution is idle."""
    if latest is not None and latest.get("kind") == AgentState.COMPACTING:
        return "compacting"
    busy, active, compacting = execution_busy_state(agent)
    if busy and not ignore_busy_handles:
        return "busy"
    if active and not ignore_busy_handles:
        return "active"
    if compacting:
        return "compacting"
    return ""


def recent_real_activity_reason(
    agent_id: str,
    *,
    now: float,
    quiet_period_sec: int,
    failure_event: str,
    activity_ms_fn: Callable[[str], int] | None = None,
) -> str:
    """Return `'recent-activity:<age>s'`, `'activity-unknown'`, or `''`."""
    if quiet_period_sec <= 0:
        return ""
    try:
        if activity_ms_fn is not None:
            last_ms = int(activity_ms_fn(agent_id) or 0)
        else:
            last_ms = int(message_store.last_real_message_activity(agent_id=agent_id) or 0)
    except Exception as exc:  # noqa: BLE001
        log_exception(failure_event, exc, detail=agent_id)
        return "activity-unknown"
    if not last_ms:
        return ""
    age = now - (last_ms / 1000.0)
    if age < quiet_period_sec:
        return f"recent-activity:{max(0, int(age))}s"
    return ""
