"""Durable message writes.

The client-message idempotency path (`record_user_message` and friends),
server-authored rows (interruption markers, dream digests) and the transcript
import (`store_transcript_turns`), plus the id and revision primitives the
other message modules share.

`message_store` is the facade. Tests monkeypatch `message_store.conn`,
`message_store.IMPORT_*` and `message_store._restore_assistant_state_txn`, so
those are read through `_facade()` at call time rather than from this module's
globals.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Callable

from .clock import iso_from_ms as _iso_from_ms, now_ms
from .db import conn, note_import_holds_lock
from . import dreaming, heartbeat, message_carry, origins, team_leader, team_store
from .message_turns import iso_ms
from .voice_markup import strip_hidden_blocks
from .message_text import (CALL_OPENING_OPEN, _strip_voice_markup, strip_delivery_context,
                           strip_injected_blocks)
from .message_import_common import (  # noqa: F401 - facade compatibility
    _message_id,
    _client_message_id,
    _norm_text,
    _next_revision,
    _row_payload,
    _latest_user_provenance,
    _FINAL_TWIN_WINDOW_S,
    _RETRY_ECHO_SLACK_S,
    _parse_timestamp,
    _is_retry_echo,
    _PARKED_SEQ_BASE,
    _client_provenance_rows,
    _client_provenance,
)
from .message_import_rows import (  # noqa: F401 - facade compatibility
    _own_turns_row,
    _park_adopted_occupant,
    _seq_taken,
    _restore_unadopted_parked,
    _parked_twin,
    _shifted_twin,
    _FinalRows,
    _streamed_twin,
    _diagnose,
    _related,
    _final_twins,
    _delete_slot,
    _ClientProvenance,
    _AgyAuthority,
    _resolve_reply_id,
    _Upserted,
    _upsert_row,
)


from .message_import_writer import (  # noqa: E402,F401  (split_apply: message-import-writer)
    _facade, _WRITE_VERBS, _TailInvalidated, _WriteNeeded, _ImportWriter, _PlanCursor, _PlanWriter,
)
def client_message_trace(client_msg_id: str) -> str:
    """The trace the durable user row was admitted under, or ""."""
    if not client_msg_id.strip():
        return ""
    row = conn().execute(
        "SELECT trace_id FROM messages WHERE message_id = ? AND role = 'user'",
        (_client_message_id(client_msg_id),),
    ).fetchone()
    return str((row["trace_id"] if row else "") or "")


def relink_client_message(client_msg_id: str, *, trace_id: str) -> None:
    """Hand an admitted-but-never-launched user row to a new attempt.

    The row gets a new revision and its conversation head moves with it, in
    the caller's transaction, so delta clients see the new trace (RM-N5)."""
    database = conn()
    new_trace = (trace_id or "").strip() or None
    row = database.execute(
        "SELECT agent_id, backend_session_id, trace_id FROM messages"
        " WHERE message_id = ? AND role = 'user'",
        (_client_message_id(client_msg_id),),
    ).fetchone()
    if row is None or row["trace_id"] == new_trace:
        return
    revision = _next_revision(database)
    # updated_at stays: it is the row's send order, by which the import links
    # transcript copies to client rows.
    database.execute(
        "UPDATE messages SET trace_id = ?, revision = ?"
        " WHERE message_id = ? AND role = 'user'",
        (new_trace, revision, _client_message_id(client_msg_id)),
    )
    database.execute(
        """INSERT INTO conversation_heads (
               agent_id, backend_session_id, revision, replace_revision
           ) VALUES (?, ?, ?, 0)
           ON CONFLICT(agent_id, backend_session_id) DO UPDATE SET
               revision = MAX(conversation_heads.revision, excluded.revision)""",
        (row["agent_id"], row["backend_session_id"], revision),
    )


def has_client_message(client_msg_id: str) -> bool:
    if not client_msg_id.strip():
        return False
    return conn().execute(
        "SELECT 1 FROM messages WHERE message_id = ? AND role = 'user'",
        (_client_message_id(client_msg_id),),
    ).fetchone() is not None


_PHASE_BY_KIND = {"final_answer": "final", "final": "final", "commentary": "commentary"}


def _row_phases(turns: list[dict[str, Any]]) -> list[str | None]:
    """commentary | final per assistant text row: text followed by tool work
    in the same turn is commentary (Codex says so itself), the rest final."""
    phases: list[str | None] = [None] * len(turns)
    work_after = False
    for index in range(len(turns) - 1, -1, -1):
        turn = turns[index]
        if turn.get("role") == "user":
            work_after = False
            continue
        if turn.get("role") != "assistant":
            continue
        if str(turn.get("text") or "").strip():
            phases[index] = _PHASE_BY_KIND.get(str(turn.get("kind") or "")) or (
                "commentary" if work_after else "final")
        if turn.get("tools") or turn.get("display_cells"):
            work_after = True
    return phases


def latest_turn_user_origin(*, agent_id: str, backend_session_id: str = "",
                            done_ts: int = 0) -> str:
    """Origin of the user row that caused the just-finished turn.

    DONE is written by backend hooks and does not carry the dispatch origin.
    Bound the lookup to messages after the previous DONE so a heartbeat,
    dreaming run, or other hidden turn cannot inherit an older user prompt.
    """
    if not agent_id:
        return ""
    database = conn()
    lower_bound = 0
    if done_ts:
        row = database.execute(
            """SELECT COALESCE(MAX(ts), 0) AS previous_done
                 FROM state_log
                WHERE agent_id = ? AND kind = 'done' AND ts < ?""",
            (agent_id, done_ts),
        ).fetchone()
        lower_bound = int(row["previous_done"] or 0) if row else 0
    params: list[Any] = [agent_id, lower_bound]
    where = "agent_id = ? AND role = 'user' AND updated_at >= ?"
    if backend_session_id:
        where += " AND backend_session_id = ?"
        params.append(backend_session_id)
    row = database.execute(
        f"""SELECT origin
              FROM messages
             WHERE {where}
             ORDER BY updated_at DESC, seq DESC
             LIMIT 1""",
        tuple(params),
    ).fetchone()
    return (row["origin"] or "user") if row else ""


def _message_activity_sql() -> str:
    """Semantic message time; rows without a transcript timestamp fall back
    to the time they were written."""
    return (
        "COALESCE("
        "CAST((julianday(timestamp) - 2440587.5) * 86400000 AS INTEGER), "
        "updated_at)"
    )


