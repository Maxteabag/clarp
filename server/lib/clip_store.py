"""SQLite persistence for clip lifecycle state."""
from __future__ import annotations

import sqlite3
import json
from typing import Any, Callable

from .db import conn, now_ms
from .protocol import ClipProducerStatus, ClipStatus


def record_clip(*, agent_id: str, path: str, voice_id: str | None = None,
                trace_id: str | None = None, byte_count: int | None = None,
                turn_id: int | None = None,
                producer_status: str | None = None,
                status: str = ClipStatus.SYNTHESIZED,
                runtime_id: Callable[[str], int | None]) -> int | None:
    rt = runtime_id(agent_id)
    if status not in ClipStatus.valid():
        raise ValueError(f"invalid clip status: {status}")
    if producer_status is not None and producer_status not in ClipProducerStatus.valid():
        raise ValueError(f"invalid clip producer status: {producer_status}")
    try:
        names = [
            "agent_id", "runtime_id", "turn_id", "path", "voice_id", "bytes",
            "trace_id", "created_at", "status", "producer_status", "completed_at",
        ]
        values: list[Any] = [
            agent_id, rt, turn_id, path, voice_id, byte_count, trace_id,
            now_ms(), status, producer_status or ClipProducerStatus.COMPLETE,
            now_ms() if producer_status in (None, ClipProducerStatus.COMPLETE) else None,
        ]
        placeholders = ", ".join("?" for _ in names)
        cur = conn().execute(
            f"INSERT INTO clips ({', '.join(names)}) VALUES ({placeholders})",
            tuple(values),
        )
        return int(cur.lastrowid or 0)
    except sqlite3.IntegrityError:
        row = conn().execute(
            "SELECT clip_id FROM clips WHERE path = ?", (path,)
        ).fetchone()
        return int(row["clip_id"]) if row else None


def mark_clip_producer_status(*, clip_id: int, producer_status: str,
                              byte_count: int | None = None,
                              error: str | None = None) -> bool:
    if producer_status not in ClipProducerStatus.valid():
        raise ValueError(f"invalid clip producer status: {producer_status}")
    fields = ["producer_status = ?"]
    values: list[Any] = [producer_status]
    if byte_count is not None:
        fields.append("bytes = ?")
        values.append(byte_count)
    if producer_status in (ClipProducerStatus.COMPLETE, ClipProducerStatus.FAILED):
        fields.append("completed_at = COALESCE(completed_at, ?)")
        values.append(now_ms())
    if error is not None:
        fields.append("error = ?")
        values.append(error[:500])
    values.append(clip_id)
    cur = conn().execute(
        f"UPDATE clips SET {', '.join(fields)} WHERE clip_id = ?",
        tuple(values),
    )
    return bool(cur.rowcount)


def mark_clip_status(*, clip_id: int | None = None, url: str | None = None,
                     status: str, error: str | None = None) -> bool:
    if status not in ClipStatus.valid():
        raise ValueError(f"invalid clip status: {status}")
    now = now_ms()
    fields = ["status = ?"]
    values: list[Any] = [status]
    if status == ClipStatus.BROADCAST:
        fields.append("broadcast_at = COALESCE(broadcast_at, ?)")
        values.append(now)
    elif status in (ClipStatus.QUEUED, ClipStatus.HELD):
        fields.append("queued_at = COALESCE(queued_at, ?)")
        values.append(now)
    elif status == ClipStatus.PLAY_START:
        fields.append("play_started_at = COALESCE(play_started_at, ?)")
        values.append(now)
    elif status == ClipStatus.PLAY_OK:
        fields.append("played_at = COALESCE(played_at, ?)")
        values.append(now)
    elif status == ClipStatus.PLAY_FAIL:
        fields.append("error = ?")
        values.append(error or "")
    if clip_id:
        values.append(clip_id)
        cur = conn().execute(
            f"UPDATE clips SET {', '.join(fields)} WHERE clip_id = ?",
            tuple(values),
        )
    else:
        name = (url or "").rsplit("/", 1)[-1]
        if not name:
            return False
        values.append(f"%/{name}")
        cur = conn().execute(
            f"UPDATE clips SET {', '.join(fields)} WHERE path LIKE ?",
            tuple(values),
        )
    return bool(cur.rowcount)


def recoverable_events(*, session: str = "", max_age_ms: int = 10 * 60 * 1000,
                       limit: int = 3, held_limit: int = 64) -> list[dict[str, Any]]:
    """Recent audio events that never reached a terminal playback ack.

    SSE remains the normal transport. This bounded read path lets an APNs wake
    or foreground recovery replay a clip whose stream was suspended after the
    client had already persisted its SSE cursor.
    """
    session = session.strip()
    session_sql = "AND s.session = ?" if session else ""
    held_params: list[Any] = [ClipStatus.HELD, ClipProducerStatus.COMPLETE,
                              ClipProducerStatus.FAILED]
    recent_params: list[Any] = [now_ms() - max(0, int(max_age_ms)),
                                ClipStatus.HELD, ClipStatus.PLAY_OK,
                                ClipStatus.PLAY_FAIL,
                                ClipProducerStatus.COMPLETE,
                                ClipProducerStatus.FAILED]
    if session:
        held_params.append(session)
        recent_params.append(session)
    held_params.append(max(1, min(int(held_limit), 64)))
    recent_params.append(max(1, min(int(limit), 10)))
    rows = conn().execute(
        f"""WITH held AS (
                SELECT s.event_id, s.payload
                  FROM sse_events s
                  JOIN clips c
                    ON c.clip_id = CAST(json_extract(s.payload, '$.clip_id') AS INTEGER)
                 WHERE s.type = 'audio'
                   AND c.status = ?
                   AND COALESCE(c.producer_status, ?) != ?
                   {session_sql}
                 ORDER BY s.event_id DESC
                 LIMIT ?
              ), recent AS (
                SELECT s.event_id, s.payload
                  FROM sse_events s
                  JOIN clips c
                    ON c.clip_id = CAST(json_extract(s.payload, '$.clip_id') AS INTEGER)
                 WHERE s.type = 'audio'
                   AND s.ts >= ?
                   AND c.status != ?
                   AND c.status NOT IN (?, ?)
                   AND COALESCE(c.producer_status, ?) != ?
                   {session_sql}
                 ORDER BY s.event_id DESC
                 LIMIT ?
              )
              SELECT event_id, payload FROM held
              UNION ALL
              SELECT event_id, payload FROM recent
              ORDER BY event_id""",
        tuple(held_params + recent_params),
    ).fetchall()
    by_clip: dict[str, dict[str, Any]] = {}
    for row in rows:
        try:
            event = json.loads(row["payload"])
        except (TypeError, json.JSONDecodeError):
            continue
        key = str(event.get("clip_id") or event.get("url") or row["event_id"])
        by_clip.setdefault(key, event)
    return sorted(by_clip.values(), key=lambda event: int(event.get("event_id") or 0))
