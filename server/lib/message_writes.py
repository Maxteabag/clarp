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

import hashlib
import json
import time
from typing import Any

from .clock import iso_from_ms as _iso_from_ms, now_ms
from .db import conn
from . import dreaming, heartbeat, origins, team_leader, team_store
from .voice_markup import strip_hidden_blocks
from .message_context import _strip_voice_markup, strip_injected_context


def _facade():
    """The `message_store` facade module, looked up at call time so a
    monkeypatch on it reaches this code path."""
    from . import message_store
    return message_store


def _message_id(agent_id: str, backend_session_id: str, source_file: str,
                seq: int) -> str:
    raw = f"{agent_id}\0{backend_session_id}\0{seq}"
    return "msg-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


def _client_message_id(client_msg_id: str) -> str:
    """Message id for a user message the client authored. The client mints
    `client_msg_id` once per send and keys its bubble by it; the server stores
    the durable user row under the SAME id (verbatim) and returns it in /log,
    so client and server match by identity — never by fuzzy text/sequence
    reconciliation. Namespaced so it can't collide with transcript ids
    (which start with 'msg-')."""
    cid = client_msg_id.strip()
    return cid if cid.startswith("u-") else f"u-{cid}"


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
    """Hand an admitted-but-never-launched user row to a new attempt."""
    conn().execute(
        "UPDATE messages SET trace_id = ? WHERE message_id = ? AND role = 'user'",
        ((trace_id or "").strip() or None, _client_message_id(client_msg_id)),
    )


def has_client_message(client_msg_id: str) -> bool:
    if not client_msg_id.strip():
        return False
    return conn().execute(
        "SELECT 1 FROM messages WHERE message_id = ? AND role = 'user'",
        (_client_message_id(client_msg_id),),
    ).fetchone() is not None


def _norm_text(text: str | None) -> str:
    return (text or "").strip()


def _next_revision(database) -> int:
    row = database.execute(
        """UPDATE message_clock SET revision = revision + 1
            WHERE singleton = 0 RETURNING revision"""
    ).fetchone()
    return int(row["revision"])


def _row_payload(row) -> tuple:
    return (
        row["role"], row["timestamp"], row["text"], row["kind"],
        row["tool_name"], row["tools_json"], row["display_cells_json"],
        row["origin"] or "user", row["sender_agent_id"] or "",
    )


