"""Durable bridge between one Oracle Realtime session and Clarp agents."""
from __future__ import annotations

from typing import Any, Callable
import re

from . import db


ACTIVE = frozenset({"accepted", "queued"})
TERMINAL = frozenset({"completed", "failed", "cancelled"})


class DelegationCollision(ValueError):
    pass


class DelegationNotCancellable(ValueError):
    pass


_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")


def normalize_id(value: str) -> str:
    result = str(value or "").strip()
    if not _ID_RE.fullmatch(result):
        raise ValueError("invalid delegation_id")
    return result


def _row(row) -> dict[str, Any] | None:
    if row is None:
        return None
    value = dict(row)
    value["delivered"] = value.get("delivered_at") is not None
    return value


def get(delegation_id: str) -> dict[str, Any] | None:
    return _row(db.conn().execute(
        "SELECT * FROM oracle_delegations WHERE delegation_id = ?",
        (delegation_id.strip(),),
    ).fetchone())


def begin(*, delegation_id: str, trace_id: str, client_msg_id: str,
          agent_id: str, session: str, request_text: str,
          owner_principal: str = "administrator") -> tuple[dict, bool]:
    delegation_id = normalize_id(delegation_id)
    trace_id = trace_id.strip()
    client_msg_id = client_msg_id.strip()
    session = session.strip()
    request_text = request_text.strip()
    owner_principal = str(owner_principal or "").strip()
    if not all((delegation_id, trace_id, client_msg_id, agent_id, session,
                request_text, owner_principal)):
        raise ValueError("complete Oracle delegation identity is required")
    database = db.conn()
    database.execute("BEGIN IMMEDIATE")
    try:
        existing = database.execute(
            "SELECT * FROM oracle_delegations WHERE delegation_id = ?",
            (delegation_id,),
        ).fetchone()
        if existing is not None:
            comparable = (
                existing["trace_id"], existing["client_msg_id"],
                existing["agent_id"], existing["session"],
                existing["request_text"], existing["owner_principal"],
            )
            incoming = (
                trace_id, client_msg_id, agent_id, session, request_text,
                owner_principal,
            )
            if comparable != incoming:
                raise DelegationCollision(
                    "delegation_id was already used for different work")
            database.execute("COMMIT")
            return _row(existing) or {}, False
        now = db.now_ms()
        database.execute(
            """INSERT INTO oracle_delegations (
                   delegation_id, owner_principal, trace_id, client_msg_id,
                   agent_id, session,
                   request_text, status, created_at, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, 'accepted', ?, ?)""",
            (delegation_id, owner_principal, trace_id, client_msg_id, agent_id, session,
             request_text, now, now),
        )
        row = database.execute(
            "SELECT * FROM oracle_delegations WHERE delegation_id = ?",
            (delegation_id,),
        ).fetchone()
        database.execute("COMMIT")
        return _row(row) or {}, True
    except BaseException:
        if database.in_transaction:
            database.execute("ROLLBACK")
        raise


def dispatch(*, ctx, delegation_id: str, session: str,
             request_text: str, authenticated_at_admission: bool,
             owner_principal: str = "administrator") -> dict:
    """Idempotently admit a silent, durable agent turn for Oracle."""
    from . import agents as agents_db
    from . import message_store, prompt_admissions
    from .turn_dispatch import TurnDispatchService

    delegation_id = normalize_id(delegation_id)
    session = str(session or "").strip()
    request_text = str(request_text or "").strip()
    if not session or not request_text:
        raise ValueError("session and request are required")
    agent = agents_db.get_by_session(session)
    if not agent:
        raise LookupError("unknown agent")
    trace_id = f"oracle-{delegation_id}"
    client_msg_id = trace_id
    record, created = begin(
        delegation_id=delegation_id,
        trace_id=trace_id,
        client_msg_id=client_msg_id,
        agent_id=agent["agent_id"],
        session=session,
        request_text=request_text,
        owner_principal=owner_principal,
    )
    # A retry after admission must not spawn a second turn. Terminal rows are
    # immutable delivery records: returning one is idempotent, while running
    # it again could repeat consequential work without any way to attach the
    # later result. Only an active row persisted immediately before a process
    # crash may resume when no durable user message exists yet.
    if not created:
        if record.get("status") in TERMINAL:
            return record
        if message_store.has_client_message(client_msg_id):
            return record
    admission = prompt_admissions.create(
        authenticated_at_admission=authenticated_at_admission,
        origin="oracle",
        sender_agent_id="",
        channel="oracle",
        observed_at=db.now_ms(),
        client_admission_id=client_msg_id,
        trace_id=trace_id,
        original_text=request_text,
    )
    try:
        result = TurnDispatchService(ctx).dispatch(
            text=request_text,
            requested_session=session,
            forced_session=session,
            trace_id=trace_id,
            client_msg_id=client_msg_id,
            synthesize_audio=False,
            origin="oracle",
            prompt_admission=admission,
            queue_if_busy=True,
        )
    except Exception as exc:
        fail(delegation_id, str(exc))
        raise
    fresh_agent = agents_db.get_by_session(result.session) or agent
    backend_session_id = agents_db.live_backend_session(fresh_agent["agent_id"])
    return mark_dispatched(
        delegation_id,
        backend_session_id=backend_session_id,
        queued=result.queued,
    ) or record