def record_user_message(*, agent_id: str, backend_session_id: str,
                        client_msg_id: str, text: str,
                        origin: str = "user", sender_agent_id: str | None = None,
                        prompt_admission_id: str = "",
                        trace_id: str = "",
                        ) -> dict[str, Any] | None:
    """Record a user message the moment /send accepts it, keyed by the
    client-authored `client_msg_id` (idempotency key).

    This is the DURABLE user row — not a transient placeholder. The client
    renders its bubble under the same id, so the two match by identity. The
    later transcript import links Claude's copy of this user turn back to this
    row (by send order) rather than inserting a second one, so a message is
    never duplicated or reconciled away.

    User rows live in a negative `seq` band so they can never collide with a
    transcript position (0..N-1); the read model orders by timestamp, so the
    band has no effect on display order. Idempotent: the same client_msg_id is
    a no-op.
    """
    if not backend_session_id or not client_msg_id:
        return None
    database = conn()
    msg_id = _client_message_id(client_msg_id)
    origin = (origin or "user").strip() or "user"
    sender_agent_id = (sender_agent_id or "").strip() or None
    if not prompt_admission_id:
        from . import prompt_admissions
        prompt_admission_id = prompt_admissions.find_for_client(
            agent_id=agent_id, client_admission_id=client_msg_id,
        )
    existing = database.execute(
        """SELECT timestamp, text, revision, origin, sender_agent_id, trace_id
             FROM messages WHERE message_id = ?""",
        (msg_id,),
    ).fetchone()
    if existing is not None:
        return {
            "id": msg_id, "role": "user", "timestamp": existing["timestamp"],
            "text": existing["text"], "kind": None, "tool_name": None,
            "tools": [], "display_cells": [],
            "origin": existing["origin"] or "user",
            "sender_agent_id": existing["sender_agent_id"] or "",
            "trace_id": existing["trace_id"] or "",
            "revision": int(existing["revision"]),
            "created": False,
        }
    row = database.execute(
        """SELECT COALESCE(MIN(seq), 0) - 1 AS next_seq
             FROM messages
            WHERE agent_id = ? AND backend_session_id = ?""",
        (agent_id, backend_session_id),
    ).fetchone()
    seq = min(int(row["next_seq"]), -1)  # negative band, never collides with transcript
    timestamp_ms = now_ms()
    timestamp = _iso_from_ms(timestamp_ms)
    revision = _next_revision(database)
    inserted = database.execute(
        """INSERT INTO messages (
               message_id, agent_id, backend_session_id, source_file, seq,
               role, timestamp, text, kind, tool_name, tools_json,
               display_cells_json, updated_at, revision, origin,
               sender_agent_id, prompt_admission_id, trace_id
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(message_id) DO NOTHING""",
        (
            msg_id, agent_id, backend_session_id, f"client:{client_msg_id}", seq,
            "user", timestamp, text, None, None, "[]", "[]",
            timestamp_ms, revision, origin, sender_agent_id,
            prompt_admission_id or None, (trace_id or "").strip() or None,
        ),
    )
    if inserted.rowcount != 1:
        # Another request admitted the same client id between our SELECT and
        # INSERT. Return the winning durable row and let dispatch admission
        # treat this request as a retry rather than launching it again.
        return record_user_message(
            agent_id=agent_id,
            backend_session_id=backend_session_id,
            client_msg_id=client_msg_id,
            text=text,
            origin=origin,
            sender_agent_id=sender_agent_id,
            prompt_admission_id=prompt_admission_id,
            trace_id=trace_id,
        )
    database.execute(
        """INSERT INTO conversation_heads (
               agent_id, backend_session_id, revision, replace_revision
           ) VALUES (?, ?, ?, 0)
           ON CONFLICT(agent_id, backend_session_id) DO UPDATE SET
               revision = MAX(conversation_heads.revision, excluded.revision)""",
        (agent_id, backend_session_id, revision),
    )
    return {
        "id": msg_id,
        "role": "user",
        "timestamp": timestamp,
        "text": text,
        "kind": None,
        "tool_name": None,
        "tools": [],
        "display_cells": [],
        "origin": origin,
        "sender_agent_id": sender_agent_id or "",
        "trace_id": (trace_id or "").strip(),
        "revision": revision,
        "created": True,
    }


MARKER_ORIGIN = origins.MARKER_ORIGIN


def marker_message_id(cause_message_id: str) -> str:
    return f"marker-{cause_message_id}"


def has_interruption_marker(cause_message_id: str) -> bool:
    if not cause_message_id:
        return False
    return conn().execute(
        "SELECT 1 FROM messages WHERE message_id = ? LIMIT 1",
        (marker_message_id(cause_message_id),),
    ).fetchone() is not None


def record_interruption_marker(*, agent_id: str, backend_session_id: str,
                               cause_message_id: str, text: str,
                               trace_id: str = "",
                               ) -> dict[str, Any] | None:
    """Write the visible "this turn was cut short" row under a user message.

    A turn the server killed (restart, crash) never writes an assistant row,
    so the user's message would sit unanswered with nothing to say why. The
    marker is an assistant-role row with origin ``system``: it renders as a
    normal reply, survives the automated-row filter, and is keyed by the
    causing message so a second boot cannot add a second one. Returns the new
    row, or None when the marker already exists.
    """
    if not agent_id or not cause_message_id:
        return None
    database = conn()
    msg_id = marker_message_id(cause_message_id)
    if database.execute(
            "SELECT 1 FROM messages WHERE message_id = ?", (msg_id,)).fetchone():
        return None
    row = database.execute(
        """SELECT COALESCE(MIN(seq), 0) - 1 AS next_seq
             FROM messages
            WHERE agent_id = ? AND backend_session_id = ?""",
        (agent_id, backend_session_id),
    ).fetchone()
    seq = min(int(row["next_seq"]), -1)
    cause = database.execute(
        f"""SELECT {_message_activity_sql()} AS ts_ms, updated_at
              FROM messages WHERE message_id = ?""",
        (cause_message_id,),
    ).fetchone()
    # Display order is timestamp then seq, and the marker's seq is below the
    # user row's, so its timestamp must be strictly later to sit under it.
    timestamp_ms = now_ms()
    if cause is not None and cause["ts_ms"] is not None:
        # SQLite's julianday conversion can round a millisecond timestamp down
        # by one. Include the durable write clock so a fast restart cannot give
        # the marker the same display timestamp and sort it above its user row.
        timestamp_ms = max(
            timestamp_ms,
            int(cause["ts_ms"]) + 1,
            int(cause["updated_at"] or 0) + 1,
        )
    timestamp = _iso_from_ms(timestamp_ms)
    revision = _next_revision(database)
    inserted = database.execute(
        """INSERT INTO messages (
               message_id, agent_id, backend_session_id, source_file, seq,
               role, timestamp, text, kind, tool_name, tools_json,
               display_cells_json, updated_at, revision, origin,
               sender_agent_id, trace_id
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(message_id) DO NOTHING""",
        (
            msg_id, agent_id, backend_session_id, f"marker:{cause_message_id}",
            seq, "assistant", timestamp, text, None, None, "[]", "[]",
            timestamp_ms, revision, MARKER_ORIGIN, None, trace_id or None,
        ),
    )
    if inserted.rowcount != 1:
        return None
    database.execute(
        """INSERT INTO conversation_heads (
               agent_id, backend_session_id, revision, replace_revision
           ) VALUES (?, ?, ?, 0)
           ON CONFLICT(agent_id, backend_session_id) DO UPDATE SET
               revision = MAX(conversation_heads.revision, excluded.revision)""",
        (agent_id, backend_session_id, revision),
    )
    return {
        "id": msg_id, "role": "assistant", "timestamp": timestamp,
        "text": text, "kind": None, "tool_name": None, "tools": [],
        "display_cells": [], "origin": MARKER_ORIGIN, "sender_agent_id": "",
        "trace_id": trace_id or "", "revision": revision, "created": True,
    }


def record_call_notice(*, agent_id: str, backend_session_id: str,
                       notice_id: str, text: str) -> dict[str, Any] | None:
    """A quiet group-call notice in an agent's chat (docs/group-calls.md).

    An assistant-role row with origin ``system``, like the interruption
    marker: it renders in the chat, never pushes or marks unread, and is never
    spoken. Keyed by ``notice_id`` so a retried change cannot post it twice.
    """
    if not agent_id or not backend_session_id or not notice_id or not str(text or "").strip():
        return None
    database = conn()
    msg_id = f"call:{notice_id}"
    row = database.execute(
        """SELECT COALESCE(MIN(seq), 0) - 1 AS next_seq
             FROM messages
            WHERE agent_id = ? AND backend_session_id = ?""",
        (agent_id, backend_session_id),
    ).fetchone()
    seq = min(int(row["next_seq"]), -1)
    timestamp_ms = now_ms()
    timestamp = _iso_from_ms(timestamp_ms)
    revision = _next_revision(database)
    inserted = database.execute(
        """INSERT INTO messages (
               message_id, agent_id, backend_session_id, source_file, seq,
               role, timestamp, text, kind, tool_name, tools_json,
               display_cells_json, updated_at, revision, origin,
               sender_agent_id
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(message_id) DO NOTHING""",
        (
            msg_id, agent_id, backend_session_id, msg_id,
            seq, "assistant", timestamp, str(text), None, None, "[]", "[]",
            timestamp_ms, revision, MARKER_ORIGIN, None,
        ),
    )
    if inserted.rowcount != 1:
        return None
    database.execute(
        """INSERT INTO conversation_heads (
               agent_id, backend_session_id, revision, replace_revision
           ) VALUES (?, ?, ?, 0)
           ON CONFLICT(agent_id, backend_session_id) DO UPDATE SET
               revision = MAX(conversation_heads.revision, excluded.revision)""",
        (agent_id, backend_session_id, revision),
    )
    return {"id": msg_id, "role": "assistant", "timestamp": timestamp,
            "text": text, "origin": MARKER_ORIGIN, "revision": revision}


