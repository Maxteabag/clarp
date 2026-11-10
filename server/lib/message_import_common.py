"""Shared read-model IDs, revisions and import input normalization.

Every SQL helper uses the caller's writer/connection; none opens a transaction.
Moved mechanically from message_writes (S5, lead603/604).
"""
from __future__ import annotations

import datetime as _dt
import hashlib
from typing import Any

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


_FINAL_TWIN_WINDOW_S = 600


_RETRY_ECHO_SLACK_S = 1.0


def _parse_timestamp(value: Any) -> _dt.datetime | None:
    try:
        stamp = _dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=_dt.timezone.utc)


def _is_retry_echo(native_timestamp: Any, next_client_timestamp: Any) -> bool:
    """A native copy of the request just matched, not of the next client row
    with the same text: that row was written after this copy was sent."""
    if next_client_timestamp is None:
        return True
    native_at = _parse_timestamp(native_timestamp)
    next_at = _parse_timestamp(next_client_timestamp)
    return (native_at is not None and next_at is not None
            and (next_at - native_at).total_seconds() > _RETRY_ECHO_SLACK_S)


_PARKED_SEQ_BASE = 10 ** 12


def _client_provenance_rows(database, agent_id, backend_session_id):
    return database.execute(
        """SELECT message_id, text, origin, sender_agent_id, timestamp FROM messages
            WHERE agent_id = ? AND backend_session_id = ?
              AND source_file LIKE 'client:%'
            ORDER BY updated_at ASC, seq ASC""",
        (agent_id, backend_session_id)).fetchall()


def _client_provenance(rows) -> dict[str, list[tuple[str, str, str, str]]]:
    provenance: dict[str, list[tuple[str, str, str, str]]] = {}
    for row in rows:
        key = _norm_text(row["text"])
        provenance.setdefault(key, []).append((
            row["message_id"], row["origin"] or "user",
            row["sender_agent_id"] or "", row["timestamp"]))
    return provenance
