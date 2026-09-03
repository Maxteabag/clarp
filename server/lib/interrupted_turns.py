"""Turns the server itself killed, and what the user gets to see of them.

A restart (deploy, update, crash-and-respawn) takes every backend child down
with it. The turn that was running never reports back, so no assistant row is
written and no terminal state is recorded. Until now the only party told was
the agent, through the restart heartbeat; the user's message just sat there
with nothing after it, indistinguishable from an agent that chose to say
nothing (issue #11).

Boot recovery runs before the restart heartbeat. For every live, non-archived
agent whose latest persisted state is still busy it records an INTERRUPTED
state (so the existing banner shows) and, when the turn was something the
user or a user-facing channel asked for, writes a visible marker row under
the orphaned message.
"""
from __future__ import annotations

from typing import Any

from . import agents as agents_db
from . import message_store, origins
from .db import conn
from .log import log, log_exception
from .protocol import AgentState, SSEType

MARKER_ORIGIN = origins.MARKER_ORIGIN
RESTART_SOURCE = "server_restart"
RESTART_MARKER_TEXT = "Turn interrupted by server restart"
# The causing user row is stored before dispatch records THINKING; the margin
# only guards against clock skew between the two writes.
_CAUSE_MARGIN_MS = 5_000
# Nobody was waiting on these turns, so nothing is shown in the transcript.
# The state change still lands so the agent stops looking busy.
_SILENT_ORIGINS = frozenset(origins.ROUTINE_AUTOMATION_ORIGINS | {"watcher"})


def orphaned_turn(agent: dict) -> dict[str, Any] | None:
    """Persisted trace of a turn that was in flight when the server died.

    Dispatch records THINKING when it spawns a turn and a terminal state
    (DONE / IDLE / INTERRUPTED) when the backend reports back. A SIGTERM'd
    child usually dies without firing that callback, so a busy state sitting
    at the top of the state log is the fingerprint of a turn nobody finished.
    Backends differ in how they stream and finish, but all of them go through
    dispatch, which is why this rule holds for every one of them.
    """
    agent_id = agent["agent_id"]
    latest = agents_db.latest_state(agent_id) or {}
    if latest.get("kind") not in AgentState.busy_states():
        return None
    detail = latest.get("detail") or {}
    if not isinstance(detail, dict):
        detail = {}
    backend_session_id = str(
        detail.get("backend_session_id")
        or agents_db.live_backend_session(agent_id)
        or "")
    cause = _causing_user_row(
        agent_id, backend_session_id, int(latest.get("ts") or 0))
    origin = str(
        (cause["origin"] if cause is not None else "")
        or detail.get("origin")
        or "user").strip() or "user"
    return {
        "agent_id": agent_id,
        "session": str(agent.get("session") or ""),
        "backend_session_id": backend_session_id,
        "trace_id": str(detail.get("trace_id") or ""),
        "origin": origin,
        "cause_message_id": cause["message_id"] if cause is not None else "",
        "state_kind": str(latest.get("kind") or ""),
    }


def _causing_user_row(agent_id: str, backend_session_id: str, state_ts: int):
    params: list[Any] = [agent_id]
    where = "agent_id = ? AND role = 'user'"
    if backend_session_id:
        where += " AND backend_session_id = ?"
        params.append(backend_session_id)
    if state_ts:
        where += " AND updated_at <= ?"
        params.append(state_ts + _CAUSE_MARGIN_MS)
    return conn().execute(
        f"""SELECT message_id, origin
              FROM messages
             WHERE {where}
             ORDER BY updated_at DESC, revision DESC, seq DESC
             LIMIT 1""",
        tuple(params),
    ).fetchone()


def recover_after_restart(stream=None) -> list[dict[str, Any]]:
    """Mark every turn the previous server process took down with it.

    Idempotent: marking records a non-busy state, so a second boot finds
    nothing to do, and the marker row is keyed by the causing message.
    """
    recovered: list[dict[str, Any]] = []
    for agent in agents_db.list_agents():
        if agent.get("archived_at"):
            continue
        if agents_db.current_runtime_id(agent["agent_id"]) is None:
            continue
        try:
            turn = orphaned_turn(agent)
            if turn is None:
                continue
            _mark(turn, stream)
            recovered.append(turn)
        except Exception as e:  # noqa: BLE001 — one bad row must not block boot
            log_exception("restartInterruptFail", e, detail=agent.get("session"))
    return recovered


def _mark(turn: dict[str, Any], stream) -> None:
    agent_id = turn["agent_id"]
    marker = None
    if turn["origin"] not in _SILENT_ORIGINS and turn["cause_message_id"]:
        marker = message_store.record_interruption_marker(
            agent_id=agent_id,
            backend_session_id=turn["backend_session_id"],
            cause_message_id=turn["cause_message_id"],
            text=RESTART_MARKER_TEXT,
        )
    agents_db.record_state(agent_id, AgentState.INTERRUPTED, {
        "source": RESTART_SOURCE,
        "reason": RESTART_SOURCE,
        "message": RESTART_MARKER_TEXT,
        "origin": turn["origin"],
        "trace_id": turn["trace_id"],
        "backend_session_id": turn["backend_session_id"],
        "cause_message_id": turn["cause_message_id"],
    })
    log("turnInterruptedByRestart",
        f"agent={agent_id} session={turn['session']} "
        f"trace={turn['trace_id'] or '∅'} origin={turn['origin']} "
        f"was={turn['state_kind']} marker={int(marker is not None)}")
    if stream is None:
        return
    try:
        stream.broadcast({
            "type": SSEType.AGENT_STATE,
            "session": turn["session"],
            "agent_id": agent_id,
            "kind": AgentState.INTERRUPTED,
            "trace_id": turn["trace_id"],
        })
        if marker is not None:
            stream.broadcast({
                "type": SSEType.TRANSCRIPT_UPDATED,
                "agent_id": agent_id,
                "session": turn["session"],
                "backend_session_id": turn["backend_session_id"],
            })
    except Exception as e:  # noqa: BLE001
        log_exception("restartInterruptBroadcastFail", e, detail=agent_id)
