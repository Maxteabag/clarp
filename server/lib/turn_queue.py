"""Durable ledger for explicit queue-after-current-turn requests."""
from __future__ import annotations

import threading
import json
import contextlib
import sys
import time
from typing import Callable

from . import db, prompt_admissions
from . import lifecycle as lk
from .lifecycle import Deadline, Edge, Lifecycle, WaitForever
from .log import log


def _receipt_failure(event: str, queue_ids, exc: BaseException, *, agents=None) -> None:
    """A failed receipt write must name its receipts without logging content."""
    for queue_id in queue_ids:
        detail = (f"queue={queue_id} agent={(agents or {}).get(queue_id, 'unknown')} "
                  f"event={event} type={type(exc).__name__} "
                  f"code={getattr(exc, 'sqlite_errorcode', 'none')}")
        try:
            if db.conn().in_transaction:
                # Joined writers belong to the caller: no recursive eventlog
                # work and no rollback/commit from this diagnostic boundary.
                print(f"queueReceiptWriteFail {detail}", file=sys.stderr, flush=True)
            else:
                log("queueReceiptWriteFail", detail)
        except Exception as diagnostic_error:  # keep the primary write failure
            try:
                print(f"queueReceiptWriteFail {detail} diagnostic={type(diagnostic_error).__name__}",
                      file=sys.stderr, flush=True)
            except Exception as fallback_error:
                # Both sinks failed. Record that loss on the ORIGINAL error;
                # a diagnostic must never replace SQLite's failure or code.
                BaseException.add_note(exc, f"queueReceiptDiagnosticLost {detail} "
                                       f"sink={type(fallback_error).__name__}")


@contextlib.contextmanager
def _receipt_write(event: str, queue_ids, *, agent_ids=None):
    """Join a caller transaction, or atomically write a receipt and revision.

    BEGIN IMMEDIATE uses db's existing plain-BUSY polling and this connection's
    original busy deadline. Never retry an admitted body, fetch or commit.
    """
    con = db.conn()
    started = time.monotonic()
    agents = dict(agent_ids or {})
    queue_ids = tuple(queue_ids)
    try:
        if agent_ids is None:
            for queue_id in queue_ids:
                row = con.execute("SELECT agent_id FROM queued_turns WHERE queue_id = ?",
                                  (queue_id,)).fetchone()
                if row:
                    agents[queue_id] = str(row[0])
        if con.in_transaction:
            yield con
        else:
            with lk.transaction(con):
                yield con
    except Exception as exc:
        _receipt_failure(f"{event}:elapsed_ms={round((time.monotonic() - started) * 1000)}",
                         queue_ids, exc, agents=agents)
        raise


def _launch_status(row) -> str:
    from . import agents, launch_receipts
    raw = launch_receipts.settings_store.get(launch_receipts._key(str(row["trace_id"])))
    receipt = None
    if raw is not None:
        try:
            receipt = json.loads(raw)
        except ValueError:
            return "unknown"  # present malformed is not missing/unsent
        if not isinstance(receipt, dict):
            return "unknown"
    if receipt is not None and (receipt.get("agent_id") != row["agent_id"]
                    or receipt.get("trace_id") != row["trace_id"]
                    or receipt.get("client_msg_id") != row["client_msg_id"]
                    or receipt.get("origin") != row["origin"]
                    or (row["status"] in ("queued", "claimed")
                        and (receipt.get("sender_agent_id") or "")
                        != (row["sender_agent_id"] or ""))):
        return "unknown"  # a trace alias does not transfer another admission's authority
    if row["prompt_admission_id"]:
        admission = db.conn().execute(
            "SELECT * FROM prompt_admissions WHERE admission_id=?",
            (row["prompt_admission_id"],)).fetchone()
        if (admission is None or any(admission[field] != row[field] for field in
                                     ("agent_id", "session", "trace_id", "origin"))
                or admission["client_admission_id"] != (row["client_msg_id"] or row["trace_id"])
                or (row["status"] in ("queued", "claimed")
                    and (admission["sender_agent_id"] or "") != (row["sender_agent_id"] or ""))):
            return "unknown"
    attempt = agents._receipt_attempt_status(str(row["agent_id"]), str(row["trace_id"]))
    if attempt is None:
        # No-turn durable work still has its first-launch route. Possibly-sent
        # steer receipts do not become new independent launches.
        if receipt and receipt.get("state") not in (launch_receipts.PENDING, launch_receipts.FENCED):
            return "unknown"
        return "unstarted"
    return _handoff_launch_status(row, attempt)