def record_dream_digest(*, agent_id: str, backend_session_id: str,
                        run_id: str, text: str) -> dict[str, Any] | None:
    """Put a finished Dream Digest into the conversation.

    Dreams run in an isolated backend session that is deliberately never
    written to the chat read model, which meant a completed digest landed in
    the dream ledger and nowhere the user would ever look. This is the one
    row a night is allowed to add: keyed by run id, so a retried import or a
    second completion cannot post it twice.
    """
    if not agent_id or not run_id or not str(text or "").strip():
        return None
    database = conn()
    msg_id = f"dream:{run_id}"
    if database.execute(
            "SELECT 1 FROM messages WHERE message_id = ?", (msg_id,)).fetchone():
        return None
    row = database.execute(
        """SELECT COALESCE(MIN(seq), 0) - 1 AS next_seq
             FROM messages
            WHERE agent_id = ? AND backend_session_id = ?""",
        (agent_id, backend_session_id),
    ).fetchone()
    # Below the transcript's numbering, like the interruption marker. seq is
    # unique per (agent, session) and the rebuild assigns its own values from
    # the transcript, so a server-authored row sitting inside that range
    # collides the moment the agent takes its next turn. Display order is
    # timestamp first, so a negative seq costs nothing: the digest still lands
    # at the end of the conversation by its own clock.
    seq = min(int(row["next_seq"]), -1)
    timestamp_ms = now_ms()
    timestamp = _iso_from_ms(timestamp_ms)
    revision = _next_revision(database)
    inserted = database.execute(
        """INSERT INTO messages (
               message_id, agent_id, backend_session_id, source_file, seq,
               role, timestamp, text, kind, tool_name, tools_json,
               display_cells_json, updated_at, revision, origin,
               sender_agent_id
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(message_id) DO NOTHING""",
        (
            msg_id, agent_id, backend_session_id, f"dream:{run_id}",
            seq, "assistant", timestamp, str(text), None, None, "[]", "[]",
            timestamp_ms, revision, "dreaming", None,
        ),
    )
    if inserted.rowcount != 1:
        return None
    database.execute(
        """INSERT INTO conversation_heads (
               agent_id, backend_session_id, revision, replace_revision
           ) VALUES (?, ?, ?, 0)
           ON CONFLICT(agent_id, backend_session_id) DO UPDATE SET
               revision = MAX(conversation_heads.revision, excluded.revision)""",
        (agent_id, backend_session_id, revision),
    )
    return {
        "id": msg_id, "role": "assistant", "timestamp": timestamp,
        "text": text, "origin": "dreaming", "revision": revision,
    }


def carry_conversation(*, agent_id: str, from_session_id: str,
                       to_session_id: str, current_trace_id: str = "") -> int:
    """Move one conversation's rows onto the native conversation that
    continues it, so the chat keeps its history.

    agy prunes old conversations; Clarp then continues in a new one that opens
    with its own record of the old one. The read model shows a single native
    conversation, so without this the history would vanish from the app.
    Carried rows sit below the new conversation's numbering and leave the
    ``client:``/``final:`` bands, so the new transcript's import neither
    deletes nor adopts them; the current turn's own user row stays a client
    row so that import links it as usual. Returns the number of rows moved.
    """
    if not agent_id or not from_session_id or not to_session_id \
            or from_session_id == to_session_id:
        return 0
    database = conn()
    database.execute("BEGIN IMMEDIATE")
    try:
        rows = database.execute(
            """SELECT message_id, source_file, trace_id, timestamp, seq,
                      updated_at, revision FROM messages
                WHERE agent_id = ? AND backend_session_id = ?""",
            (agent_id, from_session_id)).fetchall()
        floor = database.execute(
            """SELECT COALESCE(MIN(seq), 0) FROM messages
                WHERE agent_id = ? AND backend_session_id = ?""",
            (agent_id, to_session_id)).fetchone()[0]
        # Display order (timestamp, seq) is kept by the new seqs, all below
        # the new conversation's own; "the latest user row" (a reply's
        # origin, the done push, interruption markers) is read by updated_at
        # then revision, so each row keeps its updated_at and the fresh
        # revisions follow the old ones' order.
        displayed = sorted(rows, key=lambda r: (r["timestamp"] or "", r["seq"]))
        seqs = {row["message_id"]: min(int(floor), 0) - len(rows) + index
                for index, row in enumerate(displayed)}
        revision = 0
        for row in sorted(rows, key=lambda r: (r["updated_at"], r["revision"])):
            revision = _next_revision(database)
            source = str(row["source_file"] or "")
            if not (current_trace_id and row["trace_id"] == current_trace_id
                    and source.startswith("client:")):
                source = "carried:" + source
            database.execute(
                """UPDATE messages SET backend_session_id = ?, seq = ?,
                          source_file = ?, revision = ?
                    WHERE message_id = ?""",
                (to_session_id, seqs[row["message_id"]], source, revision,
                 row["message_id"]))
        if revision:
            database.execute(
                """INSERT INTO conversation_heads (
                       agent_id, backend_session_id, revision, replace_revision
                   ) VALUES (?, ?, ?, 0)
                   ON CONFLICT(agent_id, backend_session_id) DO UPDATE SET
                       revision = MAX(conversation_heads.revision, excluded.revision)""",
                (agent_id, to_session_id, revision))
        database.execute("COMMIT")
    except BaseException:
        database.execute("ROLLBACK")
        raise
    return len(rows)


# A whole-file import used to hold the write lock for its entire run: a 19 MB
# Codex transcript took 6-7 s, every other writer hit the 5 s busy timeout,
# and a /send died mid-launch (2026-09-20). Rows are keyed by position and
# idempotent, so the import commits every few hundred turns instead.
IMPORT_COMMIT_EVERY = 200
IMPORT_WRITE_BUDGET_SECONDS = 0.025
IMPORT_WRITER_YIELD_SECONDS = 0.010
# RM-N7: at least once per second of held lock, yield longer than SQLite's
# busy handler's longest sleep (100 ms), so a writer waiting in it, in any
# process and on any connection, wakes inside a gap.
IMPORT_FAIR_YIELD_EVERY_SECONDS = 1.0
IMPORT_FAIR_YIELD_SECONDS = 0.110

def store_transcript_turns(*, agent_id: str, backend_session_id: str,
                           source_file: str, turns: list[dict[str, Any]],
                           capture_at: int | None = None,
                           captured: dict[str, Any] | None = None,
                           on_stored=None,
                           out_seqs: list[int] | None = None,
                           checkpoint_status: dict | None = None,
                           recertify_checkpoint: Callable[[], bool] | None = None,
                           carry_session_rows: bool = False,
                           ) -> list[dict[str, Any]]:
    """Full materialization; only opted native checkpoints may recertify.

    Recertification never rewrites products or replays effects. Its callback
    validates source bytes outside the writer after the products commit.
    All ordinary/legacy/shadow callers retain the original single-pass path.
    """
    visible_replies = _visible_replies(turns)
    database = _facade().conn()
    phases = _row_phases(turns)
    binding = (_checkpoint_db_binding(database)
               if recertify_checkpoint is not None and on_stored is not None else None)
    checkpoint_published = checkpoint_failed = False
    if recertify_checkpoint is not None and on_stored is not None:
        original_publish = on_stored

        def watched_publish(current_database, carry):
            nonlocal checkpoint_published, checkpoint_failed
            try:
                result = original_publish(current_database, carry)
            except BaseException:
                checkpoint_failed = True
                raise
            checkpoint_published = result is True
            return result

        on_stored = watched_publish

    def recover(previous_writer=None):
        if (recertify_checkpoint is None or on_stored is None
                or checkpoint_published or checkpoint_failed
                or (previous_writer is not None
                    and not previous_writer._publication_refusals <= {"foreign_commit_yield"})):
            return
        if _recertify_full_checkpoint(
                database, binding, agent_id=agent_id, backend_session_id=backend_session_id,
                source_file=source_file, turns=turns, capture_at=capture_at, on_stored=on_stored,
                prepare=recertify_checkpoint, phases=phases, visible_replies=visible_replies,
                session_rows=carry_session_rows):
            if checkpoint_status is not None:
                checkpoint_status.pop("refused", None)

    writer = _ImportWriter(database, locked=False)
    try:
        prelude = _prepare_import_prelude(
            writer, agent_id, backend_session_id, turns, phases=phases)
        out = _store_transcript_turns_txn(
            writer, agent_id=agent_id, backend_session_id=backend_session_id,
            source_file=source_file, turns=turns,
            capture_at=capture_at, captured=captured, out_seqs=out_seqs,
            visible_replies=visible_replies, prelude=prelude.for_pass())
        if on_stored is not None and writer.checkpoint_allowed(checkpoint_status):
            _write_checkpoint(writer, database, on_stored, captured)
        if database.in_transaction:
            database.execute("COMMIT")
        recover()
        return out
    except _WriteNeeded:
        if database.in_transaction:
            database.execute("ROLLBACK")
    except _CheckpointCleanupUncertain:
        raise  # never turn cleanup uncertainty into a whole-import SQL retry
    except Exception:
        if database.in_transaction:
            database.execute("ROLLBACK")
        raise
    prepared = prelude.for_pass()
    writer = _ImportWriter(database, locked=True)
    try:
        prepared = _revalidate_import_prelude(writer, agent_id, backend_session_id, prepared)
        out = _store_transcript_turns_txn(
            writer, agent_id=agent_id, backend_session_id=backend_session_id,
            source_file=source_file, turns=turns,
            capture_at=capture_at, captured=captured, out_seqs=out_seqs,
            visible_replies=visible_replies, prelude=prepared)
        if on_stored is not None and writer.checkpoint_allowed(checkpoint_status):
            _write_checkpoint(writer, database, on_stored, captured)
        writer.finish()
        recover(writer)
        return out
    except _CheckpointCleanupUncertain:
        raise  # never turn cleanup uncertainty into a whole-import SQL retry
    except Exception:
        if database.in_transaction:
            database.execute("ROLLBACK")
        raise


