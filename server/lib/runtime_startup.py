"""Recovery owned by the agent runtime, never by the HTTP control plane."""
from __future__ import annotations

import pathlib
from typing import Callable

from . import agents as agents_db
from . import db
from .log import log, log_exception
from .resume import resume_missing_sessions
from .timing import SQLITE_RECOVERY_BUSY_TIMEOUT_MS


def restore_persisted_agents(ctx) -> None:
    """Restore backend bindings when the runtime process itself starts."""
    from .agent_store import load_agents
    from .db import conn

    agents = load_agents(ctx.agents_path)
    demand_sessions = {row[0] for row in conn().execute("""SELECT a.session
        FROM agents a JOIN janitor_configs j ON j.agent_id=a.agent_id
        WHERE a.is_janitor=1 AND json_extract(j.execution_json,'$.executor') IN ('ephemeral','deterministic')""")}
    agents = {session: row for session, row in agents.items() if session not in demand_sessions}
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
) -> dict:
    """Recover only after the process-owning runtime has restarted.

    A web-server restart never calls this function.  Ordering matters: record
    the dead runtime's busy turns before reconciliation turns stale busy state
    into idle state. Startup does not submit new assistant prompts: the agent
    performing a planned restart owns explicit targeted continuation.
    """
    if mark_interrupted is None:
        from .interrupted_turns import recover_after_restart
        mark_interrupted = recover_after_restart
    if reconcile is None:
        from .reconcile import reconcile_all
        reconcile = reconcile_all
    with db.busy_timeout(SQLITE_RECOVERY_BUSY_TIMEOUT_MS):
        restore_agents(ctx)
        interrupted = ([] if clean_handoff else
                       mark_interrupted(stream=getattr(ctx, "stream", None)))
        reconciled = int(reconcile() or 0)
    queued = int(dispatch.recover_queued() or 0)
    return {
        "interrupted": len(interrupted),
        "reconciled": reconciled,
        "queued": queued,
    }