def mark_dispatched(delegation_id: str, *, backend_session_id: str,
                    queued: bool) -> dict | None:
    database = db.conn()
    database.execute("BEGIN IMMEDIATE")
    try:
        status = "accepted"
        if queued:
            row = database.execute(
                """SELECT q.status
                     FROM oracle_delegations d
                     LEFT JOIN queued_turns q ON q.queue_id = d.client_msg_id
                    WHERE d.delegation_id = ?""",
                (delegation_id.strip(),),
            ).fetchone()
            # Holding SQLite's write reservation makes this decision mutually
            # exclusive with queue claim/start. Claimed or started is active.
            if row is not None and row["status"] == "queued":
                status = "queued"
        database.execute(
            """UPDATE oracle_delegations
                  SET backend_session_id = ?, status = ?, updated_at = ?
                WHERE delegation_id = ? AND status IN ('accepted', 'queued')""",
            (backend_session_id or "", status, db.now_ms(),
             delegation_id.strip()),
        )
        database.execute("COMMIT")
    except BaseException:
        if database.in_transaction:
            database.execute("ROLLBACK")
        raise
    return get(delegation_id)


def mark_started_for_trace(trace_id: str) -> bool:
    """A durable queued turn became the active backend turn."""
    return db.conn().execute(
        """UPDATE oracle_delegations
              SET status = 'accepted', updated_at = ?
            WHERE trace_id = ? AND status = 'queued'""",
        (db.now_ms(), str(trace_id or "").strip()),
    ).rowcount > 0


def complete_for_trace(*, trace_id: str, message_id: str, text: str) -> bool:
    """Attach the exact terminal assistant row to its Oracle delegation."""
    clean = str(text or "").strip()
    if not trace_id or not message_id or not clean:
        return False
    changed = db.conn().execute(
        """UPDATE oracle_delegations
              SET status = 'completed', result_message_id = ?,
                  result_text = ?, error = '', updated_at = ?
            WHERE trace_id = ? AND status IN ('accepted', 'queued')""",
        (message_id, clean, db.now_ms(), trace_id.strip()),
    ).rowcount
    return changed > 0


def fail(delegation_id: str, reason: str) -> dict | None:
    db.conn().execute(
        """UPDATE oracle_delegations
              SET status = 'failed', error = ?, updated_at = ?
            WHERE delegation_id = ? AND status IN ('accepted', 'queued')""",
        ((reason or "Agent turn failed")[:500], db.now_ms(),
         delegation_id.strip()),
    )
    return get(delegation_id)


def fail_for_trace(trace_id: str, reason: str) -> bool:
    return db.conn().execute(
        """UPDATE oracle_delegations
              SET status = 'failed', error = ?, updated_at = ?
            WHERE trace_id = ? AND status IN ('accepted', 'queued')""",
        ((reason or "Agent turn failed")[:500], db.now_ms(), trace_id.strip()),
    ).rowcount > 0


def reconcile_orphans(*, is_live) -> int:
    """Terminalize started turns whose process-local owner was lost.

    Replaying them after a restart could repeat consequential actions. Rows
    still waiting in the durable queue have no user message yet and are left
    untouched for normal queue recovery.
    """
    rows = db.conn().execute(
        """SELECT delegation_id, agent_id, trace_id, client_msg_id
             FROM oracle_delegations
            WHERE status IN ('accepted', 'queued')"""
    ).fetchall()
    changed = 0
    from . import message_store
    for row in rows:
        if not message_store.has_client_message(str(row["client_msg_id"] or "")):
            continue
        if is_live(str(row["agent_id"]), str(row["trace_id"] or "")):
            continue
        trace_id = str(row["trace_id"] or "")
        committed = db.conn().execute(
            """SELECT message_id, text
                 FROM messages
                WHERE role = 'assistant'
                  AND source_file IN (?, ?)
                  AND TRIM(text) <> ''
                ORDER BY revision DESC LIMIT 1""",
            (f"final:{trace_id}", f"live:{trace_id}"),
        ).fetchone()
        if committed is not None and complete_for_trace(
            trace_id=trace_id,
            message_id=str(committed["message_id"]),
            text=str(committed["text"]),
        ):
            changed += 1
            continue
        failed = fail(
            str(row["delegation_id"]),
            "Agent turn was interrupted before a durable result; delegate it again",
        )
        if failed and failed["status"] == "failed":
            changed += 1
    return changed


