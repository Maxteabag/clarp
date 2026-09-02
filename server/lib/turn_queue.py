"""Durable ledger for explicit queue-after-current-turn requests."""
from __future__ import annotations

from . import db, prompt_admissions

def enqueue(*, queue_id: str, agent_id: str, session: str, text: str,
            trace_id: str, client_msg_id: str, synthesize_audio: bool,
            origin: str, sender_agent_id: str,
            prompt_admission_id: str = "") -> bool:
    cursor = db.conn().execute(
        """INSERT INTO queued_turns (
               queue_id, agent_id, session, text, trace_id, client_msg_id,
               synthesize_audio, origin, sender_agent_id, enqueued_at,
               prompt_admission_id
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(queue_id) DO NOTHING""",
        (queue_id, agent_id, session, text, trace_id, client_msg_id,
         int(synthesize_audio), origin, sender_agent_id, db.now_ms(),
         prompt_admission_id),
    )
    inserted = cursor.rowcount == 1
    if inserted:
        _bump_revision(agent_id)
    return inserted


def contains(queue_id: str) -> bool:
    if not queue_id:
        return False
    return db.conn().execute(
        "SELECT 1 FROM queued_turns WHERE queue_id = ? AND status IN ('queued', 'claimed')",
        (queue_id,)
    ).fetchone() is not None


def status(queue_id: str) -> str:
    if not queue_id:
        return ""
    row = db.conn().execute(
        "SELECT status FROM queued_turns WHERE queue_id = ?", (queue_id,)
    ).fetchone()
    return str(row["status"] or "") if row else ""


def mark_started(queue_id: str) -> None:
    """Keep a payload-free idempotency tombstone for as long as the message.

    Durable user messages do not currently expire, so deleting this receipt on
    a timer would allow an old client retry to execute completed work again.
    Agent deletion intentionally removes its receipts along with queued work.
    """
    if queue_id:
        row = db.conn().execute(
            "SELECT agent_id FROM queued_turns WHERE queue_id = ? AND status IN ('queued', 'claimed')",
            (queue_id,),
        ).fetchone()
        cursor = db.conn().execute(
            """UPDATE queued_turns
                  SET status = 'started', text = '', sender_agent_id = '',
                      started_at = ?
                WHERE queue_id = ? AND status IN ('queued', 'claimed')""",
            (db.now_ms(), queue_id))
        if cursor.rowcount and row:
            _bump_revision(str(row["agent_id"]))
            if pending_count(str(row["agent_id"])) == 0:
                set_paused(str(row["agent_id"]), False)


def remove(queue_id: str) -> bool:
    if not queue_id:
        return False
    row = get(queue_id)
    if not row:
        return False
    con = db.conn()
    con.execute("BEGIN IMMEDIATE")
    try:
        cursor = con.execute(
            "DELETE FROM queued_turns WHERE queue_id = ? AND status = 'queued'",
            (queue_id,),
        )
        if cursor.rowcount:
            prompt_admissions.delete_unmaterialized(
                str(row.get("prompt_admission_id") or "")
            )
            _bump_revision(str(row["agent_id"]))
            if pending_count(str(row["agent_id"])) == 0:
                set_paused(str(row["agent_id"]), False)
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise
    return bool(cursor.rowcount)


def get(queue_id: str) -> dict | None:
    row = db.conn().execute(
        "SELECT * FROM queued_turns WHERE queue_id = ? AND status = 'queued'",
        (queue_id,),
    ).fetchone()
    return dict(row) if row else None


def update_text(queue_id: str, text: str) -> bool:
    value = text.strip()
    if not value:
        return False
    con = db.conn()
    con.execute("BEGIN IMMEDIATE")
    try:
        row = con.execute(
            "SELECT * FROM queued_turns WHERE queue_id = ? AND status = 'queued'",
            (queue_id,),
        ).fetchone()
        if row is None:
            con.execute("COMMIT")
            return False
        cursor = con.execute(
            """UPDATE queued_turns SET text = ?
                WHERE queue_id = ? AND status = 'queued'""",
            (value, queue_id),
        )
        if cursor.rowcount:
            prompt_admissions.update_for_queued_edit(
                str(row["prompt_admission_id"] or ""), value,
            )
            _bump_revision(str(row["agent_id"]))
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise
    return bool(cursor.rowcount)


def claim(queue_id: str) -> dict | None:
    """Atomically reserve one queued item for an explicit manual send."""
    row = db.conn().execute(
        """UPDATE queued_turns SET status = 'claimed', claimed_at = ?
              WHERE queue_id = ? AND status = 'queued'
          RETURNING *""",
        (db.now_ms(), queue_id),
    ).fetchone()
    if row:
        _bump_revision(str(row["agent_id"]))
    return dict(row) if row else None


def release_claim(queue_id: str) -> bool:
    row = db.conn().execute(
        """UPDATE queued_turns SET status = 'queued', claimed_at = NULL
              WHERE queue_id = ? AND status = 'claimed'
          RETURNING agent_id""",
        (queue_id,),
    ).fetchone()
    if row:
        _bump_revision(str(row["agent_id"]))
    return row is not None