def _handoff_launch_status(row, fallback: str) -> str:
    """Administrative frontier proof is queue-local, not global retry authority."""
    con = db.conn()
    marker = con.execute(
        """SELECT detail FROM state_log WHERE agent_id=?
             AND json_extract(detail,'$.trace_id')=? AND kind='idle'
             AND json_extract(detail,'$.reason')='update_handoff'
           ORDER BY state_id DESC LIMIT 1""",
        (row["agent_id"], row["trace_id"])).fetchone()
    if marker is None:
        return fallback
    detail = json.loads(marker["detail"] or "{}")
    frontier = detail.get("dispatch_not_started_through_turn_id")
    if (detail.get("dispatch_not_started") is not True or type(frontier) is not int
            or not 1 <= frontier <= 9223372036854775807):
        return fallback if fallback == "observed" else "unknown"
    selected = con.execute(
        """SELECT turn_id,ended_at,settled_at,outcome FROM turns
            WHERE agent_id=? AND trace_id=? ORDER BY turn_id DESC LIMIT 1""",
        (row["agent_id"], row["trace_id"])).fetchone()
    covered = con.execute(
        "SELECT ended_at,settled_at FROM turns WHERE turn_id=? AND agent_id=? AND trace_id=?",
        (frontier, row["agent_id"], row["trace_id"])).fetchone()
    if covered is None or covered["ended_at"] is None or covered["settled_at"] is not None:
        return "unknown"
    if selected is None or selected["turn_id"] > frontier:
        return fallback  # later evidence wins; an unobserved open attempt is unknown
    if selected["turn_id"] != frontier:
        return "unknown"
    if con.execute(
            """SELECT 1 FROM turns WHERE agent_id=? AND trace_id=?
                 AND outcome IN ('completed','interrupted')
                 AND (settled_at IS NOT NULL OR ended_at IS NOT NULL) LIMIT 1""",
            (row["agent_id"], row["trace_id"])).fetchone():
        return "unknown"
    if con.execute(
            """SELECT 1 FROM queued_turns WHERE agent_id=? AND trace_id=?
                 AND queue_id<>? AND status IN ('queued','claimed','parked','started') LIMIT 1""",
            (row["agent_id"], row["trace_id"], row["queue_id"])).fetchone():
        return "unknown"
    return "handoff"  # exact closed administrative attempt; pause/Resume own admission


def _has_turn(row) -> bool:
    return bool(row["agent_id"] and row["trace_id"] and db.conn().execute(
        "SELECT 1 FROM turns WHERE agent_id = ? AND trace_id = ? LIMIT 1",
        (row["agent_id"], row["trace_id"])).fetchone())


def _consumed_or_uncertain(row) -> bool:
    """A turn alone cannot override explicit, current NOTSTARTED evidence."""
    return _launch_status(row) not in ("retryable", "handoff", "unstarted")

# The receipt's lifecycle (docs/architecture/lifecycles.md; Phase 3 queue
# conversion, docs/architecture/lifecycle-conversion-plan.md section 1). This
# module is the only writer of queued_turns.status. Outside the kit, by design:
# inserts (enqueue, park, park_at_head create a row in queued or parked),
# deletes (remove, remove_taken_over, unpark, discard_parked,
# remove_for_agent take a row out of the lifecycle with their own status
# predicates) and the allow_paused flag, which is not the state.
_SETTLED = ("done", "failed", "interrupted", "ended")
QUEUE_RECEIPT = Lifecycle(
    name="queue_receipt", owner="lib/turn_queue.py",
    table="queued_turns", key="queue_id", state_column="status",
    entered_columns={"claimed": "claimed_at", "started": "started_at"},
    initial="queued",
    final=frozenset({*_SETTLED, "unknown", "cancelled", "withdrawn"}),
    edges=(
        Edge("claim", frozenset({"queued"}), "claimed"),
        Edge("release", frozenset({"claimed"}), "queued"),
        Edge("start", frozenset({"queued", "claimed"}), "started"),
        Edge("park", frozenset({"queued"}), "parked"),
        Edge("requeue", frozenset({"parked"}), "queued"),
        Edge("cancel", frozenset({"queued", "claimed", "parked"}), "cancelled"),
        Edge("cancel_pending", frozenset({"queued", "claimed"}), "cancelled"),
        Edge("withdraw", frozenset({"queued", "parked"}), "withdrawn"),
        Edge("withdraw_claimed", frozenset({"claimed"}), "withdrawn"),
        Edge("mark_unknown", frozenset({"queued", "claimed", "parked"}), "unknown"),
        Edge("call_open_unknown", frozenset({"claimed", "started"}), "unknown"),
        # A turn settles its receipt; the newest settlement wins (resettle).
        *(Edge(f"settle_{s}", frozenset({"started"}), s) for s in _SETTLED),
        *(Edge(f"resettle_{s}", frozenset(_SETTLED) - {s}, s, correction=True) for s in _SETTLED),
        Edge("idle_settle", frozenset({"started"}), "ended"),
        *(Edge(f"ended_settle_{s}", frozenset({"started"}), s) for s in _SETTLED),
    ),
    waits={
        "queued": WaitForever("dispatch, cancel or the agent's deletion ends it"),
        "parked": WaitForever("the user resumes or cancels the paused queue"),
        "claimed": Deadline(30_000, "queued"),
        "started": WaitForever("its turn's settlement ends it; the idle and ended sweeps repair a missed one"),
    },
)


