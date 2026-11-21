"""A follow-up written into a running turn and not yet settled (issue 491, part C).

From the write to the running CLI's stdin until its echo (the CLI read it) or the process's end
(it never did), the follow-up is a queue item whose receipt says it may have left. Those two
settle it in this runtime. A runtime that dies in between leaves this durable record, written
before the write, so the next one can look the follow-up up in the session file by the uuid its
stdin line carried and settle it by what is there (turn_recovery)."""
from __future__ import annotations

import json

from . import settings_store
from .clock import now_ms
from .log import log_exception

_PREFIX = "steer.written."


def record(*, agent_id: str, trace_id: str, queue_id: str, backend: str, session_id: str,
           cwd: str) -> None:
    settings_store.set_text(f"{_PREFIX}{trace_id}", json.dumps({
        "agent_id": agent_id, "trace_id": trace_id, "queue_id": queue_id,
        "backend": backend, "session_id": session_id, "cwd": cwd, "at": now_ms()}))


def clear(trace_id: str) -> None:
    try:
        settings_store.delete(f"{_PREFIX}{trace_id}")
    except Exception as exc:  # noqa: BLE001 - the next runtime finds it settled and drops it
        log_exception("steerRecordClearFail", exc, detail=trace_id)


def left() -> list[dict]:
    """Every record a runtime left; one that cannot be read is dropped."""
    found = []
    for key in settings_store.keys_with_prefix(_PREFIX):
        try:
            entry = json.loads(settings_store.get(key) or "")
            if not isinstance(entry, dict) or not entry.get("trace_id"):
                raise ValueError(key)
        except ValueError:
            settings_store.delete(key)
            continue
        found.append(entry)
    return found
