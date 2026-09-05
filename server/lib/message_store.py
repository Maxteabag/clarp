"""SQLite transcript-message read model."""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
from typing import Any

from .db import conn, now_ms
from . import dreaming, heartbeat, origins, team_leader, team_store
from .voice_markup import clean_for_display, strip_hidden_blocks


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


def has_client_message(client_msg_id: str) -> bool:
    if not client_msg_id.strip():
        return False
    return conn().execute(
        "SELECT 1 FROM messages WHERE message_id = ? AND role = 'user'",
        (_client_message_id(client_msg_id),),
    ).fetchone() is not None


def _live_message_id(agent_id: str, backend_session_id: str, trace_id: str) -> str:
    raw = f"{agent_id}\0{backend_session_id}\0{trace_id or 'live'}"
    return "live-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


def _norm_text(text: str | None) -> str:
    return (text or "").strip()


def _automation_kind(*, role: str, origin: str | None,
                     text: str | None) -> str:
    raw_origin = (origin or "user").strip() or "user"
    # External watchers are user-facing, but their trigger and resulting turn
    # are still automated chat activity. Keep display classification separate
    # from notification policy so they can be collapsed without silencing them.
    if raw_origin == "watcher":
        return "watcher"
    if raw_origin in origins.ROUTINE_AUTOMATION_ORIGINS:
        return raw_origin
    if (
        role == "user"
        and raw_origin == "schedule"
        and team_leader.should_skip_leader_tick_prompt(text or "")
    ):
        return "leader_tick"
    return ""


def _display_text_for_message(*, role: str, origin: str | None,
                              text: str | None) -> str:
    raw = strip_hidden_blocks(str(text or ""))
    kind = _automation_kind(role=role, origin=origin, text=raw)
    if not kind:
        return raw
    if role == "user":
        return {
            "heartbeat": "Automated heartbeat check",
            "leader_tick": "Automated leader check",
            "dreaming": "Automated dreaming run",
            "watcher": "Automated watcher event",
        }.get(kind, raw)
    if kind == "heartbeat" and heartbeat.HEARTBEAT_OK in raw:
        return "Heartbeat check: no action needed."
    if kind == "leader_tick" and team_leader.LEADER_NOOP in raw:
        return "Leader check: no action needed."
    if kind == "dreaming" and dreaming.DREAMING_OK in raw:
        return "Dreaming check: no action needed."
    return raw


def _strip_voice_markup(text: str | None) -> str:
    """Normalized, markup-free single-line form of a reply — used to compare a
    streamed turn (markup intact) against its durable copy when superseding the
    live row. Delegates to the canonical cleaner so the rules never drift."""
    return clean_for_display(text, oneline=True)


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


def _client_tools(tools: Any, display_cells: Any) -> list:
    if not isinstance(tools, list):
        return []
    if isinstance(display_cells, list) and display_cells:
        # Codex display cells already carry command/search output. Keep only
        # edit-shaped tools so native can reuse the existing diff renderer.
        return [
            tool for tool in tools
            if isinstance(tool, dict)
            and tool.get("name") in {"Edit", "MultiEdit", "Write"}
        ]
    return tools


