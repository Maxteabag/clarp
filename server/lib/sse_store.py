"""SQLite persistence for durable SSE replay."""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from .db import conn, now_ms

_STATEFUL_SINGLETON_TYPES = frozenset({"agent-focus", "server-version"})


def record_sse_event(event: dict[str, Any]) -> int:
    payload = dict(event)
    ts = int(payload.get("ts") or now_ms())
    event_type = str(payload.get("type") or "")
    session = str(payload.get("session") or "")
    agent_id = str(payload.get("agent_id") or "")
    # The id is only known after the INSERT, so the UPDATE that copies it into
    # the payload must share its transaction: a lock error between the two
    # would leave a replayable row without its event_id. A savepoint nests
    # inside a caller's transaction and is a transaction of its own otherwise.
    database = conn()
    database.execute("SAVEPOINT record_sse_event")
    try:
        cur = database.execute(
            """INSERT INTO sse_events (ts, type, session, agent_id, payload)
               VALUES (?, ?, ?, ?, ?)""",
            (ts, event_type, session, agent_id, json.dumps(payload, separators=(",", ":"))),
        )
        event_id = int(cur.lastrowid or 0)
        database.execute(
            "UPDATE sse_events SET payload = ? WHERE event_id = ?",
            (json.dumps({**payload, "event_id": event_id}, separators=(",", ":")),
             event_id),
        )
        database.execute("RELEASE record_sse_event")
    except BaseException:
        database.execute("ROLLBACK TO record_sse_event")
        database.execute("RELEASE record_sse_event")
        raise
    return event_id


_DELIVERY_HINT = "delivery-updated"
_DELIVERY_HINT_IDENTITY = ("session", "agent_id", "message_id")


def _egress(payload: dict[str, Any]) -> dict[str, Any] | None:
    """F4: a stored ``delivery-updated`` row leaves as the identity hint ONLY
    (session, agent_id, message_id, plus the hub's type / event_id / ts): any
    stored ``delivery`` projection or other key is dropped on the way out. A row
    whose identity is missing or invalid is dropped from the output. The stored
    row is never rewritten and no id is made up."""
    if payload.get("type") != _DELIVERY_HINT:
        return payload
    if not all(isinstance(payload.get(k), str) and payload.get(k) for k in _DELIVERY_HINT_IDENTITY):
        return None
    return {"type": _DELIVERY_HINT, **{k: payload[k] for k in _DELIVERY_HINT_IDENTITY},
            "ts": payload["ts"], "event_id": payload["event_id"]}


def _decode_event_row(row: sqlite3.Row) -> dict[str, Any] | None:
    try:
        payload = json.loads(row["payload"])
    except (TypeError, json.JSONDecodeError):
        payload = {}
    payload.setdefault("event_id", int(row["event_id"]))
    payload.setdefault("ts", int(row["ts"]))
    payload.setdefault("type", row["type"])
    return _egress(payload)


def events_after(event_id: int, limit: int = 500) -> list[dict[str, Any]]:
    """Up to ``limit`` replayable events after ``event_id``, in event_id order.
    A row dropped on egress does not count against the limit: the read continues
    past it, so a caller's cursor (the last event_id it received) is never stuck
    behind dropped rows. Work: the rows returned plus the rows dropped."""
    limit, after = max(1, min(limit, 5000)), max(0, int(event_id))
    out: list[dict[str, Any]] = []
    while len(out) < limit:
        want = limit - len(out)
        rows = conn().execute(
            """SELECT event_id, ts, type, payload
                 FROM sse_events
                WHERE event_id > ?
                ORDER BY event_id ASC
                LIMIT ?""",
            (after, want),
        ).fetchall()
        out.extend(event for event in map(_decode_event_row, rows) if event is not None)
        if len(rows) < want:
            break
        after = int(rows[-1]["event_id"])
    return out


def recent_events(window_ms: int, limit: int = 500) -> list[dict[str, Any]]:
    cutoff = now_ms() - max(0, int(window_ms))
    rows = conn().execute(
        """SELECT event_id, ts, type, payload
             FROM sse_events
            WHERE ts >= ?
            ORDER BY event_id ASC
            LIMIT ?""",
        (cutoff, max(1, min(limit, 5000))),
    ).fetchall()
    events = [event for event in map(_decode_event_row, rows) if event is not None]
    latest_singletons: dict[str, dict[str, Any]] = {}
    ordered: list[dict[str, Any]] = []
    for event in events:
        event_type = str(event.get("type") or "")
        if event_type in _STATEFUL_SINGLETON_TYPES:
            latest_singletons[event_type] = event
        else:
            ordered.append(event)
    ordered.extend(latest_singletons.values())
    return sorted(ordered, key=lambda event: int(event.get("event_id") or 0))