class _CheckpointCleanupUncertain(RuntimeError):
    """Non-retryable cleanup uncertainty, with both original errors retained."""
    def __init__(self, primary, cleanup):
        super().__init__("checkpoint recertification cleanup uncertain")
        self.primary_error = primary
        self.cleanup_error = cleanup


def _checkpoint_db_binding(database):
    """Physical/connection continuity for this import, not a durable DB epoch."""
    import pathlib
    try:
        if _facade().conn() is not database:
            return None
        rows = database.execute("PRAGMA database_list").fetchall()
        name = next((row[2] for row in rows if row[1] == "main"), "")
        if not name:
            return None
        path = pathlib.Path(name).resolve()
        stat = path.stat()
        return (str(path), stat.st_dev, stat.st_ino)
    except Exception as exc:  # noqa: BLE001 - only checkpoint admission is refused
        from .log import log_exception
        log_exception("transcriptCheckpointBindingSkip", exc)
        return None


def _recertify_full_checkpoint(database, binding, *, agent_id, backend_session_id,
                               source_file, turns, capture_at, on_stored,
                               prepare, phases, visible_replies, session_rows=False):
    """Fresh read-only carry, then exact five-input check in a new writer.

    Previous products are already committed. Never run a second row-writing
    pass here, and never certify the old carry from a final raw reread.
    """
    if (binding is None or database.in_transaction
            or _checkpoint_db_binding(database) != binding):
        return False
    fresh = {}
    primary = None
    probe = _ImportWriter(database, locked=False)
    try:
        prelude = _prepare_import_prelude(
            probe, agent_id, backend_session_id, turns,
            phases=phases, guard_carry=True, session_rows=session_rows)
        _store_transcript_turns_txn(
            probe, agent_id=agent_id, backend_session_id=backend_session_id,
            source_file=source_file, turns=turns,
            capture_at=capture_at, captured=fresh,
            visible_replies=visible_replies, prelude=prelude.for_pass())
        expected = prelude.raw_carry_inputs()
        if not fresh or _checkpoint_db_binding(database) != binding:
            return False
        database.execute("COMMIT")  # end read snapshot BEFORE writer admission
        if not prepare():  # source hashing/IO occurs OUTSIDE the writer
            return False
        writer = _ImportWriter(database, locked=True)
        writer.execute("SELECT 1")
        if _checkpoint_db_binding(database) != binding:
            return False
        current = _prepare_import_prelude(
            writer, agent_id, backend_session_id, [], raw_only=True, session_rows=session_rows)
        if current != expected:
            return False
        published = False

        def publish(current_database, carried):
            nonlocal published
            published = on_stored(current_database, carried) is True

        _write_checkpoint(writer, database, publish, fresh)
        writer.finish()
        return published
    except _WriteNeeded as exc:
        primary = exc
        return False  # no locked retry/effects/changed products in recertification
    except Exception as exc:  # noqa: BLE001 - already committed products stand
        primary = exc
        try:
            from .log import log_exception
            log_exception("transcriptCheckpointRecertifySkip", exc)
        except BaseException as diagnostic:
            primary = diagnostic  # this operation's escaping diagnostic, not caller state
            raise
        return False
    except BaseException as exc:
        primary = exc
        raise
    finally:
        try:
            if database.in_transaction:
                database.execute("ROLLBACK")
            if database.in_transaction:
                raise RuntimeError("recertification rollback did not end its transaction")
        except BaseException as cleanup:
            # Never demote a pending non-Exception control into an ordinary
            # catchable error. Preserve its exact object and the other error.
            if primary is not None and not isinstance(primary, Exception):
                raise primary from cleanup
            if not isinstance(cleanup, Exception):
                raise cleanup from primary
            # SQLITE_BUSY must not authorize replay of committed products.
            raise _CheckpointCleanupUncertain(primary, cleanup) from cleanup


def _visible_replies(turns: list[dict[str, Any]]) -> list[str]:
    """Each turn's assistant text with voice markup stripped ('' otherwise)."""
    return [_strip_voice_markup(turn.get("text")) if turn.get("role") == "assistant" else ""
            for turn in turns]


def _write_checkpoint(writer, database, on_stored, captured) -> None:
    """Run ``on_stored`` in the import's final transaction, behind a savepoint.

    The locked pass writes it into its last open batch (or a batch of its
    own when the rows are already committed). The probe pass found nothing
    to write and holds one read snapshot; the checkpoint upgrades that
    snapshot to a write, so it describes exactly the rows the probe read.
    Another writer having committed since makes SQLite refuse the upgrade;
    the checkpoint is then skipped, never retried against newer rows.
    """
    if writer.locked:
        writer.execute("SAVEPOINT transcript_checkpoint")
    elif database.in_transaction:
        database.execute("SAVEPOINT transcript_checkpoint")
    else:
        return  # a probe that read nothing has no snapshot to describe
    try:
        on_stored(database, captured if captured is not None else {})
    except Exception as exc:  # noqa: BLE001 - logged; the import itself stands
        database.execute("ROLLBACK TO transcript_checkpoint")
        database.execute("RELEASE transcript_checkpoint")
        from .log import log_exception
        log_exception("transcriptCheckpointWriteSkip", exc)
        return
    database.execute("RELEASE transcript_checkpoint")


