"""Durable ledger for explicit queue-after-current-turn requests."""
from __future__ import annotations

from typing import Callable

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
# Work the release drain holds keeps the right to run past an existing Stop
# pause that it already had on arrival (a normal send, a peer message, fresh
# user intent, an explicit send-now; never a goal wake, whose right recovery
# re-checks). It only persists that right: any later Stop clears it
# (``set_paused``). The runtime that recovers the work after the handoff reads
# ``allow_paused``.

def allow_paused(queue_id: str) -> bool:
    cursor = db.conn().execute(
        """UPDATE queued_turns SET allow_paused = 1
            WHERE queue_id = ? AND allow_paused = 0
              AND status IN ('queued', 'claimed', 'parked')""",
        (queue_id,))
    return bool(cursor.rowcount)


def _move_behind(agent_id: str, head: str) -> None:
    """Re-insert every other open row of the agent after ``head``, in their
    order (the caller holds the write transaction)."""
    con = db.conn()
    behind = [row["queue_id"] for row in con.execute(
        """SELECT queue_id FROM queued_turns
            WHERE agent_id = ? AND queue_id <> ? AND status IN ('queued', 'parked')
            ORDER BY queue_seq""", (agent_id, head))]
    for row_id in behind:
        row = con.execute("SELECT * FROM queued_turns WHERE queue_id = ?",
                          (row_id,)).fetchone()
        columns = [key for key in row.keys() if key != "queue_seq"]
        con.execute("DELETE FROM queued_turns WHERE queue_id = ?", (row_id,))
        con.execute(
            f"INSERT INTO queued_turns ({', '.join(columns)}) "
            f"VALUES ({', '.join('?' * len(columns))})",
            [row[key] for key in columns])
    _bump_revision(agent_id)


def park_at_head(*, queue_id: str, agent_id: str, session: str, text: str,
                 trace_id: str, client_msg_id: str, synthesize_audio: bool,
                 origin: str, sender_agent_id: str,
                 prompt_admission_id: str = "") -> None:
    """Park work that was already in flight ahead of everything the agent
    has open, keeping their order behind it. The caller holds the write
    transaction (the release drain's hand-over writes this, the turn's end
    and its state together)."""
    cursor = db.conn().execute(
        """INSERT INTO queued_turns (
               queue_id, agent_id, session, text, trace_id, client_msg_id,
               synthesize_audio, origin, sender_agent_id, status, enqueued_at,
               prompt_admission_id, allow_paused
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'parked', ?, ?, 0)""",
        (queue_id, agent_id, session, text, trace_id, client_msg_id,
         int(synthesize_audio), origin, sender_agent_id, db.now_ms(),
         prompt_admission_id))
    if cursor.rowcount != 1:
        raise RuntimeError(f"could not park {queue_id}")
    _move_behind(agent_id, queue_id)


def queued_row_matches(queue_id: str, *, agent_id: str, trace_id: str,
                       prompt_admission_id: str) -> bool:
    """Whether ``queue_id`` is unambiguously this work's own still-queued
    row: status queued, same agent, trace and admission."""
    if not queue_id:
        return False
    row = db.conn().execute(
        "SELECT agent_id, trace_id, prompt_admission_id, status FROM queued_turns "
        "WHERE queue_id = ?", (queue_id,)).fetchone()
    return bool(row and row["status"] == "queued" and row["agent_id"] == agent_id
                and str(row["trace_id"] or "") == trace_id
                and str(row["prompt_admission_id"] or "") == (prompt_admission_id or ""))


def take_over_at_head(queue_id: str, *, agent_id: str, trace_id: str,
                      prompt_admission_id: str) -> None:
    """Park the work's own queued row (same id, never a second row) ahead of
    everything else the agent has open. The caller holds the write
    transaction; raises when the row is not unambiguously this work's."""
    if not queued_row_matches(queue_id, agent_id=agent_id, trace_id=trace_id,
                              prompt_admission_id=prompt_admission_id):
        raise RuntimeError(f"queued row {queue_id} is not this work's")
    db.conn().execute(
        "UPDATE queued_turns SET status = 'parked' WHERE queue_id = ? AND status = 'queued'",
        (queue_id,))
    _move_behind(agent_id, queue_id)


def remove_taken_over(queue_id: str) -> bool:
    """Its work was cancelled: delete the row the drain's hand-over parked
    in place, matched by id (it may already be queued again after a revert)."""
    if not queue_id:
        return False
    row = db.conn().execute(
        "SELECT agent_id FROM queued_turns WHERE queue_id = ? AND status IN ('parked', 'queued')",
        (queue_id,)).fetchone()
    if row is None:
        return False
    db.conn().execute(
        "DELETE FROM queued_turns WHERE queue_id = ? AND status IN ('parked', 'queued')",
        (queue_id,))
    _bump_revision(str(row["agent_id"]))
    return True


def requeue_rows(queue_ids) -> int:
    """Turn the given parked rows back into queued work (a lifted fence)."""
    count = 0
    for queue_id in queue_ids:
        count += db.conn().execute(
            "UPDATE queued_turns SET status = 'queued' WHERE queue_id = ? AND status = 'parked'",
            (queue_id,)).rowcount or 0
    return count


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


# A finishing turn records its terminal state just before it gives its slot
# back (turn_dispatch.LEAKED_SLOT_GRACE_MS); settle_receipt ends its receipt.
IDLE_RECEIPT_SETTLE_GRACE_MS = 60_000
# A receipt this old on an agent with no busy state ends even when no newer
# state proves it: the agent has been idle far longer than any turn runs.
IDLE_RECEIPT_MAX_AGE_MS = 24 * 3_600_000


