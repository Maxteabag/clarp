"""Live assistant row protocol and agy turn authority.

One mutable `live:` row per in-flight turn (`upsert_live_assistant_message`),
its retraction and finalisation, the pre-turn snapshot/restore used to fence
agy imports, and the derived side effects run after a final row commits.
"""
from __future__ import annotations

import hashlib
from typing import Any

from .clock import iso_from_ms as _iso_from_ms, now_ms
from .db import conn
from . import dreaming, heartbeat, team_leader, team_store
from .voice_markup import strip_hidden_blocks
from .message_writes import _facade, _latest_user_provenance, _next_revision


def _live_message_id(agent_id: str, backend_session_id: str, trace_id: str) -> str:
    raw = f"{agent_id}\0{backend_session_id}\0{trace_id or 'live'}"
    return "live-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


# Claude Code's reply to its own isMeta "Continue from where you left off."
# record; never something a user should see. Keep in step with parse_turns.
CLAUDE_META_REPLY = "No response requested."

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
    if not text.strip() or text.strip() == CLAUDE_META_REPLY:
        # Claude Code answers its own injected "Continue from where you left
        # off." with this line whenever an interrupted session is resumed.
        # parse_turns drops it from the durable transcript; streaming it here
        # first showed it as a real bubble on every account-recovery resume.
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
                   sender_agent_id, trace_id
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            # The trace was only ever encoded into source_file, leaving this
            # column NULL on every live row, so nothing could tell the agent's
            # own reply apart from the one this trace had just delivered. See
            # model_fallbacks._superseded_by_own_reply.
            (msg_id, agent_id, backend_session_id, f"live:{trace_id or msg_id}",
             -900000, "assistant", timestamp, text, "live", None, "[]", "[]",
             timestamp_ms, revision, origin, sender_agent_id, trace_id or None),
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
                  SET text=?, updated_at=?, revision=?, origin=?, sender_agent_id=?,
                      trace_id=COALESCE(?, trace_id)
                WHERE message_id=? AND agent_id=? AND backend_session_id=?""",
            (text, timestamp_ms, revision, origin, sender_agent_id,
             trace_id or None, msg_id, agent_id, backend_session_id),
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
        if not _facade()._restore_assistant_state_txn(
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
        if not _facade()._restore_assistant_state_txn(
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
