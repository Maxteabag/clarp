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
    cur = conn().execute(
        """INSERT INTO sse_events (ts, type, session, agent_id, payload)
           VALUES (?, ?, ?, ?, ?)""",
        (ts, event_type, session, agent_id, json.dumps(payload, separators=(",", ":"))),
    )
    event_id = int(cur.lastrowid or 0)
    conn().execute(
        "UPDATE sse_events SET payload = ? WHERE event_id = ?",
        (json.dumps({**payload, "event_id": event_id}, separators=(",", ":")),
         event_id),
    )
    return event_id


def _decode_event_row(row: sqlite3.Row) -> dict[str, Any]:
    try:
        payload = json.loads(row["payload"])
    except (TypeError, json.JSONDecodeError):
        payload = {}
    payload.setdefault("event_id", int(row["event_id"]))
    payload.setdefault("ts", int(row["ts"]))
    payload.setdefault("type", row["type"])
    return payload


def events_after(event_id: int, limit: int = 500) -> list[dict[str, Any]]:
    rows = conn().execute(
        """SELECT event_id, ts, type, payload
             FROM sse_events
            WHERE event_id > ?
            ORDER BY event_id ASC
            LIMIT ?""",
        (max(0, int(event_id)), max(1, min(limit, 5000))),
    ).fetchall()
    return [_decode_event_row(row) for row in rows]


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
    events = [_decode_event_row(row) for row in rows]
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
