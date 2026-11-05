"""Moved out of server/lib/turn_dispatch.py by tools/refactor/split_apply.py (split 'turn-dispatch-module', recipe tools/refactor/splits/s4-turn-dispatch.toml).
turn_dispatch.py imports these names back. Generated: change the recipe, not this file."""

from __future__ import annotations

import pathlib
from dataclasses import replace
from typing import Any
from . import agents as agents_db
from . import turn_queue
from . import turn_lifecycle
from . import events
from .turn_lifecycle import TurnEvent
from .log import log_exception
from .protocol import AgentState
from .turn_dispatch_state import _FAILOVERS, _QUEUED, _SLOTS, _TURN_LOCK


def _existing_cwd(raw: Any) -> pathlib.Path:
    from .launch_paths import existing_workspace_path
    return existing_workspace_path(raw)


def _result_error_text(event: Any) -> str:
    """Best-effort human-readable error text out of an error-result event."""
    if not isinstance(event, dict):
        return ""
    for k in ("result", "error", "message", "subtype"):
        v = event.get(k)
        if v:
            return str(v)
    return "error result"


def _result_detail(event: dict, *, trace_id: str) -> dict:
    usage = (event.get("usage") or {}) if isinstance(event, dict) else {}
    detail: dict = {"dispatch": "clarp", "trace_id": trace_id}
    if usage:
        detail["tokens_in"] = int(
            (usage.get("input_tokens") or 0)
            + (usage.get("cache_creation_input_tokens") or 0)
            + (usage.get("cache_read_input_tokens") or 0)
        )
        detail["tokens_out"] = int(usage.get("output_tokens") or 0)
    cost = event.get("total_cost_usd") if isinstance(event, dict) else None
    if cost is not None:
        detail["cost_usd"] = float(cost)
    duration = event.get("duration_ms") if isinstance(event, dict) else None
    if duration is not None:
        detail["duration_ms"] = int(duration)
    return detail


def _result_assistant_text(event: dict) -> str:
    return str(
        event.get("_assistant_text")
        or event.get("last_agent_message")
        or event.get("result")
        or event.get("message")
        or ""
    ).strip()


def _record_dreaming_result(agent_id: str, event: dict) -> None:
    text = _result_assistant_text(event)
    if not text.strip():
        return
    try:
        from . import dreaming
        dreaming.process_assistant_text(agent_id, text, live=False, origin="dreaming")
    except Exception as e:  # noqa: BLE001
        log_exception("dreamingResultParseFail", e, detail=agent_id)


_FENCE_RETRY_DELAYS = (1.0, 5.0, 15.0)


def _record_janitor_cancelled(ctx, agent_id: str, session: str, trace_id: str) -> None:
    """Publish a truthful terminal state only for this agent's current trace."""
    if agents_db.get_trace(agent_id) != trace_id:
        return
    turn_lifecycle.try_transition(agent_id, TurnEvent.STOP_REQUESTED, {
        "source": "janitor_pause", "origin": "janitor", "trace_id": trace_id,
        "message": "Maintenance run cancelled"})
    turn_lifecycle.close_turns_for_trace(agent_id, trace_id)
    if getattr(ctx, "stream", None) is not None:
        events.broadcast(ctx.stream, events.agent_state(
            session=session, agent_id=agent_id, kind=AgentState.INTERRUPTED,
            origin="janitor"))


def _recovery_parked(agent_id: str, trace_id: str | None) -> bool:
    return any(coordinator.parked(agent_id, trace_id)
               for coordinator in _FAILOVERS.values())


def snapshot_stop_state(agent_id: str) -> dict:
    with _TURN_LOCK:
        snapshot = _SLOTS.stop_snapshot(agent_id).as_dict()
        snapshot["account_recovery_parked"] = _recovery_parked(
            agent_id, _SLOTS.get(agent_id) or None)
        return snapshot


def begin_stop(agent_id: str, *, user_stop: bool = False) -> tuple[dict, int, bool]:
    """Atomically install the Stop barrier in the process that owns turns.
    ``user_stop`` marks the user's Stop: its pause also moves the durable user
    stop generation in the same transaction. A maintenance cancel uses the same
    barrier without it and never withdraws the user's own sends (see also
    fence_user_stop)."""
    stop = None
    try:
        with _TURN_LOCK:
            value = _SLOTS.get(agent_id) or None
            recovery_parked = _recovery_parked(agent_id, value)
            queue_was_paused = bool(turn_queue.state(agent_id)["paused"])
            # As in clear_for_agent: the barrier keeps Stop atomic with
            # _finish_turn(); the pause flag lands right after the release.
            stop, dropped = _SLOTS.begin_stop(agent_id)
            if user_stop:
                _TURN_LOCK.defer(lambda: turn_queue.set_paused(agent_id, True, user_stop=True))
            else:
                _TURN_LOCK.defer(lambda: turn_queue.set_paused(agent_id, True))
    except Exception:
        # The pause could not be written ('database is locked'): the Stop
        # fails, and its barrier must not outlive it, or every later send
        # waits behind it for a finish_stop that never comes (QUEUE-N1).
        if stop is not None:
            _SLOTS.restore_stop(agent_id, stop.as_dict())
        raise
    snapshot = stop.as_dict()
    snapshot["account_recovery_parked"] = recovery_parked
    return snapshot, dropped, queue_was_paused


def restore_stop_state(agent_id: str, snapshot: dict) -> None:
    """Roll back every process-local Stop mutation after interrupt failure."""
    _SLOTS.restore_stop(agent_id, snapshot)


def prepare_queued_for_finish(
    agent_id: str, snapshot: dict, cancelled_trace_ids: set[str]
) -> None:
    """Restore preserved/new specs except cancelled ones under the barrier."""
    # Work queued before this Stop loses the right to run past its pause
    # that it had on arrival; work admitted during the Stop keeps it.
    snapshot = {**snapshot, "queued": [
        replace(spec, bypass_pause=False)
        for spec in snapshot.get("queued") or ()]}
    with _TURN_LOCK:
        cancelled = [
            spec for spec in list(snapshot.get("queued") or [])
            + list(_QUEUED.get(agent_id) or [])
            if spec.trace_id in cancelled_trace_ids]
        _SLOTS.restore_queue(agent_id, snapshot, cancelled_trace_ids)
    for spec in cancelled:
        if getattr(spec, "park_id", ""):
            turn_queue.unpark(spec.park_id)
        if getattr(spec, "taken_over", False) and spec.queue_id:
            # Exactly the row the hand-over parked for this work (by id);
            # other parked rows (held arrivals) stay held across the Stop.
            turn_queue.remove_taken_over(spec.queue_id)