def terminalize_idle_agent_receipts(
        is_live: Callable[[str], bool], *, now_ms: int | None = None,
        settle_grace_ms: int = IDLE_RECEIPT_SETTLE_GRACE_MS,
        max_age_ms: int = IDLE_RECEIPT_MAX_AGE_MS) -> dict[str, int]:
    """End every ``started`` receipt of an agent that is not running a turn.

    terminalize_ended_receipts needs the ledger to prove a turn ended, and a
    trace whose ``turns`` row never settled, on an agent with no later turn,
    stayed ``started`` for weeks (46 rows after the 2026-10-11 restart). Here
    the agent proves it: ``is_live(agent_id)`` is false (no slot, spawn,
    terminal or process; an error counts as live) and either its newest state
    is terminal, at least ``settle_grace_ms`` old and newer than the receipt's
    start, or the receipt is older than ``max_age_ms`` and the agent has no
    busy state. Such a receipt becomes ``ended``. Idempotent; queued, claimed,
    parked and terminal rows are never touched, and the row keeps
    deduplicating client retries. Returns {"ended": n} or {}."""
    from .turn_lifecycle import TERMINAL
    now = db.now_ms() if now_ms is None else now_ms
    con = db.conn()
    rows = con.execute(
        """SELECT q.queue_id, q.agent_id,
                  COALESCE(q.started_at, q.enqueued_at) AS started_at,
                  l.kind, l.ts
             FROM queued_turns q
             LEFT JOIN state_log l ON l.state_id = (
                  SELECT s.state_id FROM state_log s
                   WHERE s.agent_id = q.agent_id
                   ORDER BY s.ts DESC, s.state_id DESC LIMIT 1)
            WHERE q.status = 'started'""").fetchall()
    candidates: dict[str, list[str]] = {}
    for row in rows:
        kind = str(row["kind"] or "")
        if kind and kind not in TERMINAL:
            continue
        settled_after = (kind and int(row["ts"]) > int(row["started_at"])
                         and now - int(row["ts"]) >= settle_grace_ms)
        if settled_after or now - int(row["started_at"]) >= max_age_ms:
            candidates.setdefault(str(row["agent_id"]), []).append(
                str(row["queue_id"]))
    queue_ids = []
    for agent_id, ids in candidates.items():
        try:
            if is_live(agent_id):
                continue
        except Exception as exc:  # noqa: BLE001 - can't tell -> leave it
            log("queueReceiptIdleCheckFail", f"agent={agent_id} {exc!r}")
            continue
        queue_ids.extend(ids)
    if not queue_ids:
        return {}
    con.execute("BEGIN IMMEDIATE")
    try:
        ended = sum(
            con.execute(
                """UPDATE queued_turns SET status = 'ended'
                    WHERE queue_id = ? AND status = 'started'""",
                (queue_id,)).rowcount for queue_id in queue_ids)
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise
    if not ended:
        return {}
    log("queueReceiptsTerminalized", f"reason=idle_agent ended={ended}")
    return {"ended": ended}


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
            # A listen-first opening passes a pause that existed when the
            # call was answered (agent_rings); it never ends that pause.
            if (not queue_id.startswith("call-open:")
                    and pending_count(str(row["agent_id"])) == 0
                    and set_paused(str(row["agent_id"]), False)):
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


# --- listen-first openings (agent_rings): writes run in the caller's transaction ---

def withdraw_unclaimed(queue_id: str) -> bool:
    """A waiting (queued or Stop-held), unclaimed row that must never run."""
    cursor = db.conn().execute(
        """UPDATE queued_turns SET status = 'withdrawn', text = ''
            WHERE queue_id = ? AND status IN ('queued', 'parked') AND claimed_at IS NULL""",
        (queue_id,))
    return cursor.rowcount == 1


def claim_for_dispatch(queue_id: str) -> bool:
    """The dispatcher's claim of an opening row before it spawns."""
    cursor = db.conn().execute(
        """UPDATE queued_turns SET status = 'claimed', claimed_at = ?
            WHERE queue_id = ? AND status = 'queued'""", (db.now_ms(), queue_id))
    return cursor.rowcount == 1


def allow_paused_for(queue_id: str) -> None:
    """The opening row may pass a pause that already existed at the answer."""
    db.conn().execute("UPDATE queued_turns SET allow_paused = 1 WHERE queue_id = ?", (queue_id,))


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


STOP_GENERATION_KEY = "turn_queue.stop_generation."


def stop_generation(agent_id: str) -> int:
    """How many times this agent's queue was paused by a Stop or cancel: an
    ordering marker that moves on every Stop, even onto an already paused
    queue (agent_rings fences a listen-first opening against it)."""
    from . import settings_store
    return settings_store.get_int(STOP_GENERATION_KEY + agent_id, default=0)


def set_paused(agent_id: str, paused: bool) -> bool:
    if paused:
        from . import settings_store
        settings_store.increment(STOP_GENERATION_KEY + agent_id)
        # A Stop (the only pauser) takes back every right the release drain
        # persisted for this agent's waiting work, as it does for work waiting
        # in memory when the runtime restarts: nothing queued before this Stop
        # runs past it after a handoff. Also when the queue was already paused.
        db.conn().execute(
            "UPDATE queued_turns SET allow_paused = 0"
            " WHERE agent_id = ? AND allow_paused = 1", (agent_id,))
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