def store_transcript_tail(*, agent_id: str, backend_session_id: str,
                          source_file: str, turns: list[dict[str, Any]],
                          start_seq: int, carry: dict[str, Any],
                          capture_at: int | None = None,
                          captured: dict[str, Any] | None = None,
                          on_stored=None,
                          expected_prelude=None,
                          carry_rules=frozenset(),
                          carry_witness: dict | None = None,
                          checkpoint_status: dict | None = None,
                          ) -> list[dict[str, Any]]:
    """Apply a certified tail through the full import's admission protocol.

    Text/phase work stays outside the writer. One probe snapshot prepares
    database inputs, which are exactly revalidated under BEGIN IMMEDIATE.
    The optional durable checkpoint shares the final row transaction and
    the full import's savepoint failure handling.

    ``expected_prelude`` is the record of the rows the frontier was
    published with (lib.message_carry.record; raw rows are recorded here).
    The tail is refused (``_TailInvalidated``, its ``what`` naming the rows)
    when a row the turns before ``start_seq`` could read has changed since;
    ``carry_rules`` name what they cannot read. ``carry_witness`` receives
    what the next record can build on (lib.message_carry.record).
    """
    visible_replies = _visible_replies(turns)
    phases = _row_phases(turns)
    database = _facade().conn()
    writer = _ImportWriter(database, locked=False)

    judged = None

    def guard(prepared) -> None:
        nonlocal judged
        if expected_prelude is None:
            return
        rows = prepared.raw_carry_inputs()
        if judged is not None and rows == judged:
            return      # the rows the probe read and found standing, to the last value
        what = prepared.carry_change(
            expected_prelude, rules=carry_rules, carry=carry, boundary_seq=start_seq,
            source_file=source_file, backend_session_id=backend_session_id,
            witness=carry_witness)
        if what:
            refusal = _TailInvalidated("tail_carry_inputs_changed")
            refusal.what = what
            raise refusal
        judged = rows

    try:
        prelude = _prepare_import_prelude(
            writer, agent_id, backend_session_id, turns, phases=phases,
            guard_carry=expected_prelude is not None,
            session_rows=message_carry.SESSION_ROWS in carry_rules)
        guard(prelude)
        out = _store_transcript_turns_txn(
            writer, agent_id=agent_id, backend_session_id=backend_session_id,
            source_file=source_file, turns=turns, start_seq=start_seq, carry=carry,
            capture_at=capture_at, captured=captured,
            visible_replies=visible_replies, prelude=prelude.for_pass())
        if on_stored is not None and writer.checkpoint_allowed(checkpoint_status):
            _write_checkpoint(writer, database, on_stored, captured)
        if database.in_transaction:
            database.execute("COMMIT")
        return out
    except _WriteNeeded:
        if database.in_transaction:
            database.execute("ROLLBACK")
    except Exception:
        if database.in_transaction:
            database.execute("ROLLBACK")
        raise
    prepared = prelude.for_pass()
    writer = _ImportWriter(database, locked=True)
    try:
        prepared = _revalidate_import_prelude(writer, agent_id, backend_session_id, prepared)
        guard(prepared)
        out = _store_transcript_turns_txn(
            writer, agent_id=agent_id, backend_session_id=backend_session_id,
            source_file=source_file, turns=turns, start_seq=start_seq, carry=carry,
            capture_at=capture_at, captured=captured,
            visible_replies=visible_replies, prelude=prepared)
        if on_stored is not None and writer.checkpoint_allowed(checkpoint_status):
            _write_checkpoint(writer, database, on_stored, captured)
        writer.finish()
        return out
    except Exception:
        if database.in_transaction:
            database.execute("ROLLBACK")
        raise


def plan_transcript_tail(*, agent_id: str, backend_session_id: str,
                         source_file: str, turns: list[dict[str, Any]],
                         start_seq: int, carry: dict[str, Any],
                         ) -> dict[str, Any]:
    """What ``store_transcript_tail`` would write, computed without writing.

    Returns the rows it would hold (``out``), the ids it would delete or
    update, the clock it started from and the carried state at its end."""
    visible_replies = _visible_replies(turns)
    database = _facade().conn()
    writer = _PlanWriter(database)
    end_state: dict[str, Any] = {}
    try:
        out = _store_transcript_turns_txn(
            writer, agent_id=agent_id, backend_session_id=backend_session_id,
            source_file=source_file, turns=turns,
            start_seq=start_seq, carry=carry,
            capture_at=start_seq + len(turns), captured=end_state,
            visible_replies=visible_replies)
        return {"out": out, "deleted": writer.deleted, "updated": writer.updated,
                "revision_floor": writer.revision_floor, "end_state": end_state}
    finally:
        if database.in_transaction:
            database.execute("ROLLBACK")


@dataclass(frozen=True)
class _ImportPrelude:
    turn_index: Any
    phases: list[str | None]
    client_user_provenance: dict[str, list[tuple[str, str, str, str]]]
    user_provenance: tuple[str, str, str]
    client_rows: list
    carry_rows: list | None = None
    oracle_rows: list | None = None
    # Whether carry_rows are this session's message rows only (lib.message_carry).
    session_rows: bool = False

    def raw_carry_inputs(self) -> tuple:
        return (self.turn_index._rows, self.client_rows, self.user_provenance,
                self.carry_rows, self.oracle_rows)

    def carry_change(self, previous, *, rules=frozenset(), carry=None, boundary_seq=0,
                     source_file="", backend_session_id="", witness=None) -> str:
        """'' when the rows ``previous`` recorded still stand for the turns
        before the boundary, else what changed (lib.message_carry.changed).
        Every column of every row is compared, through the record's digests;
        revisions/counts alone cannot certify carry."""
        where = dict(carry=carry, source_file=source_file,
                     backend_session_id=backend_session_id, session_rows=self.session_rows)
        if isinstance(previous, _ImportPrelude):
            previous = previous.raw_carry_inputs()
        if not isinstance(previous, dict):
            previous = message_carry.record(previous, **where)
        return message_carry.changed(previous, self.raw_carry_inputs(), rules=rules,
                                     boundary_seq=boundary_seq, witness=witness, **where)

    def for_pass(self) -> _ImportPrelude:
        return _ImportPrelude(
            self.turn_index, self.phases,
            {key: list(rows) for key, rows in self.client_user_provenance.items()},
            self.user_provenance, self.client_rows, self.carry_rows, self.oracle_rows,
            self.session_rows)


def _carry_message_rows(database, agent_id, backend_session_id, session_rows):
    """The message rows a frontier is published with: every row of the agent,
    or with ``session_rows`` this session's (lib.message_carry.SESSION_ROWS)."""
    if session_rows:
        return database.execute(
            "SELECT * FROM messages WHERE agent_id=? AND backend_session_id=? ORDER BY message_id",
            (agent_id, backend_session_id)).fetchall()
    return database.execute("SELECT * FROM messages WHERE agent_id=? ORDER BY message_id",
                            (agent_id,)).fetchall()


def _prepare_import_prelude(database, agent_id, backend_session_id, turns,
                            *, phases=None, guard_carry=False, raw_only=False,
                            session_rows=False):
    from . import message_turns
    if raw_only:
        # A checkpoint callback captures only ordered raw inputs in its final
        # transaction. No normalization/index/phase work moves under the writer.
        return (
            message_turns.TurnIndex.read_rows(database, agent_id),
            _client_provenance_rows(database, agent_id, backend_session_id),
            _latest_user_provenance(database, agent_id, backend_session_id),
            _carry_message_rows(database, agent_id, backend_session_id, session_rows),
            database.execute("SELECT * FROM oracle_delegations WHERE agent_id=? ORDER BY delegation_id",
                             (agent_id,)).fetchall())
    turn_index = message_turns.TurnIndex(database, agent_id)
    phases = _row_phases(turns) if phases is None else phases
    rows = _client_provenance_rows(database, agent_id, backend_session_id)
    carry_rows = _carry_message_rows(
        database, agent_id, backend_session_id, session_rows) if guard_carry else None
    oracle_rows = database.execute(
        "SELECT * FROM oracle_delegations WHERE agent_id=? ORDER BY delegation_id", (agent_id,)
    ).fetchall() if guard_carry else None
    return _ImportPrelude(turn_index, phases, _client_provenance(rows),
                          _latest_user_provenance(database, agent_id, backend_session_id),
                          rows, carry_rows, oracle_rows, session_rows)


def _revalidate_import_prelude(database, agent_id, backend_session_id,
                              prelude: _ImportPrelude) -> _ImportPrelude:
    from . import message_turns
    # Exact ordered raw-row comparisons cover inserts, edits, deletes, changes
    # of membership and ordering. A count/MAX fingerprint cannot certify that.
    # No normalization or index rebuilding is done unless that input changed.
    turn_rows = message_turns.TurnIndex.read_rows(database, agent_id)
    client_rows = _client_provenance_rows(database, agent_id, backend_session_id)
    user = _latest_user_provenance(database, agent_id, backend_session_id)
    turn_index = prelude.turn_index
    if turn_rows != turn_index._rows:
        turn_index = message_turns.TurnIndex.from_rows(turn_rows)
    provenance = prelude.client_user_provenance
    if client_rows != prelude.client_rows:
        provenance = _client_provenance(client_rows)
    carry_rows = _carry_message_rows(
        database, agent_id, backend_session_id, prelude.session_rows
    ) if prelude.carry_rows is not None else None
    oracle_rows = database.execute(
        "SELECT * FROM oracle_delegations WHERE agent_id=? ORDER BY delegation_id", (agent_id,)
    ).fetchall() if prelude.carry_rows is not None else None
    return _ImportPrelude(turn_index, prelude.phases, provenance, user, client_rows,
                          carry_rows, oracle_rows, prelude.session_rows)


