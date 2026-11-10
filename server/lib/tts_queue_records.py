"""DB-backed TTS work queue.

Hooks call `enqueue(...)` to request an utterance be spoken. A server-side
worker (`lib.tts_worker.TTSWorker`) drains the queue at ~100ms intervals,
calls ElevenLabs, writes the clip, and marks the row done.

Status flow:
    queued  ──claim──►  synthesizing  ──ok──►  done
                                ╰─error──►  failed

`claim_next` is an atomic SELECT-then-UPDATE-WHERE-status='queued' pair so
two workers can't pick the same row. The worker runs a small pool (VP-06)
and passes the agents it already has in flight, so each agent's rows are
synthesized one at a time and in order.

`cancel` drops an agent's queued rows (a user Stop, a preempted turn): they
become `failed` with an error starting `cancelled: `, which readers that count
failures leave out. A row already synthesizing finishes.
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
    cur = conn().execute(
        """INSERT INTO tts_queue (agent_id, text, voice_id, session,
                                  source, trace_id, status, enqueued_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (agent_id, text, voice_id, session, source, trace_id,
         QUEUED, now_ms()),
    )
    return int(cur.lastrowid or 0)


def claim_next(busy_agents: frozenset[str] | set[str] = frozenset()) -> dict[str, Any] | None:
    """Atomically claim the oldest queued row of an agent not in
    `busy_agents`. Returns the row dict, or None if there's nothing queued."""
    c = conn()
    busy = sorted(busy_agents)
    row = c.execute(
        f"""SELECT queue_id, agent_id, text, voice_id, session,
                  source, trace_id, enqueued_at
             FROM tts_queue
            WHERE status = ?
              AND agent_id NOT IN ({",".join("?" * len(busy))})
            ORDER BY enqueued_at, queue_id LIMIT 1""",
        (QUEUED, *busy),
    ).fetchone()
    if row is None:
        return None
    cur = c.execute(
        """UPDATE tts_queue SET status = ?, claimed_at = ?
            WHERE queue_id = ? AND status = ?""",
        (SYNTHESIZING, now_ms(), row["queue_id"], QUEUED),
    )
    if cur.rowcount == 0:
        # Lost the race — another claimer got it.
        return None
    return dict(row)


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


CANCELLED_PREFIX = "cancelled: "


def cancel(agent_id: str, trace_id: str | None = None, *, reason: str) -> int:
    """Drop an agent's queued rows (only that trace's when given) so they are
    never synthesized. Returns how many were dropped."""
    sql = """UPDATE tts_queue SET status = ?, completed_at = ?, error = ?
              WHERE agent_id = ? AND status = ?"""
    params: list[Any] = [FAILED, now_ms(), (CANCELLED_PREFIX + reason)[:500], agent_id, QUEUED]
    if trace_id is not None:
        sql += " AND trace_id = ?"
        params.append(trace_id)
    return int(conn().execute(sql, params).rowcount or 0)


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


def fail_stale_in_flight(claimed_before_ms: int) -> int:
    """Fail `synthesizing` rows claimed before the cutoff: after a restart
    their reply is too old to speak (VP-05)."""
    cur = conn().execute(
        """UPDATE tts_queue SET status = ?, completed_at = ?, error = ?
            WHERE status = ? AND claimed_at < ?""",
        (FAILED, now_ms(), "stale after a restart", SYNTHESIZING, claimed_before_ms),
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
