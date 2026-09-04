"""Recovery owned by the agent runtime, never by the HTTP control plane."""
from __future__ import annotations

import pathlib
import uuid
from typing import Callable

from . import agents as agents_db
from .log import log, log_exception
from .resume import resume_missing_sessions


def restore_persisted_agents(ctx) -> None:
    """Restore backend bindings when the runtime process itself starts."""
    from .agent_store import load_agents

    agents = load_agents(ctx.agents_path)
    if not agents:
        return
    results = resume_missing_sessions(
        agents, pathlib.Path.home(),
        backend_sessions_by_session=agents_db.backend_sessions_by_session())
    for result in results:
        if result.get("ok"):
            agent = agents_db.get_by_session(result["sid"])
            if (agent and result.get("action") == "fresh"
                    and agents_db.live_backend_session(agent["agent_id"])):
                agents_db.start_runtime(agent["agent_id"], result["sid"])
            if agent and agents_db.current_runtime_id(agent["agent_id"]) is None:
                agents_db.start_runtime(agent["agent_id"], result["sid"])
            if agent and result.get("backend_session_id"):
                try:
                    agents_db.bind_backend_session(
                        agent["agent_id"], result["backend_session_id"])
                except agents_db.SessionAlreadyBound as exc:
                    log(
                        "startupSessionConflict",
                        f"{result['sid']} wants {exc.backend_session_id} "
                        f"owned by {exc.owner_agent_id}; leaving fresh",
                    )
        log("runtimeAgentRestore",
            f"{result['sid']} {result['action']} ok={result['ok']}")


def recover_runtime(
    ctx,
    dispatch,
    *,
    clean_handoff: bool = False,
    restore_agents: Callable = restore_persisted_agents,
    mark_interrupted: Callable | None = None,
    reconcile: Callable | None = None,
    restart_agents: Callable | None = None,
    restart_prompt: Callable | None = None,
) -> dict:
    """Recover only after the process-owning runtime has restarted.

    A web-server restart never calls this function.  Ordering matters: record
    the dead runtime's busy turns before reconciliation turns stale busy state
    into idle state.
    """
    if mark_interrupted is None:
        from .interrupted_turns import recover_after_restart
        mark_interrupted = recover_after_restart
    if reconcile is None:
        from .reconcile import reconcile_all
        reconcile = reconcile_all
    if restart_agents is None or restart_prompt is None:
        from .heartbeat import restart_heartbeat_agents, restart_heartbeat_prompt_text
        restart_agents = restart_agents or restart_heartbeat_agents
        restart_prompt = restart_prompt or restart_heartbeat_prompt_text

    restore_agents(ctx)
    interrupted = ([] if clean_handoff else
                   mark_interrupted(stream=getattr(ctx, "stream", None)))
    reconciled = int(reconcile() or 0)
    sent = 0
    trace_ids: list[str] = []
    for agent in ([] if clean_handoff else restart_agents()):
        session = str(agent.get("session") or "")
        if not session:
            continue
        trace_id = str(uuid.uuid4())
        trace_ids.append(trace_id)
        try:
            dispatch.dispatch(
                text=restart_prompt(agent),
                requested_session=session,
                forced_session=session,
                trace_id=trace_id,
                synthesize_audio=False,
                origin="heartbeat",
            )
            sent += 1
        except Exception as exc:  # one unavailable provider must not block boot
            log_exception("runtimeRestartHeartbeatFail", exc, detail=session)
    queued = int(dispatch.recover_queued() or 0)
    return {
        "interrupted": len(interrupted),
        "reconciled": reconciled,
        "restart_heartbeats": sent,
        "restart_trace_ids": trace_ids,
        "queued": queued,
    }