def _cas(events, where: str, params=(), *, values=None, returning: str = "", con=None):
    """Every status write: one compare-and-set statement per target, sources
    from QUEUE_RECEIPT. It joins the caller's open transaction, and runs in
    autocommit otherwise, as each writer did."""
    return lk.compare_and_set_where(con or db.conn(), QUEUE_RECEIPT, events, where=where,
                                    params=params, now=db.now_ms(), values=values,
                                    returning=returning)

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


def _reinsert_at_back(con, row) -> None:
    """Allocate the next queue_seq, preserving every other column.

    The caller owns the write transaction: deleting and reinserting is one
    reorder, not a receipt leaving and re-entering its lifecycle. In
    particular, do not commit here: a drain also records its handoff in that
    transaction. Keep SQLite's AUTOINCREMENT allocation for queue_seq.
    """
    columns = [key for key in row.keys() if key != "queue_seq"]
    con.execute("DELETE FROM queued_turns WHERE queue_id = ?", (row["queue_id"],))
    con.execute(
        f"INSERT INTO queued_turns ({', '.join(columns)}) "
        f"VALUES ({', '.join('?' * len(columns))})",
        [row[key] for key in columns])


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
        _reinsert_at_back(con, row)
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
    _cas("park", "queue_id = ?", (queue_id,))
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
        count += _cas("requeue", "queue_id = ?", (queue_id,))
    return count


def keep_start_parked(agent_id: str, *, skip_trace: str = "") -> int:
    """Turn the agent's waiting start-park rows into visible queued work, so a
    Stop's ``discard_parked`` keeps them in the paused queue (TP-N3). Read
    from the rows themselves, so a Stop retried after a failed write still
    finds them: a row whose trace has a turn (its launch began) or owns the
    slot (``skip_trace``) is left to its launch."""
    count = db.conn().execute(
        """UPDATE queued_turns SET status = 'queued'
            WHERE agent_id = ? AND status = 'parked'
              AND queue_id LIKE 'start-park-%' AND trace_id <> ?
              AND NOT EXISTS (SELECT 1 FROM turns
                               WHERE turns.agent_id = queued_turns.agent_id
                                 AND turns.trace_id = queued_turns.trace_id)""",
        (agent_id, skip_trace)).rowcount or 0
    if count:
        _bump_revision(agent_id)
    return count


def repark(agent_id: str, queue_ids) -> int:
    """Undo ``keep_start_parked`` for rows whose memory spec a rolled-back Stop
    restored: memory runs them, and the row is unparked at their launch."""
    count = 0
    for queue_id in queue_ids:
        count += db.conn().execute(
            """UPDATE queued_turns SET status = 'parked'
                WHERE queue_id = ? AND agent_id = ? AND status = 'queued'""",
            (queue_id, agent_id)).rowcount or 0
    if count:
        _bump_revision(agent_id)
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
            _reinsert_at_back(con, row)
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
        rows = _cas("requeue", "agent_id = ?", (agent_id,), returning="agent_id")
    else:
        rows = _cas("requeue", "1", returning="agent_id")
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
    # From started (settle) or from another settled outcome (resettle): the
    # union is today's ``status <> target AND status IN _SETTLEABLE``.
    queue_ids = [str(row[0]) for row in db.conn().execute(
        "SELECT queue_id FROM queued_turns WHERE agent_id = ? AND trace_id = ?",
        (agent_id, trace_id)).fetchall()]
    if not queue_ids:
        return 0
    with _receipt_write("settle", queue_ids):
        moved = int(_cas((f"settle_{status}", f"resettle_{status}"),
                         "agent_id = ? AND trace_id = ?", (agent_id, trace_id)))
    return moved


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
    affected = [queue_id for queue_ids in updates.values() for queue_id in queue_ids]
    with _receipt_write("end", affected):
        counts = {}
        for status, queue_ids in updates.items():
            counts[status] = sum(
                _cas(f"ended_settle_{status}", "queue_id = ?", (queue_id,), con=con)
                for queue_id in queue_ids)
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

# A separate cursor per thread/connection: overlapping owners do not replace
# one another's progress, and a new database never inherits an old cursor.
_REAP_PROGRESS = threading.local()
_REAP_ROWS_PER_TICK = 100  # Leave headroom for snapshot and revision SQL outside the receipt batch.


def _reap_cursor(name, con):
    previous = getattr(_REAP_PROGRESS, name, None)
    return previous[1] if previous is not None and previous[0] is con else None


def _save_reap_cursor(name, con, cursor):
    setattr(_REAP_PROGRESS, name, (con, cursor))


