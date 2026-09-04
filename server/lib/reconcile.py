"""Reconcile derived agent state against reality.

An agent's "truth" is spread across sources that drift: the state_log's latest
kind, the runtime's bound backend session, the dispatcher's in-flight slot,
and the OS process. Nothing used to reconcile them, so every desync became a
distinct wedge (stuck "thinking" badge, ghost session resumed forever, phantom
in-flight slot). This module trusts only reality — is a process alive? is a
terminal attached? does the transcript exist? — and repairs the rest.

Invariants:
  INV1  busy ⇔ live work. A busy state_log kind with no live process, no
        attached terminal and no spawning slot is repaired to IDLE.
        ("background" is agent-declared out-of-band work with no server-visible
        process, so it is deliberately NOT subject to INV1.)
  INV2  bound Claude session ⇔ transcript exists. A bound backend_session_id
        whose transcript is missing on disk is unbound (next turn starts fresh)
        — resuming it exits instantly and wedges the agent forever.
  INV3  in-flight slot ⇔ live turn. A held slot with no live turn (and nothing
        queued) is freed.

Called per agent from the snapshot read model and once for all agents at boot.
All repairs are idempotent and logged.
"""
from __future__ import annotations

import pathlib
from typing import Any

from . import agents as agents_db
from . import backends
from .log import log, log_exception
from .protocol import AgentState

# Kinds that assert a live process. Intentionally excludes "background".
_PROCESS_BUSY_KINDS = frozenset({"thinking", "tool", "compacting"})


def _projects_root(home: pathlib.Path | None) -> pathlib.Path:
    return (home or pathlib.Path.home()) / ".claude" / "projects"


def has_live_work(agent_id: str, backend: str) -> bool:
    """Reality check: process handles, an attached terminal, or a slot that is
    mid-spawn (process not yet registered)."""
    try:
        if backends.active_handles(backend, agent_id):
            return True
    except Exception as e:  # noqa: BLE001
        log_exception("reconcileHandlesFail", e, detail=agent_id)
        return True  # can't tell → don't repair
    try:
        from . import terminal_ws
        if terminal_ws.has_live_terminal(agent_id):
            return True
    except Exception:  # noqa: BLE001
        pass
    try:
        from . import turn_dispatch
        if turn_dispatch._slot_is_spawning(agent_id):
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


def reconcile_agent(agent_id: str, backend: str | None = None, *,
                    home: pathlib.Path | None = None,
                    observed_state: dict[str, Any] | None = None,
                    bound_session: str | None = None) -> dict[str, Any]:
    """Repair one agent's derived state. Returns what was repaired."""
    repaired: dict[str, Any] = {}
    agent = agents_db.get_by_agent_id(agent_id) if backend is None else None
    backend = backends.normalize(backend or (agent or {}).get("backend"))
    live = has_live_work(agent_id, backend)

    # INV1 — busy ⇔ live work
    state = observed_state if observed_state is not None else (agents_db.latest_state(agent_id) or {})
    kind = str(state.get("kind") or "")
    if kind in _PROCESS_BUSY_KINDS and not live:
        # Batch projections can predate a transition to background/done or a
        # newly spawned turn. Revalidate only the rare would-repair path.
        if observed_state is not None:
            state = agents_db.latest_state(agent_id) or {}
            kind = str(state.get('kind') or '')
            live = has_live_work(agent_id, backend)
        if kind in _PROCESS_BUSY_KINDS and not live:
            agents_db.record_state(agent_id, AgentState.IDLE,
                                   {"reason": "reconcile", "was": kind})
            log("reconcileStuckBusy", f"agent={agent_id} was={kind} → idle")
            repaired["state"] = kind

    # INV2 — bound Claude session ⇔ transcript exists
    if backend == backends.CLAUDE and not live:
        bsid = bound_session if bound_session is not None else agents_db.live_backend_session(agent_id)
        if bsid:
            from .transcript_log import find_latest_jsonl
            if (find_latest_jsonl(bsid, projects_root=_projects_root(home)) is None
                    and agents_db.live_backend_session(agent_id) == bsid
                    and not has_live_work(agent_id, backend)):
                agents_db.end_current_runtime(agent_id)
                log("reconcileGhostSession",
                    f"agent={agent_id} bsid={bsid} has no transcript; unbound")
                repaired["ghost_session"] = bsid

    # INV3 — in-flight slot ⇔ live turn
    if not live:
        try:
            from . import turn_dispatch
            freed = turn_dispatch.free_stale_slot(agent_id)
            if freed:
                log("reconcileStaleSlot", f"agent={agent_id} dead_trace={freed}")
                repaired["slot"] = freed
        except Exception as e:  # noqa: BLE001
            log_exception("reconcileSlotFail", e, detail=agent_id)
    return repaired


def reconcile_all(*, home: pathlib.Path | None = None) -> int:
    """Boot-time pass over every agent. Returns the number repaired."""
    count = 0
    for a in agents_db.list_agents():
        try:
            if reconcile_agent(a["agent_id"], a.get("backend"), home=home):
                count += 1
        except Exception as e:  # noqa: BLE001
            log_exception("reconcileAgentFail", e, detail=a.get("agent_id"))
    if count:
        log("reconcileAll", f"repaired {count} agent(s)")
    return count
