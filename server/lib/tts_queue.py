"""DB-backed TTS work queue.

Hooks call `enqueue(...)` to request an utterance be spoken. A server-side
worker (`lib.tts_worker.TTSWorker`) drains the queue at ~100ms intervals,
calls ElevenLabs, writes the clip, and marks the row done.

Status flow:
    queued  ──claim──►  synthesizing  ──ok──►  done
                                ╰─error──►  failed

`claim_next` is an atomic SELECT-then-UPDATE-WHERE-status='queued' pair so
two workers can't pick the same row. Today there's only one worker, but
the discipline is cheap and protects against a future split.
"""
from __future__ import annotations

from typing import Any

from .db import conn, now_ms


# Single-source-of-truth statuses. Lifecycle column 'status' on tts_queue.
QUEUED        = "queued"
SYNTHESIZING  = "synthesizing"
DONE          = "done"
FAILED        = "failed"


def enqueue(*,
            agent_id: str,
            text: str,
            voice_id: str,
            session: str,
            source: str,
            trace_id: str | None = None,
            synthesize_audio: bool = True) -> int:
    """Append one utterance to the queue. Returns the queue_id.

    Caller (hook or future watcher) is responsible for deciding WHAT to
    speak (cursor advance, sanitization, persona prefix). The queue is
    the producer/consumer seam; everything before the seam is decision
    logic, everything after the seam is mechanical synthesis.

    Returns 0 for a silent turn. No row is inserted, so the worker never sees
    the request and ElevenLabs is never billed.
    """
    if not synthesize_audio:
        from .log import log
        log("ttsSuppressedSilentTurn", f"agent={agent_id} session={session}")
        return 0
    connection = conn()
    connection.execute("BEGIN IMMEDIATE")
    try:
        owner = connection.execute(
            """SELECT agent_id FROM agents
                 WHERE agent_id=? AND session=? AND deleted_at IS NULL""",
            (agent_id, session),
        ).fetchone()
        if owner is None:
            raise ValueError(
                f"agent session changed before TTS enqueue: {session}")
        cur = connection.execute(
            """INSERT INTO tts_queue (agent_id, text, voice_id, session,
                                      source, trace_id, status, enqueued_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (agent_id, text, voice_id, session, source, trace_id,
             QUEUED, now_ms()),
        )
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    return int(cur.lastrowid or 0)


def claim_next() -> dict[str, Any] | None:
    """Atomically claim the oldest queued row. Returns the row dict, or
    None if there's nothing queued."""
    c = conn()
    c.execute("BEGIN IMMEDIATE")
    try:
        row = c.execute(
            """SELECT q.queue_id,q.agent_id,q.text,q.voice_id,q.session,
                      q.source,q.trace_id,q.enqueued_at,
                      a.agent_id AS live_agent_id
                 FROM tts_queue q
                 LEFT JOIN agents a ON a.agent_id=q.agent_id
                   AND a.session=q.session AND a.deleted_at IS NULL
                WHERE q.status=?
                ORDER BY q.enqueued_at LIMIT 1""",
            (QUEUED,),
        ).fetchone()
        if row is None:
            c.execute("COMMIT")
            return None
        if row["live_agent_id"] is None:
            c.execute(
                """UPDATE tts_queue SET status=?,completed_at=?,error=?
                     WHERE queue_id=? AND status=?""",
                (FAILED, now_ms(), "agent session no longer active",
                 row["queue_id"], QUEUED),
            )
            c.execute("COMMIT")
            return None
        cur = c.execute(
            """UPDATE tts_queue SET status = ?, claimed_at = ?
                WHERE queue_id = ? AND status = ?""",
            (SYNTHESIZING, now_ms(), row["queue_id"], QUEUED),
        )
        c.execute("COMMIT")
    except BaseException:
        c.execute("ROLLBACK")
        raise
    if cur.rowcount == 0:
        return None
    result = dict(row)
    result.pop("live_agent_id", None)
    return result


def mark_done(queue_id: int, *, clip_id: int | None = None) -> None:
    conn().execute(
        """UPDATE tts_queue SET status = ?, completed_at = ?, clip_id = ?
            WHERE queue_id = ?""",
        (DONE, now_ms(), clip_id, queue_id),
    )


def mark_failed(queue_id: int, error: str) -> None:
    conn().execute(
        """UPDATE tts_queue SET status = ?, completed_at = ?, error = ?
            WHERE queue_id = ?""",
        (FAILED, now_ms(), error[:500] if error else None, queue_id),
    )


# ---- observability helpers ---------------------------------------------

def pending_count() -> int:
    row = conn().execute(
        "SELECT COUNT(*) AS n FROM tts_queue WHERE status = ?",
        (QUEUED,),
    ).fetchone()
    return int(row["n"]) if row else 0


def in_flight_count() -> int:
    row = conn().execute(
        "SELECT COUNT(*) AS n FROM tts_queue WHERE status = ?",
        (SYNTHESIZING,),
    ).fetchone()
    return int(row["n"]) if row else 0


def recent(limit: int = 50) -> list[dict[str, Any]]:
    rows = conn().execute(
        """SELECT queue_id, agent_id, status, error, enqueued_at,
                  claimed_at, completed_at, clip_id
             FROM tts_queue
             ORDER BY enqueued_at DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


# ---- cost ceiling -------------------------------------------------------

def recent_synth_count(window_ms: int) -> int:
    """How many rows have been enqueued in the last `window_ms`. Used by
    the worker / hook to enforce a cost ceiling."""
    cutoff = now_ms() - window_ms
    row = conn().execute(
        "SELECT COUNT(*) AS n FROM tts_queue WHERE enqueued_at > ?",
        (cutoff,),
    ).fetchone()
    return int(row["n"]) if row else 0


# ---- janitor ------------------------------------------------------------

def prune_old(*, max_age_ms: int) -> int:
    """Delete done/failed rows older than `max_age_ms`. Returns count deleted."""
    cutoff = now_ms() - max_age_ms
    cur = conn().execute(
        """DELETE FROM tts_queue
            WHERE status IN (?, ?) AND completed_at < ?""",
        (DONE, FAILED, cutoff),
    )
    return int(cur.rowcount or 0)


def reset_in_flight() -> int:
    """Reset rows stuck in `synthesizing` back to `queued`. Called at
    server start so a worker that died mid-claim doesn't strand its row."""
    cur = conn().execute(
        "UPDATE tts_queue SET status = ?, claimed_at = NULL WHERE status = ?",
        (QUEUED, SYNTHESIZING),
    )
    return int(cur.rowcount or 0)