def reset_stale_claims(max_age_ms: int = 30_000) -> int:
    """Return abandoned manual-send claims to the visible durable queue."""
    cutoff = db.now_ms() - max(0, max_age_ms)
    rows = db.conn().execute(
        """UPDATE queued_turns SET status = 'queued', claimed_at = NULL
              WHERE status = 'claimed' AND COALESCE(claimed_at, 0) <= ?
          RETURNING agent_id""",
        (cutoff,),
    ).fetchall()
    for agent_id in {str(row["agent_id"]) for row in rows}:
        _bump_revision(agent_id)
    return len(rows)


def claimed_count() -> int:
    row = db.conn().execute(
        "SELECT COUNT(*) AS count FROM queued_turns WHERE status = 'claimed'"
    ).fetchone()
    return int(row["count"] if row else 0)


def remove_for_agent(agent_id: str) -> int:
    con = db.conn()
    con.execute("BEGIN IMMEDIATE")
    try:
        rows = con.execute(
            "SELECT prompt_admission_id FROM queued_turns WHERE agent_id = ?",
            (agent_id,),
        ).fetchall()
        cursor = con.execute(
            "DELETE FROM queued_turns WHERE agent_id = ?", (agent_id,),
        )
        removed = int(cursor.rowcount or 0)
        for row in rows:
            prompt_admissions.delete_unmaterialized(
                str(row["prompt_admission_id"] or "")
            )
        if removed:
            _bump_revision(agent_id)
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise
    return removed


def pending(agent_id: str = "") -> list[dict]:
    if agent_id:
        rows = db.conn().execute(
            """SELECT * FROM queued_turns
                 WHERE status = 'queued' AND agent_id = ? ORDER BY queue_seq""",
            (agent_id,),
        )
    else:
        rows = db.conn().execute(
            "SELECT * FROM queued_turns WHERE status = 'queued' ORDER BY queue_seq")
    return [dict(row) for row in rows]


def pending_counts() -> dict[str, int]:
    return {
        str(row["agent_id"]): int(row["count"])
        for row in db.conn().execute(
            """SELECT agent_id, COUNT(*) AS count
                 FROM queued_turns
                WHERE status = 'queued'
                GROUP BY agent_id""")
    }


def pending_count(agent_id: str) -> int:
    row = db.conn().execute(
        """SELECT COUNT(*) AS count FROM queued_turns
            WHERE agent_id = ? AND status = 'queued'""",
        (agent_id,),
    ).fetchone()
    return int(row["count"] if row else 0)


def state(agent_id: str) -> dict[str, int | bool]:
    """Return the count and revision from one SQLite read snapshot."""
    row = db.conn().execute(
        """SELECT
               (SELECT COUNT(*) FROM queued_turns
                 WHERE agent_id = ? AND status = 'queued') AS pending_count,
               COALESCE((SELECT revision FROM queue_state_revisions
                          WHERE agent_id = ?), 0) AS revision,
               COALESCE((SELECT paused FROM queue_state_revisions
                          WHERE agent_id = ?), 0) AS paused""",
        (agent_id, agent_id, agent_id),
    ).fetchone()
    return {
        "count": int(row["pending_count"] or 0),
        "revision": int(row["revision"] or 0),
        "paused": bool(row["paused"]),
    }


def states() -> dict[str, dict[str, int | bool]]:
    return {
        str(row["agent_id"]): {
            "count": int(row["pending_count"] or 0),
            "revision": int(row["revision"] or 0),
            "paused": bool(row["paused"]),
        }
        for row in db.conn().execute(
            """SELECT r.agent_id,
                      COALESCE(q.pending_count, 0) AS pending_count,
                      r.revision,
                      r.paused
                 FROM queue_state_revisions r
                 LEFT JOIN (
                     SELECT agent_id, COUNT(*) AS pending_count
                       FROM queued_turns
                      WHERE status = 'queued'
                      GROUP BY agent_id
                 ) q ON q.agent_id = r.agent_id""")
    }


def revision(agent_id: str) -> int:
    row = db.conn().execute(
        "SELECT revision FROM queue_state_revisions WHERE agent_id = ?",
        (agent_id,),
    ).fetchone()
    return int(row["revision"] or 0) if row else 0


def is_paused(agent_id: str) -> bool:
    row = db.conn().execute(
        "SELECT paused FROM queue_state_revisions WHERE agent_id = ?", (agent_id,)
    ).fetchone()
    return bool(row and row["paused"])


def set_paused(agent_id: str, paused: bool) -> bool:
    if is_paused(agent_id) == paused:
        return False
    db.conn().execute(
        """INSERT INTO queue_state_revisions (agent_id, revision, paused)
             VALUES (?, 1, ?)
             ON CONFLICT(agent_id) DO UPDATE
               SET revision = revision + 1, paused = excluded.paused""",
        (agent_id, int(paused)),
    )
    return True


def _bump_revision(agent_id: str) -> None:
    db.conn().execute(
        """INSERT INTO queue_state_revisions (agent_id, revision) VALUES (?, 1)
           ON CONFLICT(agent_id) DO UPDATE SET revision = revision + 1""",
        (agent_id,),
    )
