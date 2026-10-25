"""Pending-launch receipts: proof that an admitted user message never reached a backend.

A user send is admitted (its durable row written) before the dispatcher opens a
turn and hands the prompt to a backend. Anything that fails in between used to
leave the row with no state at all, and `agents.trace_launch_status` rightly
calls missing state "unknown": a retry with the same client message id was
refused for good (2026-10-25, Lumi: the post-COMMIT SSE insert hit "database is
locked" and the message was saved but never sent).

The receipt closes that gap without guessing. It is written in the admission
transaction, so it exists exactly when the row does, and it moves to
``attempted`` (fail-closed) immediately before the prompt is handed to a
backend by spawn or steer. Only a receipt still ``pending`` proves nothing was
sent; ``attempted`` says nothing, and the state log keeps deciding as before.
Stop fences pending receipts so an automatic retry is not fresh intent.

Rows live in ``settings`` under one prefix: same database and transaction as
the admission, no schema change. Pruning is throttled, uses the key index, and
never deletes a fenced receipt; deleting any other one only makes it unknown.

The handoff never trusts a compare-and-set that changed nothing: it reads the
receipt back and refuses a fenced or superseded one. Stop also records an
in-process epoch first, so a fence write that failed still blocks a same-id
resend here, and it retries the write.
"""
from __future__ import annotations

import json
import threading
import time

from . import db, settings_store
from .db import now_ms
from .timing import SQLITE_CRITICAL_BUSY_TIMEOUT_MS

PREFIX = "launch-receipt:"
PENDING = "pending"
ATTEMPTED = "attempted"
FENCED = "fenced"
RELAUNCHED = "relaunched"
RETENTION_MS = 7 * 24 * 3600 * 1000
PRUNE_INTERVAL_SEC = 600.0

_LOCK = threading.Lock()
_STOP_EPOCH: dict[str, int] = {}     # agent_id -> ms of its latest Stop in this process
_LAST_PRUNE = 0.0


class HandoffRefused(RuntimeError):
    """The receipt says this prompt must not be handed to a backend."""

    def __init__(self, state: str):
        self.state = state
        super().__init__(f"launch receipt is {state}")


def eligible(*, origin: str, client_msg_id: str, call_opening: bool) -> bool:
    """User sends the client retries under the same id. Queue receipts,
    openings and machine origins keep their own recovery."""
    return origin == "user" and bool(client_msg_id) and not call_opening


def _key(trace_id: str) -> str:
    return PREFIX + trace_id


def get(trace_id: str) -> dict | None:
    if not trace_id:
        return None
    raw = settings_store.get(_key(trace_id))
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def record_pending(*, agent_id: str, trace_id: str, client_msg_id: str,
                   origin: str, sender_agent_id: str = "",
                   supersedes: str = "", user_stop_generation: int = 0) -> None:
    """Write the receipt inside the caller's admission transaction.

    ``user_stop_generation`` is the agent's durable USER Stop counter at
    admission (turn_queue, moved only in the pause transaction of the user's
    Stop, never by a maintenance cancel or its restore): a later user Stop is
    visible to a retry even across a restart, without relying on this
    module's fence write or its in-process epoch."""
    stamp = now_ms()
    if supersedes and supersedes != trace_id:
        settings_store.set_json_field_if(_key(supersedes), "state", RELAUNCHED, expect=PENDING)
    settings_store.set_text(_key(trace_id), json.dumps({
        "state": PENDING, "agent_id": agent_id, "trace_id": trace_id,
        "client_msg_id": client_msg_id, "origin": origin,
        "sender_agent_id": sender_agent_id or "", "admitted_at": stamp,
        "user_stop_gen": int(user_stop_generation),
    }, separators=(",", ":")), updated_at=stamp)
    _maybe_prune(stamp)


def _maybe_prune(stamp: int) -> None:
    global _LAST_PRUNE
    now = time.monotonic()
    with _LOCK:
        if _LAST_PRUNE and now - _LAST_PRUNE < PRUNE_INTERVAL_SEC:
            return
        _LAST_PRUNE = now
    # Fenced receipts are kept: without them an older state row could read
    # "retryable" again after Stop. Any other deletion only yields "unknown".
    settings_store.prune_prefix_keeping(
        PREFIX, updated_before=stamp - RETENTION_MS, field="state", keep=(FENCED,))