def _store_transcript_turns_txn(database, *, agent_id: str,
                                backend_session_id: str, source_file: str,
                                turns: list[dict[str, Any]],
                                start_seq: int = 0,
                                carry: dict[str, Any] | None = None,
                                capture_at: int | None = None,
                                captured: dict[str, Any] | None = None,
                                out_seqs: list[int] | None = None,
                                visible_replies: list[str] | None = None,
                                prelude: _ImportPrelude | None = None,
                                ) -> list[dict[str, Any]]:
    """Import ``turns`` as transcript rows ``start_seq`` onward.

    A full import passes every turn (``start_seq`` 0, no ``carry``). The
    incremental import passes the turns from a checkpointed turn on and the
    state this loop carried when it reached that turn (``carry``, as
    ``captured`` recorded it at ``capture_at``); the transcript then holds
    ``start_seq + len(turns)`` turns in all.
    """
    out: list[dict[str, Any]] = []
    if out_seqs is not None:
        out_seqs.clear()
    latest_revision = 0
    prelude = prelude or _prepare_import_prelude(database, agent_id, backend_session_id, turns)
    turn_index, phases = prelude.turn_index, prelude.phases
    touched_traces: set[str] = set()
    newest_trace = ""
    provenance = _ClientProvenance(database, agent_id, backend_session_id,
                                   prepared=prelude.client_user_provenance)
    if carry is None:
        latest_user_key, current_origin, current_sender_agent_id = prelude.user_provenance
        # Incremental transcript reads may begin with assistant-only deltas. Carry
        # the durable heartbeat user key across those reads so every partial update
        # is still accounted once per heartbeat turn, never once per delta.
        current_heartbeat_key = (
            latest_user_key if current_origin == "heartbeat" else ""
        )
    else:
        provenance.resume(carry["consumed_client_ids"])
        current_origin = carry["current_origin"]
        current_sender_agent_id = carry["current_sender_agent_id"]
        current_heartbeat_key = carry["current_heartbeat_key"]
    agy = _AgyAuthority()
    # The request whose client row was matched last and has had no answer yet.
    # When an attempt fails, the dispatcher sends the same prompt again and
    # the backend records one more copy of it per retry.
    unanswered_key = ""
    current_request_trace = ""
    adopted_final_ids: set[str] = set()
    skipped_slot_removed = False
    final_rows = _FinalRows(database, agent_id, backend_session_id)
    # Rows this import already wrote: never moved again (RM-N1).
    placed_ids: set[str] = set()
    # For the carried-inputs guard (lib.message_carry), recorded with the
    # carry: the latest time a turn so far carries (None: not known, the
    # carry this import started from did not record it), and whether a reply
    # came before any user turn and so read the conversation's latest user row.
    latest_ms: int | None = 0
    user_turn_seen = initial_provenance_used = False
    if carry is not None:
        agy.resume(carry)
        unanswered_key = carry["unanswered_key"]
        current_request_trace = carry["current_request_trace"]
        adopted_final_ids = set(carry["adopted_final_ids"])
        newest_trace = carry["newest_trace"]
        latest_ms = carry.get("prefix_latest_ms")
        user_turn_seen = True
        initial_provenance_used = bool(carry.get("initial_provenance_used", True))

    def capture() -> None:
        """The carried state as the loop stands now (lib.transcript_incremental)."""
        captured.clear()
        captured.update(json.loads(json.dumps({
            "consumed_client_ids": provenance.consumed,
            "current_origin": current_origin,
            "current_sender_agent_id": current_sender_agent_id,
            "current_heartbeat_key": current_heartbeat_key,
            "assistant_ordinal": agy.ordinal,
            "authority_tools": agy.tools,
            "closed_authority": sorted(agy.closed),
            "unanswered_key": unanswered_key,
            "current_request_trace": current_request_trace,
            "adopted_final_ids": sorted(adopted_final_ids),
            "newest_trace": newest_trace,
            "prefix_latest_ms": latest_ms,
            "initial_provenance_used": initial_provenance_used,
        })))

    def commit_batch() -> None:
        nonlocal latest_revision
        # Publish the revision with each committed chunk, including
        # removals, so a later lock failure cannot hide durable changes.
        if skipped_slot_removed:
            latest_revision = max(latest_revision, _next_revision(database))
        database.execute(
            """INSERT INTO conversation_heads
                   (agent_id, backend_session_id, revision, replace_revision)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(agent_id, backend_session_id) DO UPDATE SET
                   revision = MAX(conversation_heads.revision, excluded.revision),
                   replace_revision = MAX(conversation_heads.replace_revision,
                                          excluded.replace_revision)""",
            (agent_id, backend_session_id, latest_revision,
             latest_revision if skipped_slot_removed else 0))
        database.commit()
        # SQLite does not promise fair immediate writer reacquisition.
        # Yield outside the transaction so admissions can take the writer.
        database.yield_writer()
        final_rows.invalidate()

    # Each assistant reply of this batch with the turn it answers, for the
    # live-row cleanup below; '' when no turn can be named.
    batch_finals: list[tuple[str, str]] = []
    for index, turn in enumerate(turns):
        seq = start_seq + index
        if captured is not None and seq == capture_at:
            capture()
        database.seq = seq
        if database.due():
            commit_batch()
        role = turn.get("role")
        turn_ms = iso_ms(turn.get("timestamp"))
        if turn_ms is not None and latest_ms is not None:
            latest_ms = max(latest_ms, turn_ms)
        if role == "assistant" and not user_turn_seen:
            initial_provenance_used = True
        reply_trace = ""
        if role == "assistant":
            reply_trace = current_request_trace or turn_index.trace_at(turn.get("timestamp"))
        if role == "user":
            agy.close_open()
        if role == "assistant" and not turn.get("failed"):
            unanswered_key = ""
        if role == "assistant":
            authority = agy.lookup(database, agent_id, backend_session_id)
            visible_reply = visible_replies[index] if visible_replies is not None \
                else _strip_voice_markup(turn.get("text"))
            if visible_reply:
                batch_finals.append(
                    ((authority["trace_id"] or "") if authority is not None else reply_trace,
                     visible_reply))
            if authority is not None:
                # Stream-json terminal authority owns this provider turn.
                # Its canonical final row (or empty/error tombstone) remains
                # authoritative across every later /log or watcher import.
                # Its text, that is: the tool calls the transcript records for
                # the turn are carried onto that row below.
                agy.collect(authority, turn)
                # Nothing is written at this position: whatever an earlier
                # import left here would outlive a shift (RM-N1).
                removed = _delete_slot(database, agent_id, backend_session_id,
                                       source_file, seq)
                skipped_slot_removed = skipped_slot_removed or removed > 0
                continue
        text = str(turn.get("text") or "")
        origin = "user"
        sender_agent_id = ""
        msg_id = str(turn.get("id") or _message_id(
            agent_id, backend_session_id, source_file, seq))
        if role == "user" and text.lstrip().startswith(CALL_OPENING_OPEN):
            # A listen-first initiative's input (docs/agent-rings.md): the
            # agent opens the call; it is never a user row.
            removed = _delete_slot(database, agent_id, backend_session_id, source_file, seq)
            skipped_slot_removed = skipped_slot_removed or removed > 0
            continue
        if role == "user":
            user_turn_seen = True      # every path below sets whose turn it is
            text = strip_injected_blocks(text)
            words = strip_delivery_context(text)
            if words != text and not provenance.candidates(_norm_text(text)):
                # A delivery notice in front of the words is the Host's,
                # unless the user's own row begins with it (they pasted it):
                # that row stands for this turn as it is.
                text = words
            if heartbeat.should_skip_heartbeat_prompt(text):
                current_heartbeat_key = msg_id
                current_origin, current_sender_agent_id = "heartbeat", ""
                origin, sender_agent_id = "heartbeat", ""
            elif team_leader.should_skip_leader_tick_prompt(text):
                current_heartbeat_key = ""
                current_origin, current_sender_agent_id = "leader_tick", ""
                origin, sender_agent_id = "leader_tick", ""
            elif dreaming.should_skip_dream_prompt(text):
                current_heartbeat_key = ""
                current_origin, current_sender_agent_id = "dreaming", ""
                origin, sender_agent_id = "dreaming", ""
            else:
                current_heartbeat_key = ""
        elif role == "assistant":
            text = strip_hidden_blocks(text)
            heartbeat_accounting = ""
            # Each token counts only in its own automation's turn (AUT-07).
            skip, text = origins.ack_for_origin(
                heartbeat.strip_heartbeat_ack(text), text, token=heartbeat.HEARTBEAT_OK,
                origin=current_origin, own="heartbeat")
            if skip:
                if current_origin == "heartbeat":
                    heartbeat_accounting = "noop"
                text = "Heartbeat check: no action needed."
            # The probe only transforms; the locked pass records, once.
            skip, text = dreaming.process_assistant_text(
                agent_id, text, live=not database.locked, origin=current_origin)
            if skip:
                text = "Dreaming check: no action needed."
            skip, text = origins.ack_for_origin(
                team_leader.strip_leader_noop(text), text, token=team_leader.LEADER_NOOP,
                origin=current_origin, own="leader_tick")
            if skip:
                if database.locked and current_origin == "leader_tick":
                    team_leader.record_leader_noop(agent_id)
                text = "Leader check: no action needed."
            if (
                current_origin == "heartbeat"
                and not heartbeat_accounting
                and not heartbeat.is_neutral_heartbeat_status(text)
            ):
                heartbeat_accounting = "activity"
        else:
            heartbeat_accounting = ""
        # A user turn already owned by a durable client row → don't import a
        # second copy; the client row stands in for it (matched by send order).
        if role == "user" and turn.get("id") is None:
            key = _norm_text(text)
            rows = provenance.candidates(key)
            if key and key == unanswered_key and _is_retry_echo(
                    turn.get("timestamp"), rows[0][3] if rows else None):
                # A retry's copy: the matched client row already stands for
                # it. Imported, it showed the message twice and later replies
                # lost their sender. Clear whatever an earlier import left here.
                removed = _delete_slot(database, agent_id, backend_session_id,
                                       source_file, seq)
                skipped_slot_removed = skipped_slot_removed or removed > 0
                continue
            if rows:
                running, began = turn_index.running_at(turn.get("timestamp"))
                client_msg_id, current_origin, current_sender_agent_id, _ = \
                    provenance.take(key, rows, _own_turns_row(database, rows, running, began))
                unanswered_key = key
                authored = database.execute("SELECT trace_id FROM messages WHERE message_id=?", (client_msg_id,)).fetchone()
                current_request_trace = (authored["trace_id"] or "") if authored else ""
                if running and current_request_trace not in turn_index.outcomes:
                    # A message steered into the running turn has a trace but
                    # no turn of its own: what follows it is that turn's reply.
                    current_request_trace = running
                current_heartbeat_key = (
                    client_msg_id if current_origin == "heartbeat" else ""
                )
                # Nothing is written at this position, so whatever an earlier
                # import left here would outlive it. When the parser starts
                # hiding a message every later turn shifts down, and the old
                # occupant of this slot stayed visible as a duplicate.
                removed = _delete_slot(database, agent_id, backend_session_id,
                                       source_file, seq)
                skipped_slot_removed = skipped_slot_removed or removed > 0
                continue
            current_origin, current_sender_agent_id = "user", ""
            current_heartbeat_key = ""
            unanswered_key = ""
        if role == "user":
            current_request_trace = str(turn.get("trace_id") or "")
            current_origin, current_sender_agent_id = origin, sender_agent_id
            current_heartbeat_key = msg_id if origin == "heartbeat" else ""
        elif role == "assistant":
            origin, sender_agent_id = current_origin, current_sender_agent_id
        if role == "assistant" and not turn.get("id"):
            msg_id, changed = _resolve_reply_id(
                database, agent_id=agent_id, backend_session_id=backend_session_id,
                source_file=source_file, seq=seq, msg_id=msg_id, text=text,
                timestamp=turn.get("timestamp"), request_trace=current_request_trace,
                reply_trace=reply_trace, final_rows=final_rows,
                adopted_final_ids=adopted_final_ids, placed_ids=placed_ids,
                # The prelude normalized raw text before BEGIN. Automation
                # and hidden-block processing above may replace that text;
                # those cases retain normalization of the effective value.
                # An empty prepared value is valid, not a missing value.
                visible=(visible_replies[index] if visible_replies is not None
                         and text == str(turn.get("text") or "") else None))
            skipped_slot_removed = skipped_slot_removed or changed
        row = _upsert_row(
            database, agent_id=agent_id, backend_session_id=backend_session_id,
            source_file=source_file, seq=seq, msg_id=msg_id, turn=turn, role=role,
            text=text, origin=origin, sender_agent_id=sender_agent_id,
            request_trace=current_request_trace,
            phase=phases[index] if role == "assistant" else None,
            turn_index=turn_index, final_rows=final_rows)
        timestamp, tools, display_cells = row.timestamp, row.tools, row.display_cells
        unchanged, revision = row.unchanged, row.revision
        skipped_slot_removed = skipped_slot_removed or row.changed
        newest_trace = row.row_trace or newest_trace
        if row.row_trace and not unchanged:
            # Only turns whose rows changed are re-stamped: an import re-reads
            # the whole transcript, and an untouched turn's summary stands.
            touched_traces.add(row.row_trace)
        latest_revision = max(latest_revision, revision)
        if role == "assistant" and not unchanged and database.locked:
            if heartbeat_accounting == "noop":
                heartbeat.record_heartbeat_noop_once(agent_id, current_heartbeat_key)
            elif heartbeat_accounting == "activity":
                heartbeat.record_heartbeat_activity_once(
                    agent_id, current_heartbeat_key)
        if role == "assistant" and database.locked:
            trace_id = str(turn.get("trace_id") or "")
            team_store.capture_assistant_message(
                agent_id=agent_id,
                source_message_id=msg_id,
                trace_id=trace_id,
                text=text,
            )
            from . import oracle_delegations
            oracle_delegations.complete_for_trace(
                trace_id=trace_id, message_id=msg_id, text=text)
        placed_ids.add(msg_id)
        out.append({
            **turn,
            "id": msg_id,
            "role": role,
            "timestamp": timestamp,
            "text": text,
            "tools": tools,
            "display_cells": display_cells,
            "origin": origin,
            "sender_agent_id": sender_agent_id,
            "revision": revision,
        })
        if out_seqs is not None:
            out_seqs.append(seq)
    total_turns = start_seq + len(turns)
    if captured is not None and capture_at == total_turns:
        capture()
    # The turns this import touched: their last row carries the summary.
    for trace in sorted(touched_traces | ({newest_trace} if newest_trace else set())):
        if database.due():
            commit_batch()
        latest_revision = max(latest_revision, stamp_turn(
            database, agent_id, trace, _next_revision))
    _finish_import(database, agent_id, backend_session_id,
                   source_file=source_file,
                   batch_finals=batch_finals, total_turns=total_turns,
                   skipped_slot_removed=skipped_slot_removed,
                   latest_revision=latest_revision, authority_tools=agy.tools)
    return out