def cancel(
    delegation_id: str, *, owner_principal: str = "administrator"
) -> dict | None:
    database = db.conn()
    database.execute("BEGIN IMMEDIATE")
    try:
        existing = database.execute(
            """SELECT * FROM oracle_delegations
                WHERE delegation_id = ? AND owner_principal = ?""",
            (delegation_id.strip(), owner_principal),
        ).fetchone()
        if existing is None:
            database.execute("COMMIT")
            return None
        if existing["status"] in ACTIVE:
            database.execute("COMMIT")
            raise DelegationNotCancellable(
                "nonterminal delegation requires agent cancellation")
        database.execute("COMMIT")
    except BaseException:
        if database.in_transaction:
            database.execute("ROLLBACK")
        raise
    return get(delegation_id)


def cancel_for_session(
    session: str, *, stop: Callable[[], Any],
    owner_principal: str = "administrator"
) -> list[dict]:
    """Stop the agent first, then finalize the exact previously-active rows."""
    session = str(session or "").strip()
    owner_principal = str(owner_principal or "").strip()
    if not session or not owner_principal:
        raise ValueError("session and owner principal are required")
    database = db.conn()
    candidates = database.execute(
        """SELECT delegation_id, trace_id, client_msg_id, status
             FROM oracle_delegations
            WHERE session = ? AND owner_principal = ?
              AND status IN ('accepted', 'queued')""",
        (session, owner_principal),
    ).fetchall()
    ids = [str(row["delegation_id"]) for row in candidates]
    # The durable rows remain recoverable if the authoritative local stop
    # raises. This callback runs inside the server request, removing the former
    # two-request partial-failure window.
    release = stop()
    try:
        if ids:
            placeholders = ",".join("?" for _ in ids)
            database.execute("BEGIN IMMEDIATE")
            try:
                database.execute(
                    f"""UPDATE oracle_delegations
                           SET status = 'cancelled', updated_at = ?
                         WHERE delegation_id IN ({placeholders})
                           AND owner_principal = ?
                           AND status IN ('accepted', 'queued', 'failed')""",
                    (db.now_ms(), *ids, owner_principal),
                )
                database.execute("COMMIT")
            except BaseException:
                if database.in_transaction:
                    database.execute("ROLLBACK")
                raise
    finally:
        # Remove durable rows before releasing the Stop barrier, then remove
        # the same trace IDs from the process-local queue in the release hook.
        from . import turn_queue
        for row in candidates:
            if row["status"] == "queued":
                turn_queue.remove(str(row["client_msg_id"] or ""))
        if callable(release):
            release({str(row["trace_id"] or "") for row in candidates})
    return [
        row for delegation_id in ids
        if (row := get(delegation_id)) and row["status"] == "cancelled"
    ]


def undelivered(
    *, owner_principal: str = "administrator", limit: int = 50
) -> list[dict]:
    rows = db.conn().execute(
        """SELECT * FROM oracle_delegations
            WHERE delivered_at IS NULL
              AND owner_principal = ?
              AND status IN ('completed', 'failed', 'cancelled')
            ORDER BY updated_at, delegation_id
            LIMIT ?""",
        (owner_principal, max(1, min(int(limit), 200))),
    ).fetchall()
    return [_row(row) or {} for row in rows]


def acknowledge(
    delegation_id: str, *, owner_principal: str = "administrator"
) -> bool:
    return db.conn().execute(
        """UPDATE oracle_delegations
              SET delivered_at = COALESCE(delivered_at, ?), updated_at = ?
            WHERE delegation_id = ?
              AND owner_principal = ?
              AND status IN ('completed', 'failed', 'cancelled')""",
        (db.now_ms(), db.now_ms(), delegation_id.strip(), owner_principal),
    ).rowcount > 0


def recent(
    *, owner_principal: str = "administrator", limit: int = 50
) -> list[dict]:
    rows = db.conn().execute(
        """SELECT * FROM oracle_delegations
            WHERE owner_principal = ?
            ORDER BY updated_at DESC, delegation_id DESC LIMIT ?""",
        (owner_principal, max(1, min(int(limit), 200))),
    ).fetchall()
    return [_row(row) or {} for row in rows]