def reconcile_receipts() -> dict[str, int]:
    """Converge one bounded page from exact durable turn evidence.

    At most 100 receipts are examined per pass. A connection-local keyset
    cursor advances only after commit and wraps, including past no-turn rows
    and unchanged live receipts. Admission gets at most 50ms, with a 200ms
    cooperative pass deadline; no provider/liveness calls run under the writer.
    This never releases work, changes a pause, or infers a steered association.
    """
    con = db.conn()
    if con.in_transaction:
        raise lk.LifecycleError("receipt reconciliation needs its own transaction")
    deadline = time.monotonic() + 0.2
    after = _reap_cursor("convergence", con)
    rows = []
    # One equality seek per status: (status, queue_seq) is the existing index.
    # No global sort or scan through final tombstones precedes LIMIT 100.
    for status in ("claimed", "queued", "started"):
        if after and status < after[0]:
            continue
        if len(rows) == 100 or time.monotonic() >= deadline:
            break
        sequence = after[1] if after and status == after[0] else 0
        rows.extend(con.execute(
            """SELECT queue_id, agent_id, trace_id, prompt_admission_id, queue_seq, status
                 FROM queued_turns INDEXED BY idx_queued_turns_status_seq
                WHERE status = ? AND queue_seq > ?
                ORDER BY queue_seq LIMIT ?""", (status, sequence, 100-len(rows))).fetchall())
    if not rows:
        if time.monotonic() < deadline:
            _save_reap_cursor("convergence", con, None)
        return {}
    counts: dict[str, int] = {}
    repaired = []
    deferred = []
    changed_agents = set()
    cursor = after
    admission_ms = min(50, int((deadline - time.monotonic()) * 1000))
    if admission_ms <= 0:
        return {}  # no admitted work, so no cursor progress or fresh wait
    with db.busy_timeout_at_most(admission_ms):
        with _receipt_write("reconcile", [str(row["queue_id"]) for row in rows],
                            agent_ids={row["queue_id"]: row["agent_id"] for row in rows}):
            for observed in rows:
                if time.monotonic() >= deadline:
                    break
                cursor = (observed["status"], observed["queue_seq"])
                row = con.execute(
                    """SELECT * FROM queued_turns WHERE queue_id = ? AND agent_id = ?
                         AND trace_id = ? AND prompt_admission_id IS ? AND queue_seq = ?
                         AND status IN ('queued', 'claimed', 'started')""",
                    (observed["queue_id"], observed["agent_id"], observed["trace_id"],
                     observed["prompt_admission_id"], observed["queue_seq"])).fetchone()
                if row is None or not row["agent_id"] or not row["trace_id"]:
                    continue
                turn = con.execute(
                    """SELECT turn_id, started_at, ended_at, settled_at, outcome FROM turns
                        WHERE agent_id = ? AND trace_id = ?
                        ORDER BY turn_id DESC LIMIT 1""",
                    (row["agent_id"], row["trace_id"])).fetchone()
                if turn is None:
                    continue
                launch = _launch_status(row)
                if launch != "observed":
                    if launch != "retryable":
                        deferred.append((row["queue_id"], row["agent_id"], launch))
                    continue  # NOTSTARTED, Stop fences and unknowns are not consumed proof.
                running = turn["settled_at"] is None and turn["ended_at"] is None
                target = "started"
                if not running:
                    target = (_RECEIPT_STATUS.get(turn["outcome"], "ended")
                              if turn["settled_at"] is not None else "ended")
                where = "queue_id = ? AND agent_id = ? AND trace_id = ? AND status = ?"
                params = (row["queue_id"], row["agent_id"], row["trace_id"], row["status"])
                moved = 0
                if row["status"] in ("queued", "claimed"):
                    moved = int(_cas("start", where, params,
                                     values={"text": "", "sender_agent_id": ""}, con=con))
                if not running:
                    moved += int(_cas(f"settle_{target}", where,
                                      (*params[:3], "started"), con=con))
                if moved:
                    counts[target] = counts.get(target, 0) + 1
                    changed_agents.add(str(row["agent_id"]))
                    repaired.append((row["queue_id"], row["agent_id"], target,
                                     turn["turn_id"]))
            for agent_id in changed_agents:
                _bump_revision(agent_id)
    _save_reap_cursor("convergence", con, cursor if cursor else None)
    for queue_id, agent_id, target, turn_id in repaired:
        log("queueReceiptReconciled", f"queue={queue_id} agent={agent_id} "
            f"reason=durable_turn target={target} turn={turn_id}")
    for queue_id, agent_id, reason in deferred:
        log("queueReceiptDeferred", f"queue={queue_id} agent={agent_id} reason=launch_{reason}")
    return counts


