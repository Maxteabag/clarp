"""Durable ledger for explicit queue-after-current-turn requests."""
from __future__ import annotations

from . import db, prompt_admissions
from .log import log

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


# --- Stop-parked sends ---------------------------------------------------------
#
# A send admitted while the Stop barrier is up has no durable queue row of its
# own unless it asked to queue. It used to wait only in the dispatcher's memory
# and vanished with the process; ``parked`` rows keep it. They are invisible to
# the queue UI (``pending``/``state`` count ``queued`` only) and become ``queued``
# again when ``turn_slots.rehydrate`` runs after a restart.

def park(*, queue_id: str, agent_id: str, session: str, text: str,
         trace_id: str, client_msg_id: str, synthesize_audio: bool,
         origin: str, sender_agent_id: str,
         prompt_admission_id: str = "", allow_paused: bool = False) -> bool:
    cursor = db.conn().execute(
        """INSERT INTO queued_turns (
               queue_id, agent_id, session, text, trace_id, client_msg_id,
               synthesize_audio, origin, sender_agent_id, status, enqueued_at,
               prompt_admission_id, allow_paused
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'parked', ?, ?, ?)
           ON CONFLICT(queue_id) DO NOTHING""",
        (queue_id, agent_id, session, text, trace_id, client_msg_id,
         int(synthesize_audio), origin, sender_agent_id, db.now_ms(),
         prompt_admission_id, int(allow_paused)),
    )
    return cursor.rowcount == 1


# --- release drain ---------------------------------------------------------------
#
# Work the release drain holds keeps the right to run past a Stop pause when it
# had that right on arrival (a normal send, or fresh user intent). The runtime
# that recovers it after the handoff reads ``allow_paused``.

def allow_paused(queue_id: str) -> bool:
    cursor = db.conn().execute(
        """UPDATE queued_turns SET allow_paused = 1
            WHERE queue_id = ? AND allow_paused = 0
              AND status IN ('queued', 'claimed', 'parked')""",
        (queue_id,))
    return bool(cursor.rowcount)


def move_to_back(queue_id: str) -> bool:
    """Give an open row a new, newest position, everything else unchanged,
    so rows written later for older work can be ordered in front of it."""
    con = db.conn()
    con.execute("BEGIN IMMEDIATE")
    try:
        row = con.execute(
            """SELECT * FROM queued_turns WHERE queue_id = ?
                 AND status IN ('queued', 'parked')""", (queue_id,)).fetchone()
        if row is not None:
            columns = [key for key in row.keys() if key != "queue_seq"]
            con.execute("DELETE FROM queued_turns WHERE queue_id = ?", (queue_id,))
            con.execute(
                f"INSERT INTO queued_turns ({', '.join(columns)}) "
                f"VALUES ({', '.join('?' * len(columns))})",
                [row[key] for key in columns])
            _bump_revision(str(row["agent_id"]))
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise
    return row is not None


def holds_client_message(client_msg_id: str) -> bool:
    """Whether an open (queued, claimed or parked) row carries this message."""
    if not client_msg_id:
        return False
    return db.conn().execute(
        """SELECT 1 FROM queued_turns WHERE client_msg_id = ?
             AND status IN ('queued', 'claimed', 'parked') LIMIT 1""",
        (client_msg_id,)).fetchone() is not None


def unpark(queue_id: str) -> bool:
    if not queue_id:
        return False
    cursor = db.conn().execute(
        "DELETE FROM queued_turns WHERE queue_id = ? AND status = 'parked'",
        (queue_id,))
    return bool(cursor.rowcount)


def discard_parked(agent_id: str) -> int:
    """Drop every parked row of an agent whose memory queue was cleared."""
    cursor = db.conn().execute(
        "DELETE FROM queued_turns WHERE agent_id = ? AND status = 'parked'",
        (agent_id,))
    return int(cursor.rowcount or 0)


def parked(agent_id: str = "") -> list[dict]:
    if agent_id:
        rows = db.conn().execute(
            """SELECT * FROM queued_turns
                WHERE status = 'parked' AND agent_id = ? ORDER BY queue_seq""",
            (agent_id,))
    else:
        rows = db.conn().execute(
            "SELECT * FROM queued_turns WHERE status = 'parked' ORDER BY queue_seq")
    return [dict(row) for row in rows]


def requeue_parked(agent_id: str = "") -> int:
    """Turn parked rows back into visible queued work (after a restart)."""
    if agent_id:
        rows = db.conn().execute(
            """UPDATE queued_turns SET status = 'queued'
                WHERE status = 'parked' AND agent_id = ? RETURNING agent_id""",
            (agent_id,)).fetchall()
    else:
        rows = db.conn().execute(
            """UPDATE queued_turns SET status = 'queued'
                WHERE status = 'parked' RETURNING agent_id""").fetchall()
    for owner in {str(row["agent_id"]) for row in rows}:
        _bump_revision(owner)
    return len(rows)


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


# A queued message's receipt after it left the queue:
#   started -> its turn is running;
#   done / failed / interrupted -> its turn settled with that outcome;
#   ended -> its turn is over but the ledger never recorded how (a later turn
#            of the same agent began, which one agent's slot only allows after
#            this one ended).
# Every receipt, terminal or not, keeps deduplicating client retries.
_RECEIPT_STATUS = {"completed": "done", "failed": "failed",
                   "interrupted": "interrupted"}
_SETTLEABLE = ("started", "done", "failed", "interrupted", "ended")