# --- turn summaries (docs/live-items.md §9; helpers in lib.message_turns) ---

def _finish_import(database, agent_id: str, backend_session_id: str, *,
                   source_file: str,
                   batch_finals: list[tuple[str, str]], total_turns: int,
                   skipped_slot_removed: bool, latest_revision: int,
                   authority_tools: dict[str, tuple[str, list, list]]) -> None:
    """The import's work after its rows: leftover live twins, parked replies
    nothing re-adopted (RM-N1c), the stale tail, the replace revision,
    stream-owned agy tools and the conversation head."""
    # Compare markup-normalized so a streamed live row (with <speak>/<vox>/…)
    # still matches its durable copy (markup stripped); raw startswith missed it.
    live_replace_revision = 0
    # A leftover live row goes only when a reply of ITS OWN turn landed in
    # this batch; another turn's kept row waits for its own reply, and a reply
    # of unknown turn clears nothing.
    for live in database.execute(
        """SELECT message_id, text, trace_id FROM messages
            WHERE agent_id = ? AND backend_session_id = ?
              AND source_file LIKE 'live:%'
              AND role = 'assistant'""",
        (agent_id, backend_session_id),
    ).fetchall():
        live_text = _strip_voice_markup(live["text"])
        if not live_text:
            continue
        live_trace = live["trace_id"] or ""
        if any(trace and trace == live_trace and _related(final, live_text)
               for trace, final in batch_finals):
            live_replace_revision = max(live_replace_revision, _next_revision(database))
            latest_revision = max(latest_revision, live_replace_revision)
            database.execute(
                "DELETE FROM messages WHERE message_id = ?",
                (live["message_id"],),
            )
    if _restore_unadopted_parked(database, agent_id, backend_session_id, source_file):
        skipped_slot_removed = True
    stale = database.execute(
        """SELECT 1 FROM messages
            WHERE agent_id = ? AND backend_session_id = ? AND seq >= ?
              AND source_file NOT LIKE 'client:%'
              AND source_file NOT LIKE 'live:%'
              AND source_file NOT LIKE 'final:%'
            LIMIT 1""",
        (agent_id, backend_session_id, total_turns),
    ).fetchone()
    if stale is not None:
        database.execute(
            """DELETE FROM messages
                WHERE agent_id = ? AND backend_session_id = ? AND seq >= ?
                  AND source_file NOT LIKE 'client:%'
                  AND source_file NOT LIKE 'live:%'
                  AND source_file NOT LIKE 'final:%'""",
            (agent_id, backend_session_id, total_turns),
        )
    stale_replace_revision = (
        _next_revision(database)
        if stale is not None or skipped_slot_removed else 0)
    replace_revision = max(stale_replace_revision, live_replace_revision)
    latest_revision = max(latest_revision, replace_revision)
    latest_revision = max(latest_revision,
                          _attach_authority_tools(database, agent_id, authority_tools))
    head = database.execute(
        """SELECT revision, replace_revision FROM conversation_heads
            WHERE agent_id = ? AND backend_session_id = ?""",
        (agent_id, backend_session_id)).fetchone()
    if database.began_at is None and head is not None \
            and head["revision"] >= latest_revision \
            and head["replace_revision"] >= replace_revision:
        return  # nothing written, and the head already covers every row
    database.execute(
        """INSERT INTO conversation_heads (
               agent_id, backend_session_id, revision, replace_revision
           ) VALUES (?, ?, ?, ?)
           ON CONFLICT(agent_id, backend_session_id) DO UPDATE SET
               revision = MAX(conversation_heads.revision, excluded.revision),
               replace_revision = MAX(
                   conversation_heads.replace_revision,
                   excluded.replace_revision
               )""",
        (agent_id, backend_session_id, latest_revision, replace_revision),
    )