def terminalize_idle_agent_receipts(
        is_live: Callable[[str], bool], *, now_ms: int | None = None,
        settle_grace_ms: int = IDLE_RECEIPT_SETTLE_GRACE_MS,
        max_age_ms: int = IDLE_RECEIPT_MAX_AGE_MS) -> dict[str, int]:
    """Repair a bounded page of ``started`` receipts on agents with no turn.

    terminalize_ended_receipts needs the ledger to prove a turn ended, and a
    trace whose ``turns`` row never settled, on an agent with no later turn,
    stayed ``started`` for weeks (46 rows after the 2026-10-11 restart). Here
    the agent proves it: ``is_live(agent_id)`` is false (no slot, spawn,
    terminal or process; an error counts as live) and either its newest state
    is terminal, at least ``settle_grace_ms`` old and newer than the receipt's
    start, or the receipt is older than ``max_age_ms`` and the agent has no
    busy state. Such a receipt becomes ``ended``. Idempotent; queued, claimed,
    parked and terminal rows are never touched, and the row keeps
    deduplicating client retries. Each call examines at most 100 receipts,
    resuming its thread/connection's keyset cursor on the next call. Returns
    {"ended": n} or {}. Liveness runs before the write transaction; persisted
    state and age gates are rechecked inside it (the signed reaper race fence)."""
    from .turn_lifecycle import TERMINAL
    now = db.now_ms() if now_ms is None else now_ms
    con = db.conn()
    cursor = _reap_cursor("idle", con)
    keyset = " AND (COALESCE(q.started_at, 0), q.queue_id) > (?, ?)" if cursor else ""
    rows = con.execute(
        """SELECT q.queue_id, q.agent_id,
                  COALESCE(q.started_at, q.enqueued_at) AS started_at,
                  COALESCE(q.started_at, 0) AS cursor_at, l.kind, l.ts
             FROM queued_turns q
             LEFT JOIN state_log l ON l.state_id = (
                  SELECT s.state_id FROM state_log s
                   WHERE s.agent_id = q.agent_id
                   ORDER BY s.ts DESC, s.state_id DESC LIMIT 1)
            WHERE q.status = 'started'""" + keyset
        + " ORDER BY COALESCE(q.started_at, 0), q.queue_id LIMIT 100",
        (cursor[1], cursor[2]) if cursor else ()).fetchall()

    def due(kind, ts, started):
        kind = str(kind or "")
        if kind and kind not in TERMINAL:
            return False
        settled_after = (kind and int(ts) > started and now - int(ts) >= settle_grace_ms)
        return bool(settled_after or now - started >= max_age_ms)

    candidates = {str(row["agent_id"]) for row in rows
                  if due(row["kind"], row["ts"], int(row["started_at"]))}
    idle = set()
    for agent_id in candidates:
        try:
            if is_live(agent_id):
                continue
        except Exception as exc:  # noqa: BLE001 - can't tell -> leave it
            log("queueReceiptIdleCheckFail", f"agent={agent_id} {exc!r}")
            continue
        idle.add(agent_id)
    if not idle:
        last = rows[-1] if len(rows) == _REAP_ROWS_PER_TICK else None
        _save_reap_cursor("idle", con, ("started", last["cursor_at"], last["queue_id"]) if last else None)
        return {}
    observed = {str(row["queue_id"]): str(row["agent_id"]) for row in rows}

    def eligible(row):
        agent_id = str(row["agent_id"])
        if agent_id not in idle or observed.get(str(row["queue_id"])) != agent_id:
            return None
        # Signed queue-reaper race fence: the snapshot and liveness check
        # happened outside this transaction. Re-read persisted state here.
        latest = con.execute(
            "SELECT kind, ts FROM state_log WHERE agent_id = ? "
            "ORDER BY ts DESC, state_id DESC LIMIT 1", (agent_id,)).fetchone()
        started = int(row["started_at"] if row["started_at"] is not None else row["enqueued_at"])
        kind, ts = (latest["kind"], latest["ts"]) if latest else (None, None)
        return "idle_settle" if due(kind, ts, started) else None

    try:
        moved, cursor = lk.reap_tick(con, QUEUE_RECEIPT, now=now, cursor=cursor,
                                     states=("started",), eligible=eligible, max_rows=_REAP_ROWS_PER_TICK)
    except Exception as exc:
        _receipt_failure("idle_end", observed, exc, agents=observed)
        raise
    _save_reap_cursor("idle", con, cursor)
    ended = len(moved)
    if not ended:
        return {}
    log("queueReceiptsTerminalized", f"reason=idle_agent ended={ended}")
    return {"ended": ended}


def mark_launched_call_open_unknown(queue_id: str) -> bool:
    """A launched listen-first initiative whose turn failed after the backend had it
    (agent_rings.resolve_launch_failure): its row becomes the durable ``unknown``, never
    queued, retried or counted again. Only a ``call-open:`` row claimed or started for its one
    launch; an ordinary row is never touched here (mark_unknown keeps its own scope)."""
    queue_id = str(queue_id or "")
    if not queue_id.startswith("call-open:"):
        return False
    return _cas("call_open_unknown", "queue_id = ?", (queue_id,), values={"text": ""}) == 1


