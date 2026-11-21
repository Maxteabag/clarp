"""The dispatcher's side of a takeover (issue 491, part B; the steps are lib.takeover's).

A user's message that would wait for the agent's running Claude turn, when that turn has
printed nothing yet, ends it and starts ONE turn that is given both messages, so one answer
covers both. A turn that has printed anything (a word, a tool call) is never cut: the message
then waits (part A). Bounds: only the user's own word does this (origins.USER_WORD_ORIGINS),
never an agent's message, never on a turn another agent's message started, never on a turn
that is itself a takeover (a third message waits: a takeover is never taken over, so nothing
can starve), never behind the release drain's fence, never below the Claude version that has
the resume pair.
"""
from __future__ import annotations

from dataclasses import replace

from . import agents as agents_db
from . import coordination, origins, takeover, turn_lifecycle, turn_queue
from .log import log, log_exception
from .turn_slots import SENTINELS
from .turn_dispatch_state import (
    _INFLIGHT, _PROCESS_OWNER_PREFIX, _SLOTS, _TURN_LOCK, _alive,
)



def _first_message(agent_id: str, trace_id: str):
    """The running turn's own user row (by its trace, idx_messages_trace)."""
    from .db import conn
    return conn().execute(
        """SELECT text, origin, sender_agent_id FROM messages INDEXED BY idx_messages_trace
            WHERE trace_id = ? AND agent_id = ? AND role = 'user'
            ORDER BY COALESCE(timestamp, ''), seq LIMIT 1""",
        (trace_id, agent_id)).fetchone()


def take_over_if_silent(service, spec):
    """Freeze, judge, end. Returns the spec to launch as the takeover (the running turn is
    ended, the slot is this spec's), or None: nothing changed and the message waits."""
    if spec.origin not in origins.USER_WORD_ORIGINS:
        return None
    from .backend.registry import by_id
    try:
        backend = by_id(spec.backend)
    except Exception as exc:  # noqa: BLE001 - an unknown backend never takes over
        log_exception("takeoverBackendUnknown", exc, detail=spec.backend)
        return None
    if not getattr(backend, "takeover_strategy", ""):
        return None
    agent_id = spec.agent_id
    with _TURN_LOCK:
        running = _INFLIGHT.get(agent_id) or ""
        if (not running or running in SENTINELS or running.startswith(_PROCESS_OWNER_PREFIX)
                or _SLOTS.fenced or _SLOTS.is_spawning(agent_id)):
            return None
    try:
        handles = _alive(list(service.backends.active_handles(spec.backend, agent_id) or ()))
    except Exception as exc:  # noqa: BLE001 - unknown: the message waits
        log_exception("takeoverHandlesFail", exc, detail=agent_id)
        return None
    if len(handles) != 1:
        return None
    handle = handles[0]
    if (not getattr(handle, "takeover_capable", False) or getattr(handle, "takeover_of", False)
            or getattr(handle, "output_seen", False)):
        return None
    first = _first_message(agent_id, running)
    if (first is None or (first["origin"] or "user") not in origins.USER_WORD_ORIGINS
            or coordination.audience_of(agent_id, running) == coordination.AGENTS):
        return None
    session_id = agents_db.live_backend_session(agent_id) or spec.backend_session_id
    if not session_id:
        return None
    try:
        if not backend.takeover_ready():
            return None
    except Exception as exc:  # noqa: BLE001 - unknown: the message waits
        log_exception("takeoverPrepareFail", exc, detail=agent_id)
        return None
    key = takeover.record_freeze(agent_id=agent_id, trace_id=running,
                                 pgid=handle.process_group, pid=handle.pid)
    plan = None
    ended = False
    try:
        if not takeover.freeze(handle):
            return None
        if takeover.drained(handle) and not handle.output_seen:
            # Judged while frozen, before any kill: the backend reads its own session
            # (Claude: the resume point; agy: message 1 must not be there yet).
            try:
                plan = backend.takeover_plan(
                    session_id=session_id, home=service.home, cwd=str(spec.cwd),
                    running_trace=running, first_text=str(first["text"] or ""))
            except Exception as exc:  # noqa: BLE001 - unreadable: no takeover
                log_exception("takeoverJudgeFail", exc, detail=agent_id)
                plan = None
        if plan is not None:
            with _TURN_LOCK:
                if _INFLIGHT.get(agent_id) != running:
                    plan = None             # a Stop or another send took the slot meanwhile
                else:
                    # The slot first: the frozen turn's dying callbacks find it superseded.
                    _SLOTS.claim(agent_id, spec.trace_id)
        if plan is None:
            takeover.thaw(handle)
            return None
        agents_db.set_trace_for_session(spec.session, spec.trace_id)
        takeover.end_frozen(handle)
        ended = True
    except BaseException:
        if not ended and plan is None:
            takeover.thaw(handle)
        raise
    finally:
        takeover.clear_freeze(key)
    try:
        handle.proc.wait(timeout=5)
    except Exception as exc:  # noqa: BLE001 - SIGKILL of a stopped group; logged, not waited on
        log_exception("takeoverWaitFail", exc, detail=agent_id)
    try:
        turn_lifecycle.settle_replaced(agent_id, running)
        turn_queue.settle_receipt(agent_id, running, takeover.REPLACED)
    except Exception as exc:  # noqa: BLE001 - bookkeeping; the takeover goes on
        log_exception("takeoverSettleFail", exc, detail=agent_id)
    log("turnTakenOver", f"agent={agent_id} backend={spec.backend} replaced={running} "
                         f"by={spec.trace_id} resume_at="
                         f"{'yes' if plan.get('resume_at') else 'no'}")
    return replace(spec, takeover=plan, backend_session_id=session_id, is_new_session=False)