def settle_receipt(agent_id: str, trace_id: str, outcome: str) -> int:
    """Its turn settled: the receipt of the message that started it ends too.

    The newest settlement wins, so a same-trace retry that later succeeds
    turns a ``failed`` receipt into ``done``."""
    if not trace_id:
        return 0
    status = _RECEIPT_STATUS.get(outcome, "ended")
    cursor = db.conn().execute(
        f"""UPDATE queued_turns SET status = ?
              WHERE agent_id = ? AND trace_id = ? AND status <> ?
                AND status IN ({','.join('?' * len(_SETTLEABLE))})""",
        (status, agent_id, trace_id, status, *_SETTLEABLE))
    return int(cursor.rowcount or 0)


def terminalize_ended_receipts(live_traces: frozenset[str] | set[str] = frozenset()
                               ) -> dict[str, int]:
    """Startup repair: end every ``started`` receipt whose turn is over.

    Before receipts were settled with their turn, every one stayed ``started``
    forever (Pebble, 2026-10-10: 77 of them, read as stuck work). A receipt
    ends when a turn row of its trace settled, or when a later turn of the
    same agent began. A trace in ``live_traces`` (it holds a slot now) and a
    receipt with neither signal stay ``started``. Idempotent; queued, claimed
    and parked rows are never touched. Returns {status: count}."""
    con = db.conn()
    rows = con.execute(
        """SELECT q.queue_id, q.trace_id,
                  (SELECT t.outcome FROM turns t
                    WHERE t.agent_id = q.agent_id AND t.trace_id = q.trace_id
                      AND t.settled_at IS NOT NULL
                    ORDER BY t.settled_at DESC, t.turn_id DESC LIMIT 1) AS outcome,
                  EXISTS (SELECT 1 FROM turns t
                           WHERE t.agent_id = q.agent_id
                             AND t.trace_id <> q.trace_id
                             AND t.started_at > COALESCE(q.started_at, q.enqueued_at)
                         ) AS later
             FROM queued_turns q
            WHERE q.status = 'started'""").fetchall()
    updates: dict[str, list[str]] = {}
    for row in rows:
        if row["trace_id"] in live_traces:
            continue
        if row["outcome"]:
            status = _RECEIPT_STATUS.get(str(row["outcome"]), "ended")
        elif row["later"]:
            status = "ended"
        else:
            continue
        updates.setdefault(status, []).append(str(row["queue_id"]))
    if not updates:
        return {}
    con.execute("BEGIN IMMEDIATE")
    try:
        counts = {}
        for status, queue_ids in updates.items():
            counts[status] = sum(
                con.execute(
                    """UPDATE queued_turns SET status = ?
                        WHERE queue_id = ? AND status = 'started'""",
                    (status, queue_id)).rowcount for queue_id in queue_ids)
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise
    counts = {status: n for status, n in counts.items() if n}
    if counts:
        log("queueReceiptsTerminalized",
            " ".join(f"{status}={n}" for status, n in sorted(counts.items())))
    return counts


def mark_started(queue_id: str) -> None:
    """Keep a payload-free idempotency tombstone for as long as the message.

    Durable user messages do not currently expire, so deleting this receipt on
    a timer would allow an old client retry to execute completed work again.
    Agent deletion intentionally removes its receipts along with queued work.
    The receipt is ``started`` while its turn runs; settle_receipt ends it.
    """
    if queue_id:
        row = db.conn().execute(
            "SELECT agent_id, origin, trace_id FROM queued_turns WHERE queue_id = ? AND status IN ('queued', 'claimed')",
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
            # A fast backend may settle its turn before the dispatcher gets
            # here; the receipt then ends at once.
            settled = db.conn().execute(
                """SELECT outcome FROM turns
                    WHERE agent_id = ? AND trace_id = ? AND settled_at IS NOT NULL
                      AND NOT EXISTS (
                          SELECT 1 FROM turns o
                           WHERE o.agent_id = turns.agent_id
                             AND o.trace_id = turns.trace_id
                             AND o.settled_at IS NULL AND o.ended_at IS NULL)
                    ORDER BY settled_at DESC LIMIT 1""",
                (row["agent_id"], row["trace_id"])).fetchone()
            if settled:
                settle_receipt(str(row["agent_id"]), str(row["trace_id"]),
                               str(settled["outcome"] or ""))
            if pending_count(str(row["agent_id"])) == 0 and set_paused(str(row["agent_id"]), False):
                # Nothing is held any more, so a Stop's pause ends here. Say
                # which item ended it: a peer message or goal wake can do so.
                log("queuePauseEndedByDrain",
                    f"agent={row['agent_id']} queue={queue_id} origin={row['origin']}")


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


def cancel(queue_id: str) -> bool:
    """Retire fenced work once, preserving a payload-free retry receipt."""
    con = db.conn()
    con.execute("BEGIN IMMEDIATE")
    try:
        original = con.execute(
            "SELECT prompt_admission_id FROM queued_turns WHERE queue_id=?",
            (queue_id,),
        ).fetchone()
        row = con.execute(
            """UPDATE queued_turns
                  SET status='cancelled', text='', sender_agent_id='',
                      prompt_admission_id='', claimed_at=NULL
                WHERE queue_id=? AND status IN ('queued','claimed','parked')
                RETURNING agent_id""",
            (queue_id,),
        ).fetchone()
        if row:
            prompt_admissions.delete_unmaterialized(
                str(original["prompt_admission_id"] or ""))
            _bump_revision(str(row["agent_id"]))
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise
    return row is not None


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


def cancel_for_traces(c, agent_id: str, trace_ids: list[str]) -> None:
    """Cancel an agent's pending rows for ``trace_ids`` inside the caller's
    transaction (the janitor fence commits them with its own rows)."""
    for trace in trace_ids:
        c.execute("UPDATE queued_turns SET status='cancelled',text='' "
                  "WHERE agent_id=? AND trace_id=? AND status IN ('queued','claimed')",
                  (agent_id, trace))