def mark_started(queue_id: str) -> None:
    """Keep a payload-free idempotency tombstone for as long as the message.

    Durable user messages do not currently expire, so deleting this receipt on
    a timer would allow an old client retry to execute completed work again.
    Agent deletion intentionally removes its receipts along with queued work.
    The receipt is ``started`` while its turn runs; settle_receipt ends it.
    """
    if queue_id:
        with _receipt_write("start", (queue_id,)):
            row = db.conn().execute(
                "SELECT agent_id, origin, trace_id FROM queued_turns WHERE queue_id = ? AND status IN ('queued', 'claimed')",
                (queue_id,),
            ).fetchone()
            started = _cas("start", "queue_id = ?", (queue_id,),
                           values={"text": "", "sender_agent_id": ""})  # stamps started_at
            if started and row:
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
        if started and row:
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
        moved = _cas("cancel", "queue_id = ?", (queue_id,), con=con, returning="agent_id",
                     values={"text": "", "sender_agent_id": "", "prompt_admission_id": "",
                             "claimed_at": None})
        row = moved[0] if moved else None
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


def dispatchable(queue_id: str, *, agent_id: str, trace_id: str, session: str,
                 client_msg_id: str, origin: str, sender_agent_id: str,
                 prompt_admission_id: str) -> bool:
    """Fresh receipt evidence inside the existing same-client admission gate.

    No write/transaction: claim/release/reset retain their own writer checks.
    A stale recovery snapshot must not bypass terminal/uncertain receipt state.
    """
    row = db.conn().execute("SELECT * FROM queued_turns WHERE queue_id=?", (queue_id,)).fetchone()
    if row is None or row["status"] not in ("queued", "claimed"):
        return False
    expected = {"agent_id": agent_id, "trace_id": trace_id, "session": session,
                "client_msg_id": client_msg_id, "origin": origin,
                "sender_agent_id": sender_agent_id, "prompt_admission_id": prompt_admission_id}
    if any(str(row[key] or "") != str(value or "") for key, value in expected.items()):
        return False
    return not _consumed_or_uncertain(row)


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
    return _cas("withdraw", "queue_id = ? AND claimed_at IS NULL", (queue_id,),
                values={"text": ""}) == 1


def claim_for_dispatch(queue_id: str) -> bool:
    """The dispatcher's claim of an opening row before it spawns (once)."""
    with _receipt_write("claim", (queue_id,)):
        row = get(queue_id)
        if row and _consumed_or_uncertain(row):
            return False
        return _cas("claim", "queue_id = ?", (queue_id,)) == 1  # stamps claimed_at


def mark_unknown(queue_id: str) -> bool:
    """An opening row whose launch can never be known (agent_rings, P4): not
    pending, claimed, settleable or a success, and nothing moves it back."""
    return _cas("mark_unknown", "queue_id = ?", (queue_id,), values={"text": ""}) == 1


def withdraw_claimed(queue_id: str) -> bool:
    """A claimed opening row that provably never passed the dispatcher's gate."""
    return _cas("withdraw_claimed", "queue_id = ?", (queue_id,), values={"text": ""}) == 1


def allow_paused_for(queue_id: str) -> None:
    """The opening row may pass a pause that already existed at the answer."""
    db.conn().execute("UPDATE queued_turns SET allow_paused = 1 WHERE queue_id = ?", (queue_id,))


def claim(queue_id: str) -> dict | None:
    """Atomically reserve one queued item for an explicit manual send."""
    with _receipt_write("claim", (queue_id,)):
        current = get(queue_id)
        if current and _consumed_or_uncertain(current):
            return None
        moved = _cas("claim", "queue_id = ?", (queue_id,), returning="*")  # stamps claimed_at
        row = moved[0] if moved else None
        if row:
            _bump_revision(str(row["agent_id"]))
    return dict(row) if row else None


def release_claim(queue_id: str) -> bool:
    with _receipt_write("release", (queue_id,)):
        current = db.conn().execute(
            "SELECT * FROM queued_turns WHERE queue_id = ? AND status = 'claimed'",
            (queue_id,)).fetchone()
        if current and _consumed_or_uncertain(current):
            return False
        moved = _cas("release", "queue_id = ?", (queue_id,), returning="agent_id",
                     values={"claimed_at": None})
        row = moved[0] if moved else None
        if row:
            _bump_revision(str(row["agent_id"]))
    return row is not None