def _latest_user_provenance(database, agent_id: str,
                            backend_session_id: str) -> tuple[str, str, str]:
    row = database.execute(
        """SELECT message_id, origin, sender_agent_id
             FROM messages
            WHERE agent_id = ? AND backend_session_id = ? AND role = 'user'
            ORDER BY updated_at DESC, revision DESC, seq DESC
            LIMIT 1""",
        (agent_id, backend_session_id),
    ).fetchone()
    if row is None:
        return "", "user", ""
    return (
        row["message_id"], row["origin"] or "user",
        row["sender_agent_id"] or "",
    )


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
               sender_agent_id
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(message_id) DO NOTHING""",
        (
            msg_id, agent_id, backend_session_id, f"marker:{cause_message_id}",
            seq, "assistant", timestamp, text, None, None, "[]", "[]",
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
    return {
        "id": msg_id, "role": "assistant", "timestamp": timestamp,
        "text": text, "kind": None, "tool_name": None, "tools": [],
        "display_cells": [], "origin": MARKER_ORIGIN, "sender_agent_id": "",
        "revision": revision, "created": True,
    }


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


# A whole-file import used to hold the write lock for its entire run: a 19 MB
# Codex transcript took 6-7 s, every other writer hit the 5 s busy timeout,
# and a /send died mid-launch (2026-09-20). Rows are keyed by position and
# idempotent, so the import commits every few hundred turns instead.
IMPORT_COMMIT_EVERY = 200
IMPORT_WRITE_BUDGET_SECONDS = 0.025
IMPORT_WRITER_YIELD_SECONDS = 0.010


def store_transcript_turns(*, agent_id: str, backend_session_id: str,
                           source_file: str, turns: list[dict[str, Any]]
                           ) -> list[dict[str, Any]]:
    # Pure text normalization can dominate a large transcript import; do it
    # before taking SQLite's single writer lock, once per assistant message.
    final_assistant_texts = []
    for turn in turns:
        if turn.get("role") == "assistant":
            text = _strip_voice_markup(turn.get("text"))
            if text:
                final_assistant_texts.append(text)
    database = _facade().conn()
    database.execute("BEGIN IMMEDIATE")
    try:
        out = _store_transcript_turns_txn(
            database, agent_id=agent_id,
            backend_session_id=backend_session_id,
            source_file=source_file, turns=turns,
            final_assistant_texts=final_assistant_texts)
        database.execute("COMMIT")
        return out
    except Exception:
        if database.in_transaction:
            database.execute("ROLLBACK")
        raise


def _store_transcript_turns_txn(database, *, agent_id: str,
                                backend_session_id: str, source_file: str,
                                turns: list[dict[str, Any]],
                                final_assistant_texts: list[str]
                                ) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    latest_revision = 0
    # User turns the client already recorded durably (keyed by client_msg_id).
    # Claude's transcript carries its own copy of each; we link those back to
    # the client rows by text in send order rather than inserting a duplicate.
    client_user_provenance: dict[str, list[tuple[str, str, str]]] = {}
    for r in database.execute(
        """SELECT message_id, text, origin, sender_agent_id FROM messages
            WHERE agent_id = ? AND backend_session_id = ?
              AND source_file LIKE 'client:%'
            ORDER BY updated_at ASC, seq ASC""",
        (agent_id, backend_session_id),
    ).fetchall():
        key = _norm_text(r["text"])
        client_user_provenance.setdefault(key, []).append((
            r["message_id"],
            r["origin"] or "user",
            r["sender_agent_id"] or "",
        ))
    latest_user_key, current_origin, current_sender_agent_id = _latest_user_provenance(
        database, agent_id, backend_session_id)
    # Incremental transcript reads may begin with assistant-only deltas. Carry
    # the durable heartbeat user key across those reads so every partial update
    # is still accounted once per heartbeat turn, never once per delta.
    current_heartbeat_key = (
        latest_user_key if current_origin == "heartbeat" else ""
    )
    assistant_ordinal = 0
    current_request_trace = ""
    adopted_final_ids: set[str] = set()
    skipped_slot_removed = False
    batch_started = time.monotonic()
    for seq, turn in enumerate(turns):
        if seq and _facade().IMPORT_COMMIT_EVERY > 0 and (
                seq % _facade().IMPORT_COMMIT_EVERY == 0
                or time.monotonic() - batch_started >= _facade().IMPORT_WRITE_BUDGET_SECONDS):
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
            database.execute("COMMIT")
            # SQLite does not promise fair immediate writer reacquisition.
            # Yield outside the transaction so admissions can take the writer.
            time.sleep(_facade().IMPORT_WRITER_YIELD_SECONDS)
            database.execute("BEGIN IMMEDIATE")
            batch_started = time.monotonic()
        role = turn.get("role")
        if role == "assistant":
            authority = database.execute(
                """SELECT trace_id FROM agy_turn_authority
                     WHERE agent_id=? AND backend_session_id=?
                       AND assistant_start_ordinal<=?
                       AND (assistant_end_ordinal IS NULL
                            OR ?<assistant_end_ordinal)
                     ORDER BY updated_at DESC LIMIT 1""",
                (agent_id, backend_session_id, assistant_ordinal,
                 assistant_ordinal),).fetchone()
            assistant_ordinal += 1
            if authority is not None:
                # Stream-json terminal authority owns this provider turn.
                # Its canonical final row (or empty/error tombstone) remains
                # authoritative across every later /log or watcher import.
                continue
        text = str(turn.get("text") or "")
        origin = "user"
        sender_agent_id = ""
        msg_id = str(turn.get("id") or _message_id(
            agent_id, backend_session_id, source_file, seq))
        if role == "user":
            text = strip_injected_context(text)
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
            skip, text = heartbeat.strip_heartbeat_ack(text)
            if skip:
                if current_origin == "heartbeat":
                    heartbeat_accounting = "noop"
                text = "Heartbeat check: no action needed."
            skip, text = dreaming.process_assistant_text(agent_id, text)
            if skip:
                text = "Dreaming check: no action needed."
            skip, text = team_leader.strip_leader_noop(text)
            if skip:
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
            rows = client_user_provenance.get(key) or []
            if rows:
                client_msg_id, current_origin, current_sender_agent_id = rows.pop(0)
                authored = database.execute("SELECT trace_id FROM messages WHERE message_id=?", (client_msg_id,)).fetchone()
                current_request_trace = (authored["trace_id"] or "") if authored else ""
                current_heartbeat_key = (
                    client_msg_id if current_origin == "heartbeat" else ""
                )
                if not rows:
                    client_user_provenance.pop(key, None)
                # Nothing is written at this position, so whatever an earlier
                # import left here would outlive it. When the parser starts
                # hiding a message every later turn shifts down, and the old
                # occupant of this slot stayed visible as a duplicate.
                removed = database.execute(
                    """DELETE FROM messages
                        WHERE agent_id = ? AND backend_session_id = ?
                          AND source_file = ? AND seq = ?""",
                    (agent_id, backend_session_id, source_file, seq)).rowcount
                skipped_slot_removed = skipped_slot_removed or removed > 0
                continue
            current_origin, current_sender_agent_id = "user", ""
            current_heartbeat_key = ""
        if role == "user":
            current_request_trace = str(turn.get("trace_id") or "")
            current_origin, current_sender_agent_id = origin, sender_agent_id
            current_heartbeat_key = msg_id if origin == "heartbeat" else ""
        elif role == "assistant":
            origin, sender_agent_id = current_origin, current_sender_agent_id
        if role == "assistant" and not turn.get("id"):
            # Keep the already visible completion identity when its exact
            # authored request and final text identify this transcript reply.
            # Never deduplicate equal replies belonging to different requests.
            slot = database.execute("SELECT message_id FROM messages WHERE agent_id=? AND backend_session_id=? AND source_file=? AND seq=? AND role='assistant'",
                (agent_id, backend_session_id, source_file, seq)).fetchone()
            if slot:
                msg_id = slot["message_id"]
            elif current_request_trace:
                candidates = database.execute("SELECT message_id,text FROM messages WHERE agent_id=? AND backend_session_id=? AND source_file=? AND role='assistant'",
                    (agent_id, backend_session_id, "final:"+current_request_trace)).fetchall()
                matching = [r for r in candidates if r["message_id"] not in adopted_final_ids
                            and _strip_voice_markup(r["text"]) == _strip_voice_markup(text)]
                if len(matching) == 1:
                    msg_id = matching[0]["message_id"]
                    adopted_final_ids.add(msg_id)
        timestamp = turn.get("timestamp")
        kind = turn.get("kind")
        tool_name = turn.get("tool_name")
        tools = turn.get("tools") if isinstance(turn.get("tools"), list) else []
        display_cells = (
            turn.get("display_cells")
            if isinstance(turn.get("display_cells"), list)
            else []
        )
        tools_json = json.dumps(tools, separators=(",", ":"))
        display_cells_json = json.dumps(display_cells, separators=(",", ":"))
        existing = database.execute(
            """SELECT role, timestamp, text, kind, tool_name, tools_json,
                      display_cells_json, updated_at, revision,
                      origin, sender_agent_id
                 FROM messages WHERE message_id = ?""",
            (msg_id,),
        ).fetchone()
        payload = (
            role, timestamp, text, kind, tool_name,
            tools_json, display_cells_json, origin, sender_agent_id,
        )
        unchanged = existing is not None and _row_payload(existing) == payload
        revision = int(existing["revision"]) if unchanged else _next_revision(database)
        updated_at = int(existing["updated_at"]) if unchanged else now_ms()
        latest_revision = max(latest_revision, revision)
        database.execute(
            """INSERT INTO messages (
               message_id, agent_id, backend_session_id, source_file, seq,
               role, timestamp, text, kind, tool_name, tools_json,
               display_cells_json, updated_at, revision, origin,
               sender_agent_id
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(message_id) DO UPDATE SET
               source_file = excluded.source_file,
               seq = excluded.seq,
               role = excluded.role,
               timestamp = excluded.timestamp,
               text = excluded.text,
               kind = excluded.kind,
               tool_name = excluded.tool_name,
               tools_json = excluded.tools_json,
               display_cells_json = excluded.display_cells_json,
               updated_at = excluded.updated_at,
               revision = excluded.revision,
               origin = excluded.origin,
               sender_agent_id = excluded.sender_agent_id""",
        (msg_id, agent_id, backend_session_id, source_file, seq,
             role, timestamp, text, kind, tool_name, tools_json,
             display_cells_json, updated_at,
             revision, origin, sender_agent_id),
        )
        if role == "assistant" and not unchanged:
            if heartbeat_accounting == "noop":
                heartbeat.record_heartbeat_noop_once(agent_id, current_heartbeat_key)
            elif heartbeat_accounting == "activity":
                heartbeat.record_heartbeat_activity_once(
                    agent_id, current_heartbeat_key)
        if role == "assistant":
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
    # Compare markup-normalized so a streamed live row (with <speak>/<vox>/…)
    # still matches its durable copy (markup stripped); raw startswith missed it.
    live_replace_revision = 0
    for live in database.execute(
        """SELECT message_id, text FROM messages
            WHERE agent_id = ? AND backend_session_id = ?
              AND source_file LIKE 'live:%'
              AND role = 'assistant'""",
        (agent_id, backend_session_id),
    ).fetchall():
        live_text = _strip_voice_markup(live["text"])
        if not live_text:
            continue
        if any(final.startswith(live_text) or live_text.startswith(final)
               for final in final_assistant_texts):
            live_replace_revision = max(live_replace_revision, _next_revision(database))
            latest_revision = max(latest_revision, live_replace_revision)
            database.execute(
                "DELETE FROM messages WHERE message_id = ?",
                (live["message_id"],),
            )
    stale = database.execute(
        """SELECT 1 FROM messages
            WHERE agent_id = ? AND backend_session_id = ? AND seq >= ?
              AND source_file NOT LIKE 'client:%'
              AND source_file NOT LIKE 'live:%'
              AND source_file NOT LIKE 'final:%'
            LIMIT 1""",
        (agent_id, backend_session_id, len(turns)),
    ).fetchone()
    database.execute(
        """DELETE FROM messages
            WHERE agent_id = ? AND backend_session_id = ? AND seq >= ?
              AND source_file NOT LIKE 'client:%'
              AND source_file NOT LIKE 'live:%'
              AND source_file NOT LIKE 'final:%'""",
        (agent_id, backend_session_id, len(turns)),
    )
    stale_replace_revision = (
        _next_revision(database)
        if stale is not None or skipped_slot_removed else 0)
    replace_revision = max(stale_replace_revision, live_replace_revision)
    latest_revision = max(latest_revision, replace_revision)
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
    return out
