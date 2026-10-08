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
        queued) is freed. With an external runtime the slots are the
        runtime's and a held one reads as live work here, so instead the
        runtime is asked to run its own leak check once the turn has visibly
        settled (see _runtime_slot_may_have_leaked).

Called per agent from the snapshot read model and once for all agents at boot.
All repairs are idempotent and logged.
"""
from __future__ import annotations

import pathlib
import threading
import time
from typing import Any

from . import agents as agents_db
from . import backends, db, turn_lifecycle
from .log import log, log_exception

# Kinds that assert a live process. Intentionally excludes "background".
_PROCESS_BUSY_KINDS = turn_lifecycle.BUSY

# agent_id → monotonic time the runtime was last asked to check its slot.
_LEAK_CHECK_ASKED: dict[str, float] = {}
_LEAK_CHECK_LOCK = threading.Lock()


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
        if backends.external_live_work(backend, agent_id):
            return True
    except Exception as e:  # noqa: BLE001
        log_exception("reconcileExternalWorkFail", e, detail=agent_id)
        return True  # can't tell → don't repair
    try:
        from . import turn_dispatch
        if turn_dispatch.live_work(agent_id).terminal:
            return True
    except Exception:  # noqa: BLE001
        pass
    try:
        if _slot_is_spawning(agent_id):
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


def _slot_is_spawning(agent_id: str) -> bool:
    """Is a turn for this agent claimed but not yet registered as a process?

    With an external runtime the answer is in its ``status`` result, which
    ``backends`` already caches for a short window; asking the runtime
    directly cost one socket round trip per agent per snapshot. This process
    only consults the dispatcher's own slot table when it owns its turns.
    """
    if getattr(backends, "_RUNTIME_CLIENT", None) is None:
        from . import turn_dispatch
        return turn_dispatch._slot_is_spawning(agent_id)
    return agent_id in (backends.runtime_status().get("spawning") or ())


def _runtime_slot_may_have_leaked(agent_id: str) -> bool:
    """Whether to ask the external runtime to check this agent's slot.

    Only when the runtime holds a slot (not mid-spawn, not a terminal) for an
    agent whose newest state has been terminal for the runtime's grace
    period, and at most once per grace period per agent: snapshots reconcile
    every agent on every read, and the runtime may rightly keep the slot.
    """
    from . import turn_dispatch
    try:
        status = backends.runtime_status()
    except Exception:  # noqa: BLE001 - runtime_status logs once per window
        return False
    if (agent_id not in (status.get("active") or {})
            or agent_id in set(status.get("spawning") or ())
            or agent_id in set(status.get("terminals") or ())):
        return False
    state = agents_db.latest_state(agent_id) or {}
    grace_ms = turn_dispatch.LEAKED_SLOT_GRACE_MS
    if (state.get("kind") not in turn_lifecycle.TERMINAL
            or db.now_ms() - int(state.get("ts") or 0) < grace_ms):
        return False
    now = time.monotonic()
    with _LEAK_CHECK_LOCK:
        asked = _LEAK_CHECK_ASKED.get(agent_id)
        if asked is not None and now - asked < grace_ms / 1000:
            return False
        _LEAK_CHECK_ASKED[agent_id] = now
    return True


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
            # A repair is the one write the table would refuse on its own.
            turn_lifecycle.transition(
                agent_id, turn_lifecycle.TurnEvent.RECONCILE_REPAIR,
                {"reason": "reconcile", "was": kind}, force=True)
            log("reconcileStuckBusy", f"agent={agent_id} was={kind} → idle")
            repaired["state"] = kind

    # INV2 — bound session ⇔ something to resume. A CLI that resumes by
    # transcript file answers None for a session whose file is gone (a ghost
    # binding); one that resumes by id always has a target.
    if not live:
        bsid = bound_session if bound_session is not None else agents_db.live_backend_session(agent_id)
        if bsid:
            if (backends.by_id(backend).resume_target(bsid, "", home) is None
                    and agents_db.live_backend_session(agent_id) == bsid
                    and not has_live_work(agent_id, backend)):
                agents_db.end_current_runtime(agent_id)
                log("reconcileGhostSession",
                    f"agent={agent_id} bsid={bsid} has no transcript; unbound")
                repaired["ghost_session"] = bsid

    # INV3 — in-flight slot ⇔ live turn
    runtime_owned = getattr(backends, "_RUNTIME_CLIENT", None) is not None
    if not live or runtime_owned:
        try:
            from . import turn_dispatch
            if runtime_owned and not _runtime_slot_may_have_leaked(agent_id):
                return repaired
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
            repaired = db.retry_locked(
                lambda aid=a["agent_id"], backend=a.get("backend"):
                    reconcile_agent(aid, backend, home=home)
            )
            if repaired:
                count += 1
        except Exception as e:  # noqa: BLE001
            log_exception("reconcileAgentFail", e, detail=a.get("agent_id"))
    if count:
        log("reconcileAll", f"repaired {count} agent(s)")
    return count