def reset_stale_claims(max_age_ms: int = 30_000, *, exclude=()) -> int:
    """Return a bounded page of abandoned manual-send claims to the queue.
    ``exclude`` is filtered before the page limit (agent_rings openings).
    Each call moves at most 100 rows and keeps a thread/connection cursor;
    the cutoff still uses this call's own clock. An explicit caller-owned
    transaction is joined without committing or nesting."""
    con, now = db.conn(), db.now_ms()
    excluded = tuple(exclude)
    options = dict(states=("claimed",), cutoffs={"claimed": now - max(0, max_age_ms)},
                   values_by_event={lk.EXPIRED: {"claimed_at": None}},
                   where=("queue_id NOT IN (" + ",".join("?" for _ in excluded) + ")") if excluded else "",
                   params=excluded)
    options["eligible"] = lambda row: None if _consumed_or_uncertain(row) else lk.EXPIRED
    changed_agents = set()

    def agents(moved):
        if not moved:
            return set()
        marks = ",".join("?" for _ in moved)
        return {str(row[0]) for row in con.execute(
            f"SELECT DISTINCT agent_id FROM queued_turns WHERE queue_id IN ({marks})",
            [transition.key for transition in moved])}

    def committed(outbox, moved):
        batch_agents = agents(moved)
        outbox.add(lambda: changed_agents.update(batch_agents))

    cursor = _reap_cursor("claims", con)
    failure_scope = (" AND queue_id NOT IN (" + ",".join("?" for _ in excluded) + ")") if excluded else ""
    failure_args = [now - max(0, max_age_ms), *excluded]
    if cursor:
        failure_scope += " AND (COALESCE(claimed_at, 0), queue_id) > (?, ?)"
        failure_args.extend((cursor[1], cursor[2]))
    failure_rows = con.execute(
        "SELECT queue_id, agent_id FROM queued_turns WHERE status = 'claimed' "
        "AND COALESCE(claimed_at, 0) <= ?" + failure_scope
        + " ORDER BY COALESCE(claimed_at, 0), queue_id LIMIT 100",
        failure_args).fetchall()
    try:
        if con.in_transaction:
            # Join an explicitly owned transaction, never commit it or nest.
            # Production recovery calls outside one; this preserves the API
            # for a caller that composes a reset with its own writes.
            result = lk.reap(con, QUEUE_RECEIPT, now=now, after=cursor, **options)
            moved, cursor = result.moved, result.cursor
            changed_agents.update(agents(moved))
        else:
            moved, cursor = lk.reap_tick(con, QUEUE_RECEIPT, now=now, cursor=cursor,
                                         effects=committed, max_rows=_REAP_ROWS_PER_TICK, **options)
        _save_reap_cursor("claims", con, cursor)
        return len(moved)
    except Exception as exc:
        _receipt_failure("reset_claims", [str(row[0]) for row in failure_rows], exc,
                         agents={str(row[0]): str(row[1]) for row in failure_rows})
        raise
    finally:
        # Preserve the existing tick's partial-commit and once-per-agent
        # revision boundary (including its 100-change writer batch cap).
        primary = sys.exception()
        try:
            if changed_agents:
                if con.in_transaction:
                    for agent_id in changed_agents:
                        _bump_revision(agent_id)
                else:
                    with lk.transaction(con):
                        for agent_id in changed_agents:
                            _bump_revision(agent_id)
        except Exception as exc:
            _receipt_failure("reset_revision", [str(row[0]) for row in failure_rows], exc,
                             agents={str(row[0]): str(row[1]) for row in failure_rows})
            if primary is not None:
                raise primary
            raise


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


def pending_for_dispatch() -> list[dict]:
    """Recovery excludes consumed/uncertain launches independently of page repair.

    Keep pending()/wire counts authoritative to stored receipt state; a queue
    beyond the convergence page must still never cause a second launch.
    Explicit, current NOTSTARTED work keeps its existing recovery eligibility.
    """
    return [row for row in pending() if not _consumed_or_uncertain(row)]


# A queued row whose turn just ended may be a head the dispatcher is about to
# retry from memory (a failed handoff retries within a minute).
DEAD_ROW_GRACE_MS = 120_000


