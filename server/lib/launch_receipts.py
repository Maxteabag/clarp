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
the admission, no schema change. Old rows are pruned on write.
"""
from __future__ import annotations

import json

from . import db, settings_store
from .db import now_ms
from .timing import SQLITE_CRITICAL_BUSY_TIMEOUT_MS

PREFIX = "launch-receipt:"
PENDING = "pending"
ATTEMPTED = "attempted"
FENCED = "fenced"
RELAUNCHED = "relaunched"
RETENTION_MS = 7 * 24 * 3600 * 1000


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
                   supersedes: str = "") -> None:
    """Write the receipt inside the caller's admission transaction."""
    stamp = now_ms()
    if supersedes and supersedes != trace_id:
        settings_store.set_json_field_if(_key(supersedes), "state", RELAUNCHED, expect=PENDING)
    settings_store.set_text(_key(trace_id), json.dumps({
        "state": PENDING, "agent_id": agent_id, "trace_id": trace_id,
        "client_msg_id": client_msg_id, "origin": origin,
        "sender_agent_id": sender_agent_id or "", "admitted_at": stamp,
    }, separators=(",", ":")), updated_at=stamp)
    settings_store.prune_prefix(PREFIX, updated_before=stamp - RETENTION_MS)


def mark_attempted(trace_id: str) -> None:
    """Called immediately before a backend receives the prompt. Raises when
    the write fails, so the caller can refuse to hand it over: a launch that
    is not recorded here could later be mistaken for one that never ran."""
    if not trace_id:
        return
    with db.busy_timeout(SQLITE_CRITICAL_BUSY_TIMEOUT_MS):
        settings_store.set_json_field_if(_key(trace_id), "state", ATTEMPTED, expect=PENDING)


def fence_agent(agent_id: str) -> int:
    """Stop: a message admitted but not yet launched is no longer
    retryable by an automatic resend under the same id."""
    if not agent_id:
        return 0
    return settings_store.set_json_field_for_prefix(
        PREFIX, "state", FENCED, expect=PENDING,
        match_field="agent_id", match_value=agent_id)


def status(agent_id: str, trace_id: str, *, client_msg_id: str | None = None,
           origin: str | None = None, sender_agent_id: str | None = None) -> str:
    """"retryable" only for a pending receipt of this agent (and, when given,
    this client id and principal); "fenced" after Stop; otherwise "unknown"."""
    receipt = get(trace_id)
    if not receipt or receipt.get("agent_id") != agent_id:
        return "unknown"
    state = receipt.get("state")
    if state == FENCED:
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