def _stop_generation_moved(receipt: dict) -> bool:
    """Durable: the USER stopped this agent after admission. A receipt without
    the counter (none exist in production) fails closed."""
    from . import turn_queue
    if "user_stop_gen" not in receipt:
        return True
    agent_id = str(receipt.get("agent_id") or "")
    return turn_queue.user_stop_generation(agent_id) > int(receipt["user_stop_gen"])


def _stopped_after_admission(receipt: dict) -> bool:
    with _LOCK:
        stopped_at = _STOP_EPOCH.get(str(receipt.get("agent_id") or ""), 0)
    return bool(stopped_at) and stopped_at >= int(receipt.get("admitted_at") or 0)


def mark_attempted(trace_id: str, *, override_fence: bool = False) -> str:
    """Called immediately before a backend receives the prompt.

    Returns "attempted" (moved from pending), "already_attempted" or
    "no_receipt". Raises HandoffRefused for a fenced or superseded receipt,
    and any database error as is, so the caller refuses to hand it over: a
    launch not recorded here could later be mistaken for one that never ran.
    A compare-and-set that changed no row never authorizes a handoff by
    itself; the receipt is read back to say why. ``override_fence`` is for a
    launch Stop or a durable queue row already decided runs: a fence that
    raced it is replaced; a superseded receipt still refuses.
    """
    if not trace_id:
        return "no_receipt"
    with db.busy_timeout(SQLITE_CRITICAL_BUSY_TIMEOUT_MS):
        receipt = get(trace_id)
        if (not override_fence and receipt and receipt.get("state") == PENDING
                and _stopped_after_admission(receipt)):
            settings_store.set_json_field_if(_key(trace_id), "state", FENCED, expect=PENDING)
            raise HandoffRefused(FENCED)
        if settings_store.set_json_field_if(_key(trace_id), "state", ATTEMPTED, expect=PENDING):
            return "attempted"
        if override_fence and settings_store.set_json_field_if(
                _key(trace_id), "state", ATTEMPTED, expect=FENCED):
            return "attempted"
        receipt = get(trace_id)
    if receipt is None:
        return "no_receipt"
    state = receipt.get("state")
    if state == ATTEMPTED:
        return "already_attempted"
    raise HandoffRefused(str(state))


def note_stop(agent_id: str) -> None:
    """Stop, in this process, before its fence write: a resend or handoff
    here never treats an earlier admission as unsent, even if that write fails."""
    if agent_id:
        with _LOCK:
            _STOP_EPOCH[agent_id] = now_ms()


def fence_agent(agent_id: str) -> int:
    """Stop: a message admitted but not yet launched is no longer
    retryable by an automatic resend under the same id."""
    if not agent_id:
        return 0
    return settings_store.set_json_field_for_prefix(
        PREFIX, "state", FENCED, expect=PENDING,
        match_field="agent_id", match_value=agent_id)


def reset_for_tests() -> None:
    global _LAST_PRUNE
    with _LOCK:
        _STOP_EPOCH.clear()
        _LAST_PRUNE = 0.0


def status(agent_id: str, trace_id: str, *, client_msg_id: str | None = None,
           origin: str | None = None, sender_agent_id: str | None = None) -> str:
    """"retryable" only for a pending receipt of this agent (and, when given,
    this client id and principal); "fenced" after Stop; otherwise "unknown"."""
    receipt = get(trace_id)
    if not receipt or receipt.get("agent_id") != agent_id:
        return "unknown"
    state = receipt.get("state")
    if state == FENCED or (state == PENDING and (
            _stopped_after_admission(receipt) or _stop_generation_moved(receipt))):
        return "fenced"
    if state != PENDING:
        return "unknown"
    if client_msg_id is not None and receipt.get("client_msg_id") != client_msg_id:
        return "unknown"
    if origin is not None and receipt.get("origin") != origin:
        return "unknown"
    if (sender_agent_id is not None
            and (receipt.get("sender_agent_id") or "") != (sender_agent_id or "")):
        return "unknown"
    return "retryable"