def dead_rows(agent_id: str = "", *, prefix: str = "", now_ms: int | None = None,
              grace_ms: int = DEAD_ROW_GRACE_MS,
              live_trace: Callable[[str, str], bool] | None = None) -> list[dict]:
    """Queued rows that nothing will run again.

    The newest turn of the row's trace is over for at least ``grace_ms`` and
    its launch evidence permits no relaunch: ``pending_for_dispatch`` skips
    such a row, and ``reconcile_receipts`` defers it unless the launch is
    proven. It stays ``queued`` for good; a wake parked for an account and
    then failed before its spawn left one (2026-11-10).

    A turn is over when it settled or was closed. One that was opened and
    never closed (a launch abandoned at a phase nobody could prove; turn rows
    that never end are common) is over only under all of these: its launch is
    unproven (``unknown``: no spawn record, or one that cannot be attributed
    because the trace has more than one turn row, which does not prove that
    it never ran), a later turn of the same agent began at least
    ``grace_ms`` ago, and ``live_trace(agent_id, trace_id)`` says the runtime
    no longer holds the agent's slot for it. A later turn alone proves
    nothing, since a hook inside a running process can mint a new trace; a
    turn that runs or waits for an account holds its slot until it finishes.
    Without ``live_trace``, or when it cannot answer, an open turn is live.

    Read-only: what becomes of the row is its owner's call. The goal
    scheduler retires its own wakes; a message's payload is kept."""
    now = db.now_ms() if now_ms is None else now_ms
    where, args = "status = 'queued'", []
    if agent_id:
        where += " AND agent_id = ?"
        args.append(agent_id)
    if prefix:
        where += " AND substr(queue_id, 1, ?) = ?"
        args.extend((len(prefix), prefix))
    dead = []
    for row in db.conn().execute(
            f"SELECT * FROM queued_turns WHERE {where} ORDER BY queue_seq", args).fetchall():
        row = dict(row)
        if not row["agent_id"] or not row["trace_id"]:
            continue
        turn = db.conn().execute(
            """SELECT turn_id, ended_at, settled_at FROM turns
                WHERE agent_id = ? AND trace_id = ? ORDER BY turn_id DESC LIMIT 1""",
            (row["agent_id"], row["trace_id"])).fetchone()
        if turn is None:
            continue  # never opened: it waits for its dispatch
        over_at = max(int(turn["settled_at"] or 0), int(turn["ended_at"] or 0))
        still_open = not over_at
        if still_open:
            if live_trace is None:
                continue
            moved_on = db.conn().execute(
                """SELECT MIN(started_at) FROM turns
                    WHERE agent_id = ? AND turn_id > ? AND trace_id <> ?""",
                (row["agent_id"], turn["turn_id"], row["trace_id"])).fetchone()[0]
            over_at = int(moved_on or 0)
        if not over_at or now - over_at < grace_ms:
            continue  # running, or a retry may still follow
        launch = _launch_status(row)
        if launch in ("retryable", "handoff", "unstarted"):
            continue  # recovery relaunches it
        if still_open:
            if launch != "unknown":
                continue  # it was spawned: its receipt follows that turn
            try:
                if live_trace(str(row["agent_id"]), str(row["trace_id"])):
                    continue
            except Exception as exc:  # noqa: BLE001 - cannot tell: it stays live
                log("queueDeadRowLiveCheckFail", f"queue={row['queue_id']} {exc!r}")
                continue
        dead.append(row)
    return dead


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


def holds_autonomous_wake(agent_id: str) -> bool:
    """True when a Stop paused the queue or work is already queued or claimed."""
    row = db.conn().execute(
        """SELECT
               COALESCE((SELECT paused FROM queue_state_revisions WHERE agent_id = ?), 0)
               OR EXISTS (
                   SELECT 1 FROM queued_turns
                    WHERE agent_id = ? AND status IN ('queued', 'claimed')
               ) AS held""",
        (agent_id, agent_id),
    ).fetchone()
    return bool(row and row["held"])


STOP_GENERATION_KEY = "turn_queue.stop_generation."


def stop_generation(agent_id: str) -> int:
    """How many times this agent's queue was paused by a Stop or cancel: an
    ordering marker that moves on every Stop, even onto an already paused
    queue (agent_rings fences a listen-first opening against it)."""
    from . import settings_store
    return settings_store.get_int(STOP_GENERATION_KEY + agent_id, default=0)


USER_STOP_GENERATION_KEY = "turn_queue.user_stop_generation."


def user_stop_generation(agent_id: str) -> int:
    """How many times the USER stopped this agent: moved only by a pause that
    is the user's Stop, in that pause's own transaction. Maintenance cancels
    and their restores never move it (launch_receipts reads it)."""
    from . import settings_store
    return settings_store.get_int(USER_STOP_GENERATION_KEY + agent_id, default=0)


def set_paused(agent_id: str, paused: bool, *, user_stop: bool = False) -> bool:
    """Pause or resume the agent's queue. A pause (every Stop or cancel) also
    moves the stop generation, in the same transaction as the pause itself;
    a pause that is the user's Stop (``user_stop``) also moves the user stop
    generation in that same transaction."""
    con = db.conn()
    own = not con.in_transaction
    if own:
        con.execute("BEGIN IMMEDIATE")
    try:
        changed = _set_paused(agent_id, paused, user_stop=user_stop)
        if own:
            con.execute("COMMIT")
    except BaseException:
        if own:
            con.execute("ROLLBACK")
        raise
    return changed


def _set_paused(agent_id: str, paused: bool, *, user_stop: bool = False) -> bool:
    if paused:
        from . import settings_store
        settings_store.increment(STOP_GENERATION_KEY + agent_id)
        if user_stop:
            settings_store.increment(USER_STOP_GENERATION_KEY + agent_id)
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
        _cas("cancel_pending", "agent_id = ? AND trace_id = ?", (agent_id, trace),
             values={"text": ""}, con=c)