def _attach_authority_tools(database, agent_id: str,
                            collected: dict[str, tuple[str, list, list]]) -> int:
    """Put each stream-owned agy turn's transcript tool calls on its final row.

    agy's whole transcript is parsed on every import, so the calls collected
    here are the turn's complete set and replace what the row held. They are
    settled against how the turn ended (a call cut off by a stopped turn does
    not spin forever) and written under a new revision only when they change;
    the row's text stays the stream's."""
    from . import message_turns
    latest = 0
    for trace_id, (message_id, tools, cells) in collected.items():
        row = database.execute("SELECT tools_json, display_cells_json FROM messages "
                               "WHERE message_id=?", (message_id,)).fetchone()
        if row is None:
            continue
        turn = database.execute("SELECT outcome FROM turns WHERE agent_id=? AND trace_id=? "
                                "ORDER BY started_at DESC LIMIT 1", (agent_id, trace_id)).fetchone()
        outcome = turn["outcome"] if turn is not None else None
        tools_json = json.dumps(message_turns.settle_statuses(tools, outcome), separators=(",", ":"))
        cells_json = json.dumps(message_turns.settle_statuses(cells, outcome), separators=(",", ":"))
        if (row["tools_json"], row["display_cells_json"]) == (tools_json, cells_json):
            continue
        revision = _next_revision(database)
        database.execute("UPDATE messages SET tools_json=?, display_cells_json=?, "
                         "revision=? WHERE message_id=?",
                         (tools_json, cells_json, revision, message_id))
        latest = max(latest, revision)
    return latest


def turn_stamp(database, agent_id: str, trace_id: str):
    """What ``stamp_turn`` reads and decides for one turn, without writing:
    ``(turn, rows, summary)``. ``turn`` is its ``turns`` row or None, ``rows``
    its message rows across backend sessions in display order, ``summary`` the
    JSON its last row carries (None while the turn is unsettled or unknown).
    None when the turn has no rows."""
    from . import message_turns
    turn = database.execute(
        """SELECT started_at, settled_at, outcome FROM turns WHERE agent_id = ? AND trace_id = ?
            ORDER BY started_at DESC LIMIT 1""", (agent_id, trace_id)).fetchone()
    rows = database.execute(
        """SELECT message_id, backend_session_id, tools_json, display_cells_json, turn_json
             FROM messages WHERE agent_id = ? AND trace_id = ?
            ORDER BY COALESCE(timestamp, ''), seq""", (agent_id, trace_id)).fetchall()
    if not rows:
        return None
    summary = None
    if turn is not None and turn["settled_at"] is not None:
        started = int(turn["started_at"])
        ended = max(int(turn["settled_at"]), started)
        summary = json.dumps({"turn_id": trace_id, "status": turn["outcome"],
                              "started_at_ms": started, "ended_at_ms": ended,
                              "worked_ms": ended - started, "tool_count": message_turns.tool_count(rows)},
                             separators=(",", ":"))
    return turn, rows, summary


def stamp_turn(database, agent_id: str, trace_id: str, next_revision) -> int:
    from . import message_turns
    """Re-stamp one turn's rows (inside the caller's write transaction).
    Returns the highest revision written, 0 when nothing changed."""
    if not trace_id:
        return 0
    stamped = turn_stamp(database, agent_id, trace_id)
    if stamped is None:
        return 0
    turn, rows, summary = stamped
    outcome = turn["outcome"] if turn is not None else None
    highest = 0
    last = rows[-1]["message_id"]
    for row in rows:
        tools = message_turns.settle_statuses(json.loads(row["tools_json"] or "[]"), outcome)
        cells = message_turns.settle_statuses(json.loads(row["display_cells_json"] or "[]"), outcome)
        tools_json = json.dumps(tools, separators=(",", ":"))
        cells_json = json.dumps(cells, separators=(",", ":"))
        turn_json = summary if row["message_id"] == last else None
        if (tools_json, cells_json, turn_json) == (
                json.dumps(json.loads(row["tools_json"] or "[]"), separators=(",", ":")),
                json.dumps(json.loads(row["display_cells_json"] or "[]"), separators=(",", ":")),
                row["turn_json"]):
            continue
        revision = next_revision(database)
        database.execute(
            """UPDATE messages SET tools_json = ?, display_cells_json = ?, turn_json = ?,
                      revision = ?, updated_at = ? WHERE message_id = ?""",
            (tools_json, cells_json, turn_json, revision, now_ms(), row["message_id"]))
        database.execute(
            """INSERT INTO conversation_heads (agent_id, backend_session_id, revision, replace_revision)
               VALUES (?, ?, ?, 0)
               ON CONFLICT(agent_id, backend_session_id) DO UPDATE SET
                   revision = MAX(conversation_heads.revision, excluded.revision)""",
            (agent_id, row["backend_session_id"], revision))
        highest = max(highest, revision)
    return highest


def restamp_turn(agent_id: str, trace_id: str) -> None:
    """Stamp a turn's rows in a write transaction of its own (a turn settled)."""
    from .db import conn
    from .message_writes import _next_revision
    database = conn()
    own = not database.in_transaction
    if own:
        database.execute("BEGIN IMMEDIATE")
    try:
        stamp_turn(database, agent_id, trace_id, _next_revision)
        if own:
            database.execute("COMMIT")
    except Exception:
        if own and database.in_transaction:
            database.execute("ROLLBACK")
        raise