def _iso_from_ms(ts_ms: int) -> str:
    dt = _dt.datetime.fromtimestamp(ts_ms / 1000, tz=_dt.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


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


def upsert_live_assistant_message(*, agent_id: str, backend_session_id: str,
                                  trace_id: str = "", text: str
                                  ) -> dict[str, Any] | None:
    """Store the current streamed assistant text for an in-flight Claude turn.

    It is one mutable row per active turn, not one row per token. When the
    durable transcript later catches up, store_transcript_turns removes this
    live row if the final assistant text covers it.
    """
    if not agent_id or not backend_session_id:
        return None
    text = strip_hidden_blocks(str(text or ""))
    if not text.strip():
        return None
    leader_noop_text = text
    skip, text = heartbeat.strip_heartbeat_ack(text)
    if not skip:
        skip, text = dreaming.process_assistant_text(agent_id, text, live=True)
    if not skip:
        leader_noop_text = text
        skip, text = team_leader.strip_leader_noop(text)
    database = conn()
    if trace_id:
        active = database.execute(
            """SELECT trace_id FROM turns
                 WHERE agent_id=? AND ended_at IS NULL
                 ORDER BY turn_id DESC LIMIT 1""", (agent_id,),
        ).fetchone()
        if active is None or (active["trace_id"] or "") != trace_id:
            return None
    msg_id = _live_message_id(agent_id, backend_session_id, trace_id)
    if skip:
        _user_key, origin, _sender_agent_id = _latest_user_provenance(
            database, agent_id, backend_session_id)
        if origin == "heartbeat":
            text = "Heartbeat check: no action needed."
        elif origin == "dreaming":
            text = "Dreaming check: no action needed."
        elif origin == "leader_tick":
            text = "Leader check: no action needed."
        else:
            text = "Automated check: no action needed."
        if team_leader.contains_leader_noop(leader_noop_text):
            team_leader.record_leader_noop(agent_id)
    timestamp_ms = now_ms()
    timestamp = _iso_from_ms(timestamp_ms)
    _user_key, origin, sender_agent_id = _latest_user_provenance(
        database, agent_id, backend_session_id)
    existing = database.execute(
        """SELECT timestamp, text, revision, origin, sender_agent_id
             FROM messages WHERE message_id = ?""",
        (msg_id,),
    ).fetchone()
    if (
        existing is not None
        and (existing["text"] or "") == text
        and (existing["origin"] or "user") == origin
        and (existing["sender_agent_id"] or "") == sender_agent_id
    ):
        return {
            "id": msg_id, "role": "assistant",
            "timestamp": existing["timestamp"], "text": text, "kind": "live",
            "tool_name": None, "tools": [], "display_cells": [],
            "origin": origin, "sender_agent_id": sender_agent_id,
            "revision": int(existing["revision"]),
            "changed": False,
        }
    replaced_live = False
    if existing is None:
        inserted, revision, replaced_live = _insert_live_message_atomic(
            database=database, msg_id=msg_id, agent_id=agent_id,
            backend_session_id=backend_session_id, trace_id=trace_id,
            timestamp=timestamp, timestamp_ms=timestamp_ms, text=text,
            origin=origin, sender_agent_id=sender_agent_id,
        )
        if not inserted:
            return None
    else:
        updated, revision = _update_live_message_atomic(
            database=database, msg_id=msg_id, agent_id=agent_id,
            backend_session_id=backend_session_id, trace_id=trace_id,
            timestamp_ms=timestamp_ms, text=text, origin=origin,
            sender_agent_id=sender_agent_id,
        )
        if not updated:
            return None
        timestamp = existing["timestamp"]
    return {
        "id": msg_id, "role": "assistant", "timestamp": timestamp,
        "text": text, "kind": "live", "tool_name": None, "tools": [],
        "display_cells": [],
        "origin": origin, "sender_agent_id": sender_agent_id,
        "revision": revision, "changed": True,
    }


def _insert_live_message_atomic(
    *, database, msg_id: str, agent_id: str, backend_session_id: str,
    trace_id: str, timestamp: str, timestamp_ms: int, text: str,
    origin: str, sender_agent_id: str,
) -> tuple[bool, int, bool]:
    database.execute("BEGIN IMMEDIATE")
    try:
        if trace_id:
            active = database.execute(
                """SELECT trace_id FROM turns
                     WHERE agent_id=? AND ended_at IS NULL
                     ORDER BY turn_id DESC LIMIT 1""", (agent_id,),
            ).fetchone()
            if active is None or (active["trace_id"] or "") != trace_id:
                database.execute("ROLLBACK")
                return False, 0, False
        revision = _next_revision(database)
        replaced = database.execute(
            """DELETE FROM messages
                WHERE agent_id=? AND backend_session_id=?
                  AND source_file LIKE 'live:%' AND message_id<>?""",
            (agent_id, backend_session_id, msg_id),
        ).rowcount > 0
        database.execute(
            """INSERT INTO messages (
                   message_id, agent_id, backend_session_id, source_file, seq,
                   role, timestamp, text, kind, tool_name, tools_json,
                   display_cells_json, updated_at, revision, origin,
                   sender_agent_id
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (msg_id, agent_id, backend_session_id, f"live:{trace_id or msg_id}",
             -900000, "assistant", timestamp, text, "live", None, "[]", "[]",
             timestamp_ms, revision, origin, sender_agent_id),
        )
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
            (agent_id, backend_session_id, revision, revision if replaced else 0),
        )
        database.execute("COMMIT")
        return True, revision, replaced
    except Exception:
        database.execute("ROLLBACK")
        raise


def _update_live_message_atomic(
    *, database, msg_id: str, agent_id: str, backend_session_id: str,
    trace_id: str, timestamp_ms: int, text: str, origin: str,
    sender_agent_id: str,
) -> tuple[bool, int]:
    database.execute("BEGIN IMMEDIATE")
    try:
        if trace_id:
            active = database.execute(
                """SELECT trace_id FROM turns
                     WHERE agent_id=? AND ended_at IS NULL
                     ORDER BY turn_id DESC LIMIT 1""", (agent_id,),
            ).fetchone()
            if active is None or (active["trace_id"] or "") != trace_id:
                database.execute("ROLLBACK")
                return False, 0
        revision = _next_revision(database)
        changed = database.execute(
            """UPDATE messages
                  SET text=?, updated_at=?, revision=?, origin=?, sender_agent_id=?
                WHERE message_id=? AND agent_id=? AND backend_session_id=?""",
            (text, timestamp_ms, revision, origin, sender_agent_id, msg_id,
             agent_id, backend_session_id),
        ).rowcount > 0
        if not changed:
            database.execute("ROLLBACK")
            return False, 0
        database.execute(
            """INSERT INTO conversation_heads (
                   agent_id, backend_session_id, revision, replace_revision
               ) VALUES (?, ?, ?, 0)
               ON CONFLICT(agent_id, backend_session_id) DO UPDATE SET
                   revision = MAX(conversation_heads.revision, excluded.revision)""",
            (agent_id, backend_session_id, revision),
        )
        database.execute("COMMIT")
        return True, revision
    except Exception:
        database.execute("ROLLBACK")
        raise


def delete_live_assistant_message(*, agent_id: str, backend_session_id: str,
                                  trace_id: str = "") -> bool:
    """Retract a provisional assistant row after authoritative empty output."""
    database = conn()
    msg_id = _live_message_id(agent_id, backend_session_id, trace_id)
    exists = database.execute(
        "SELECT 1 FROM messages WHERE message_id = ?", (msg_id,),
    ).fetchone()
    if exists is None:
        return False
    database.execute("DELETE FROM messages WHERE message_id = ?", (msg_id,))
    revision = _next_revision(database)
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
        (agent_id, backend_session_id, revision, revision),
    )
    return True


def finalize_live_assistant_message(*, agent_id: str, backend_session_id: str,
                                    trace_id: str, text: str
                                    ) -> dict[str, Any] | None:
    """Finalize one bounded live row and run canonical durable side effects."""
    row = upsert_live_assistant_message(
        agent_id=agent_id, backend_session_id=backend_session_id,
        trace_id=trace_id, text=text)
    if row is None:
        return None
    database = conn()
    database.execute("BEGIN IMMEDIATE")
    try:
        turn = database.execute(
            """SELECT turn_id, trace_id FROM turns
                 WHERE agent_id=? AND ended_at IS NULL
                 ORDER BY turn_id DESC LIMIT 1""", (agent_id,),
        ).fetchone()
        if turn is None or (turn["trace_id"] or "") != trace_id:
            database.execute("ROLLBACK")
            return None
        revision = _next_revision(database)
        sequence = -800000 + int(turn["turn_id"])
        changed = database.execute(
            """UPDATE messages
                  SET source_file=?, seq=?, kind=NULL, revision=?, updated_at=?
                WHERE message_id=? AND agent_id=? AND backend_session_id=?""",
            (f"final:{trace_id}", sequence, revision, now_ms(), row["id"],
             agent_id, backend_session_id),
        ).rowcount > 0
        if not changed:
            database.execute("ROLLBACK")
            return None
        database.execute(
            """INSERT INTO conversation_heads (
                   agent_id, backend_session_id, revision, replace_revision
               ) VALUES (?, ?, ?, 0)
               ON CONFLICT(agent_id, backend_session_id) DO UPDATE SET
                   revision=MAX(conversation_heads.revision, excluded.revision)""",
            (agent_id, backend_session_id, revision),)
        database.execute("COMMIT")
        row = {**row, "kind": None, "revision": revision}
    except Exception:
        database.execute("ROLLBACK")
        raise
    user_key, origin, _sender = _latest_user_provenance(
        conn(), agent_id, backend_session_id)
    final_text = str(row.get("text") or "")
    if origin == "heartbeat":
        skip, _clean = heartbeat.strip_heartbeat_ack(text)
        if skip:
            heartbeat.record_heartbeat_noop_once(agent_id, user_key)
        elif not heartbeat.is_neutral_heartbeat_status(final_text):
            heartbeat.record_heartbeat_activity_once(agent_id, user_key)
    dreaming.process_assistant_text(agent_id, text, live=False)
    team_store.capture_assistant_message(
        agent_id=agent_id, source_message_id=str(row["id"]),
        trace_id=trace_id, text=final_text)
    from . import oracle_delegations
    oracle_delegations.complete_for_trace(
        trace_id=trace_id, message_id=str(row["id"]), text=final_text)
    return row


def capture_assistant_state(*, agent_id: str,
                            backend_session_id: str) -> dict[str, Any]:
    return _capture_assistant_state_txn(
        conn(), agent_id=agent_id, backend_session_id=backend_session_id)


def _capture_assistant_state_txn(database, *, agent_id: str,
                                 backend_session_id: str) -> dict[str, Any]:
    messages = [dict(row) for row in database.execute(
        """SELECT * FROM messages
             WHERE agent_id=? AND backend_session_id=? AND role='assistant'""",
        (agent_id, backend_session_id),).fetchall()]
    source_ids = [row["message_id"] for row in messages]
    team_messages: list[dict[str, Any]] = []
    if source_ids:
        marks = ",".join("?" for _ in source_ids)
        team_messages = [dict(row) for row in database.execute(
            f"SELECT * FROM team_messages WHERE source_message_id IN ({marks})",
            source_ids,).fetchall()]
    return {"messages": messages, "team_messages": team_messages}


def begin_agy_assistant_turn(*, agent_id: str, backend_session_id: str,
                             trace_id: str,
                             observed_assistant_count: int) -> dict[str, Any] | None:
    """Atomically fence imports and capture the exact pre-turn baseline."""
    database = conn()
    database.execute("BEGIN IMMEDIATE")
    try:
        active = database.execute(
            """SELECT trace_id FROM turns
                 WHERE agent_id=? AND ended_at IS NULL
                 ORDER BY turn_id DESC LIMIT 1""", (agent_id,),).fetchone()
        if active is None or (active["trace_id"] or "") != trace_id:
            database.execute("ROLLBACK")
            return None
        start = max(0, int(observed_assistant_count))
        database.execute(
            """UPDATE agy_turn_authority
                  SET assistant_end_ordinal=?, updated_at=?
                WHERE agent_id=? AND backend_session_id=?
                  AND assistant_end_ordinal IS NULL AND trace_id<>?""",
            (start, now_ms(), agent_id, backend_session_id, trace_id),)
        database.execute(
            """INSERT INTO agy_turn_authority (
                   agent_id,backend_session_id,trace_id,
                   assistant_start_ordinal,assistant_end_ordinal,
                   terminal_status,authoritative_message_id,updated_at
               ) VALUES (?,?,?,?,NULL,'pending',NULL,?)
               ON CONFLICT(agent_id,backend_session_id,trace_id) DO NOTHING""",
            (agent_id, backend_session_id, trace_id, start, now_ms()),)
        snapshot = _capture_assistant_state_txn(
            database, agent_id=agent_id,
            backend_session_id=backend_session_id)
        database.execute("COMMIT")
        return snapshot
    except Exception:
        database.execute("ROLLBACK")
        raise


def restore_assistant_state(*, agent_id: str, backend_session_id: str,
                            trace_id: str, snapshot: dict[str, Any]) -> bool:
    """Restore the exact pre-turn assistant/derived payload under turn ownership."""
    database = conn()
    database.execute("BEGIN IMMEDIATE")
    try:
        if not _restore_assistant_state_txn(
                database, agent_id=agent_id,
                backend_session_id=backend_session_id,
                trace_id=trace_id, snapshot=snapshot):
            database.execute("ROLLBACK")
            return False
        database.execute("COMMIT")
        return True
    except Exception:
        database.execute("ROLLBACK")
        raise


def _restore_assistant_state_txn(database, *, agent_id: str,
                                 backend_session_id: str, trace_id: str,
                                 snapshot: dict[str, Any]) -> bool:
        active = database.execute(
            """SELECT trace_id FROM turns
                 WHERE agent_id=? AND ended_at IS NULL
                 ORDER BY turn_id DESC LIMIT 1""", (agent_id,),).fetchone()
        if active is None or (active["trace_id"] or "") != trace_id:
            return False
        current_ids = [row["message_id"] for row in database.execute(
            """SELECT message_id FROM messages
                 WHERE agent_id=? AND backend_session_id=? AND role='assistant'""",
            (agent_id, backend_session_id),).fetchall()]
        baseline_messages = list(snapshot.get("messages") or [])
        baseline_ids = [row["message_id"] for row in baseline_messages]
        affected_ids = list(dict.fromkeys(current_ids + baseline_ids))
        if affected_ids:
            marks = ",".join("?" for _ in affected_ids)
            current_team_ids = [row["team_message_id"] for row in database.execute(
                f"SELECT team_message_id FROM team_messages "
                f"WHERE source_message_id IN ({marks})", affected_ids).fetchall()]
            baseline_team_ids = {
                row["team_message_id"] for row in snapshot.get("team_messages") or []}
            doomed_team_ids = [team_id for team_id in current_team_ids
                               if team_id not in baseline_team_ids]
            if doomed_team_ids:
                team_marks = ",".join("?" for _ in doomed_team_ids)
                database.execute(
                    f"DELETE FROM team_inbox WHERE team_message_id IN ({team_marks})",
                    doomed_team_ids)
                database.execute(
                    f"DELETE FROM team_messages WHERE team_message_id IN ({team_marks})",
                    doomed_team_ids)
        # Server-authored assistant rows are exempt. The transcript is the
        # source of truth for anything the CLI said, so the rebuild clears and
        # re-derives it — but a dream digest was written by an isolated
        # backend session that appears in no transcript, so deleting it here
        # would erase it permanently on the agent's very next turn.
        database.execute(
            "DELETE FROM messages WHERE agent_id=? AND backend_session_id=? "
            "AND role='assistant' AND source_file NOT LIKE 'dream:%'",
            (agent_id, backend_session_id))
        message_columns = (
            "message_id", "agent_id", "backend_session_id", "source_file", "seq",
            "role", "timestamp", "text", "kind", "tool_name", "tools_json",
            "display_cells_json", "updated_at", "revision", "origin",
            "sender_agent_id", "prompt_admission_id")
        database.executemany(
            f"INSERT INTO messages ({','.join(message_columns)}) "
            f"VALUES ({','.join('?' for _ in message_columns)})",
            [tuple(row[column] for column in message_columns)
             for row in baseline_messages])
        revision = _next_revision(database)
        database.execute(
            """INSERT INTO conversation_heads (
                   agent_id, backend_session_id, revision, replace_revision
               ) VALUES (?, ?, ?, ?)
               ON CONFLICT(agent_id, backend_session_id) DO UPDATE SET
                   revision=MAX(conversation_heads.revision, excluded.revision),
                   replace_revision=MAX(
                       conversation_heads.replace_revision,
                       excluded.replace_revision)""",
            (agent_id, backend_session_id, revision, revision))
        return True


def commit_agy_assistant_turn(*, agent_id: str, backend_session_id: str,
                              trace_id: str, snapshot: dict[str, Any],
                              terminal_status: str, text: str = ""
                              ) -> dict[str, Any] | None:
    """Restore provisional imports and commit terminal authority atomically."""
    if terminal_status not in {"success", "empty", "error"}:
        raise ValueError("invalid AGY terminal status")
    database = conn()
    database.execute("BEGIN IMMEDIATE")
    try:
        if not _restore_assistant_state_txn(
                database, agent_id=agent_id,
                backend_session_id=backend_session_id,
                trace_id=trace_id, snapshot=snapshot):
            database.execute("ROLLBACK")
            return None
        message_id: str | None = None
        row: dict[str, Any] | None = None
        clean_text = strip_hidden_blocks(str(text or ""))
        if terminal_status == "success" and clean_text.strip():
            turn = database.execute(
                """SELECT turn_id FROM turns
                     WHERE agent_id=? AND trace_id=? AND ended_at IS NULL
                     ORDER BY turn_id DESC LIMIT 1""",
                (agent_id, trace_id),).fetchone()
            if turn is None:
                database.execute("ROLLBACK")
                return None
            message_id = _live_message_id(
                agent_id, backend_session_id, trace_id)
            timestamp_ms = now_ms()
            revision = _next_revision(database)
            _user_key, origin, sender_agent_id = _latest_user_provenance(
                database, agent_id, backend_session_id)
            sequence = -800000 + int(turn["turn_id"])
            database.execute(
                """INSERT INTO messages (
                       message_id,agent_id,backend_session_id,source_file,seq,
                       role,timestamp,text,kind,tool_name,tools_json,
                       display_cells_json,updated_at,revision,origin,
                       sender_agent_id,prompt_admission_id
                   ) VALUES (?,?,?,?,?,'assistant',?,?,NULL,NULL,'[]','[]',?,?,?,?,NULL)""",
                (message_id, agent_id, backend_session_id,
                 f"final:{trace_id}", sequence, _iso_from_ms(timestamp_ms),
                 clean_text, timestamp_ms, revision, origin, sender_agent_id),)
            database.execute(
                """INSERT INTO conversation_heads (
                       agent_id,backend_session_id,revision,replace_revision
                   ) VALUES (?,?,?,?)
                   ON CONFLICT(agent_id,backend_session_id) DO UPDATE SET
                       revision=MAX(conversation_heads.revision,excluded.revision),
                       replace_revision=MAX(
                           conversation_heads.replace_revision,
                           excluded.replace_revision)""",
                (agent_id, backend_session_id, revision, revision),)
            row = {
                "id": message_id, "role": "assistant",
                "timestamp": _iso_from_ms(timestamp_ms), "text": clean_text,
                "kind": None, "tool_name": None, "tools": [],
                "display_cells": [], "origin": origin,
                "sender_agent_id": sender_agent_id,
                "revision": revision, "changed": True,
            }
        database.execute(
            """UPDATE agy_turn_authority
                  SET terminal_status=?,authoritative_message_id=?,updated_at=?
                WHERE agent_id=? AND backend_session_id=? AND trace_id=?""",
            (terminal_status, message_id, now_ms(), agent_id,
             backend_session_id, trace_id),)
        database.execute("COMMIT")
        return row or {"committed": True}
    except Exception:
        database.execute("ROLLBACK")
        raise


def apply_final_assistant_side_effects(*, agent_id: str,
                                       backend_session_id: str,
                                       trace_id: str,
                                       row: dict[str, Any]) -> None:
    """Run derived hooks after the canonical terminal transaction commits."""
    user_key, origin, _sender = _latest_user_provenance(
        conn(), agent_id, backend_session_id)
    final_text = str(row.get("text") or "")
    if origin == "heartbeat":
        skip, _clean = heartbeat.strip_heartbeat_ack(final_text)
        if skip:
            heartbeat.record_heartbeat_noop_once(agent_id, user_key)
        elif not heartbeat.is_neutral_heartbeat_status(final_text):
            heartbeat.record_heartbeat_activity_once(agent_id, user_key)
    dreaming.process_assistant_text(agent_id, final_text, live=False)
    team_store.capture_assistant_message(
        agent_id=agent_id, source_message_id=str(row["id"]),
        trace_id=trace_id, text=final_text)
    from . import oracle_delegations
    oracle_delegations.complete_for_trace(
        trace_id=trace_id, message_id=str(row["id"]), text=final_text)


# The server appends a team-context block to the prompt the backend sees (see
# turn_dispatch._with_team_context). The backend's transcript copies the whole
# augmented prompt back, so on import we must strip the block from user turns —
# otherwise it leaks into the chat as if User typed it. Marker shared with
# turn_dispatch.
TEAM_CONTEXT_OPEN = "--- Clarp team context ---"
TEAM_CONTEXT_CLOSE = "--- End Clarp team context ---"


def strip_injected_team_context(text: str) -> str:
    """Drop server-injected team-context blocks from a user turn.

    Older turns appended the block after the user's text; newer turns prepend it
    for prompt-cache friendliness. Preserve the real user text in either order.
    """
    if not text or TEAM_CONTEXT_OPEN not in text:
        return text
    out = text
    while TEAM_CONTEXT_OPEN in out:
        before, rest = out.split(TEAM_CONTEXT_OPEN, 1)
        if TEAM_CONTEXT_CLOSE not in rest:
            return before.rstrip()
        _, after = rest.split(TEAM_CONTEXT_CLOSE, 1)
        out = (before + after).strip()
    return out


def store_transcript_turns(*, agent_id: str, backend_session_id: str,
                           source_file: str, turns: list[dict[str, Any]]
                           ) -> list[dict[str, Any]]:
    database = conn()
    database.execute("BEGIN IMMEDIATE")
    try:
        out = _store_transcript_turns_txn(
            database, agent_id=agent_id,
            backend_session_id=backend_session_id,
            source_file=source_file, turns=turns)
        database.execute("COMMIT")
        return out
    except Exception:
        database.execute("ROLLBACK")
        raise


def _store_transcript_turns_txn(database, *, agent_id: str,
                                backend_session_id: str, source_file: str,
                                turns: list[dict[str, Any]]
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
    for seq, turn in enumerate(turns):
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
            text = strip_injected_team_context(text)
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
                current_heartbeat_key = (
                    client_msg_id if current_origin == "heartbeat" else ""
                )
                if not rows:
                    client_user_provenance.pop(key, None)
                continue
            current_origin, current_sender_agent_id = "user", ""
            current_heartbeat_key = ""
        if role == "user":
            current_origin, current_sender_agent_id = origin, sender_agent_id
            current_heartbeat_key = msg_id if origin == "heartbeat" else ""
        elif role == "assistant":
            origin, sender_agent_id = current_origin, current_sender_agent_id
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
    final_assistant_texts = [
        _strip_voice_markup(t.get("text"))
        for t in turns
        if t.get("role") == "assistant" and _strip_voice_markup(t.get("text"))
    ]
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
    stale_replace_revision = _next_revision(database) if stale is not None else 0
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


def list_messages(*, agent_id: str, backend_session_id: str = "",
                  after_revision: int = 0,
                  before_message_id: str = "",
                  limit: int = 100,
                  include_automated: bool = True) -> list[dict[str, Any]]:
    params: list[Any] = [agent_id]
    where = "m.agent_id = ?"
    if backend_session_id:
        where += " AND m.backend_session_id = ?"
        params.append(backend_session_id)
    if after_revision:
        where += " AND m.revision > ?"
        params.append(int(after_revision))
    if before_message_id:
        cursor = conn().execute(
            """SELECT COALESCE(timestamp, '') AS timestamp, seq
                 FROM messages
                WHERE message_id = ? AND agent_id = ?
                  AND (? = '' OR backend_session_id = ?)""",
            (before_message_id, agent_id, backend_session_id, backend_session_id),
        ).fetchone()
        if cursor is None:
            return []
        where += """ AND (
            COALESCE(m.timestamp, '') < ?
            OR (COALESCE(m.timestamp, '') = ? AND m.seq < ?)
        )"""
        params.extend([cursor["timestamp"], cursor["timestamp"], int(cursor["seq"])])
    if not include_automated:
        # Apply presentation filtering before LIMIT. Otherwise a busy heartbeat
        # or watcher can fill the entire tail window and make a healthy chat
        # appear to end days earlier. Keep this SQL predicate equivalent to
        # _automation_kind above.
        where += """ AND NOT (
            COALESCE(m.origin, 'user') IN ('watcher', 'heartbeat', 'leader_tick', 'dreaming')
            OR (
                m.role = 'user'
                AND COALESCE(m.origin, 'user') = 'schedule'
                AND TRIM(COALESCE(m.text, '')) = ?
            )
        )"""
        params.append(team_leader.TICK_PROMPT)
    params.append(max(1, min(limit, 5000)))
    # Display order is by timestamp (tie-break seq). User rows live in a
    # negative seq band and transcript rows in 0..N-1, so ordering by seq would
    # be wrong; timestamp interleaves the client-recorded user turns with the
    # transcript's assistant turns correctly and removes any seq-collision
    # coupling between the two sources.
    if after_revision:
        order = "m.revision ASC, COALESCE(m.timestamp, '') ASC, m.seq ASC"
        query = f"""SELECT m.message_id, m.role, m.timestamp, m.text, m.kind,
                           m.tool_name, m.tools_json, m.display_cells_json,
                           m.revision, m.origin, m.sender_agent_id, m.trace_id,
                           sender.persona AS sender_name,
                           sender.session AS sender_session
                      FROM messages m
                      LEFT JOIN agents sender
                        ON sender.agent_id = m.sender_agent_id
                     WHERE {where}
                     ORDER BY {order}
                     LIMIT ?"""
    else:
        query = f"""SELECT m.message_id, m.role, m.timestamp, m.text, m.kind,
                           m.tool_name, m.tools_json, m.display_cells_json,
                           m.revision, m.origin, m.sender_agent_id, m.trace_id,
                           sender.persona AS sender_name,
                           sender.session AS sender_session
                      FROM (
                            SELECT message_id, role, timestamp, text, kind,
                                   tool_name, tools_json, display_cells_json,
                                   seq, revision, origin, sender_agent_id, trace_id
                              FROM messages m
                             WHERE {where}
                             ORDER BY COALESCE(timestamp, '') DESC, seq DESC
                             LIMIT ?
                      ) m
                      LEFT JOIN agents sender
                        ON sender.agent_id = m.sender_agent_id
                     ORDER BY COALESCE(m.timestamp, '') ASC, m.seq ASC"""
    rows = conn().execute(query, tuple(params)).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        automation_kind = _automation_kind(
            role=row["role"], origin=row["origin"], text=row["text"],
        )
        display_text = _display_text_for_message(
            role=row["role"], origin=row["origin"], text=row["text"],
        )
        try:
            tools = json.loads(row["tools_json"] or "[]")
        except json.JSONDecodeError:
            tools = []
        try:
            display_cells = json.loads(row["display_cells_json"] or "[]")
        except json.JSONDecodeError:
            display_cells = []
        out.append({
            "id": row["message_id"],
            "role": row["role"],
            "timestamp": row["timestamp"] or "",
            "text": display_text,
            "kind": row["kind"],
            "tool_name": row["tool_name"],
            "tools": _client_tools(tools, display_cells),
            "display_cells": (
                display_cells if isinstance(display_cells, list) else []
            ),
            "origin": (row["origin"] or "user"),
            "sender_agent_id": (row["sender_agent_id"] or ""),
            "trace_id": (row["trace_id"] or ""),
            "sender_name": (row["sender_name"] or ""),
            "sender_session": (row["sender_session"] or ""),
            "revision": int(row["revision"]),
            "automated": bool(automation_kind),
            "automation_kind": automation_kind,
        })
    return out


def last_message_head(*, agent_id: str, max_len: int = 80) -> dict[str, Any]:
    """One-line preview of the agent's most recent real non-tool message —
    for the agent-list overview, so the client needn't open each chat to see it.
    Strips <speak> voice tags and collapses whitespace. '' when nothing to show."""
    routine_origins = tuple(sorted(origins.ROUTINE_AUTOMATION_ORIGINS))
    routine_placeholders = ",".join("?" for _ in routine_origins)
    rows = conn().execute(
        f"""SELECT message_id, backend_session_id, revision, role, text, origin
              FROM messages
             WHERE agent_id = ?
               AND COALESCE(text, '') != ''
               AND COALESCE(tool_name, '') = ''
               AND COALESCE(origin, 'user') NOT IN (
                   {routine_placeholders}
               )
             ORDER BY {_message_activity_sql()} DESC, seq DESC, updated_at DESC
             LIMIT 50""",
        (agent_id, *routine_origins),
    ).fetchall()
    row = next(
        (
            candidate for candidate in rows
            if not _automation_kind(
                role=candidate["role"],
                origin=candidate["origin"],
                text=candidate["text"],
            )
        ),
        None,
    )
    if not row:
        return {"preview": "", "message_id": "", "revision": 0,
                "conversation_id": ""}
    raw_text = _display_text_for_message(
        role=row["role"], origin=row["origin"], text=row["text"],
    )
    text = clean_for_display(raw_text, oneline=True)
    if not text:
        return {"preview": "", "message_id": "", "revision": 0,
                "conversation_id": ""}
    preview = ("You: " if row["role"] == "user" else "") + text
    if len(preview) > max_len:
        preview = preview[: max_len - 1].rstrip() + "…"
    return {
        "preview": preview,
        "message_id": row["message_id"],
        "revision": int(row["revision"] or 0),
        "conversation_id": row["backend_session_id"] or "",
    }


def last_message_preview(*, agent_id: str, max_len: int = 80) -> str:
    return str(last_message_head(agent_id=agent_id, max_len=max_len)["preview"])


def last_message_activity(*, agent_id: str) -> int:
    """Epoch ms of the latest actual message, not its latest cache import."""
    row = conn().execute(
        f"""SELECT MAX({_message_activity_sql()}) AS t
              FROM messages
             WHERE agent_id = ?""",
        (agent_id,),
    ).fetchone()
    return int(row["t"] or 0)


def message_tool_details(*, session: str, message_id: str) -> dict[str, Any] | None:
    """Return the heavy tool payload for one message, scoped to its agent."""
    row = conn().execute(
        """SELECT m.tools_json, m.display_cells_json
             FROM messages m
             JOIN agents a ON a.agent_id = m.agent_id
            WHERE a.session = ? AND m.message_id = ? AND a.deleted_at IS NULL""",
        (session, message_id),
    ).fetchone()
    if row is None:
        return None
    try:
        tools = json.loads(row["tools_json"] or "[]")
    except json.JSONDecodeError:
        tools = []
    try:
        cells = json.loads(row["display_cells_json"] or "[]")
    except json.JSONDecodeError:
        cells = []
    return {
        "message_id": message_id,
        "tools": _client_tools(tools, cells),
        "display_cells": cells if isinstance(cells, list) else [],
    }


def last_real_message_activity(*, agent_id: str) -> int:
    """Epoch ms of the latest User-origin message/reply.

    Autonomous turns can write visible assistant rows too; those must not make
    heartbeat/leader schedulers believe User is in an active session. Server
    dispatch propagates the causing prompt's origin onto assistant rows, so
    origin='user' is the durable "real conversation" signal.
    """
    row = conn().execute(
        f"""SELECT MAX({_message_activity_sql()}) AS t
              FROM messages
             WHERE agent_id = ?
               AND COALESCE(origin, 'user') = 'user'""",
        (agent_id,),
    ).fetchone()
    return int(row["t"] or 0)


def latest_revision(*, agent_id: str, backend_session_id: str = "") -> int:
    params: list[Any] = [agent_id]
    where = "agent_id = ?"
    if backend_session_id:
        where += " AND backend_session_id = ?"
        params.append(backend_session_id)
    row = conn().execute(
        f"""SELECT MAX(revision) AS revision
              FROM (
                    SELECT revision FROM messages WHERE {where}
                    UNION ALL
                    SELECT revision FROM conversation_heads WHERE {where}
              )""",
        tuple(params + params),
    ).fetchone()
    return int(row["revision"] or 0)


def requires_replace(*, agent_id: str, backend_session_id: str = "",
                     after_revision: int = 0) -> bool:
    params: list[Any] = [agent_id]
    where = "agent_id = ?"
    if backend_session_id:
        where += " AND backend_session_id = ?"
        params.append(backend_session_id)
    row = conn().execute(
        f"""SELECT COALESCE(MAX(replace_revision), 0) AS revision
              FROM conversation_heads WHERE {where}""",
        tuple(params),
    ).fetchone()
    return int(row["revision"] or 0) > int(after_revision or 0)
