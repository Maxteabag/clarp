"""Shared read-model IDs, revisions and import input normalization.

Every SQL helper uses the caller's writer/connection; none opens a transaction.
Moved mechanically from message_writes (S5, lead603/604).
"""
from __future__ import annotations

import datetime as _dt
import hashlib
from typing import Any, NamedTuple

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
    return _user_provenance(row)


def _user_provenance(row) -> tuple[str, str, str]:
    if row is None:
        return "", "user", ""
    return (
        row["message_id"], row["origin"] or "user",
        row["sender_agent_id"] or "",
    )


# ---- the same two answers from message rows that were read anyway --------------
#
# A frontier is published with every row of the session, every column
# (message_writes._carry_message_rows). An import that has read those rows
# has the /send rows and the latest user row in its hands; asking SQLite for
# them again makes it fetch every row of the session from the table twice
# more, because no index holds source_file or role (on a Host whose database
# is larger than its memory map that was two thirds of what a tail read).
#
# The answers must be the statements' own, to the row. Both statements order
# by columns that end in seq, and (agent_id, backend_session_id, seq) is
# unique, so within one session neither order has a tie and both are fully
# determined: sorting the same rows by the same keys gives the same list.
# What is left is to compare as SQLite compares: NULL, then numbers, then
# text by its bytes, then blobs; and to match LIKE as SQLite does, without
# regard to the case of ASCII letters.

def _sqlite_order(value):
    """A key that orders plain column values as SQLite's ORDER BY does."""
    if value is None:
        return (0, 0)
    if isinstance(value, (int, float)):
        return (1, value)
    if isinstance(value, str):
        return (2, value.encode("utf-8", "surrogatepass"))
    return (3, bytes(value))


def _like_prefix(value, prefix: str) -> bool:
    """``value LIKE '<prefix>%'`` for an ASCII prefix without wildcards."""
    if not isinstance(value, str):
        return False                    # NULL; anything else never gets here (_derivable)
    head = value[:len(prefix)]
    return head.isascii() and head.lower() == prefix


def _derivable(rows, backend_session_id) -> bool:
    """Whether every value the two answers are decided by is of a kind this
    module compares exactly as SQLite does. A blob where text belongs is not
    (LIKE and = read it differently); then the statements are asked."""
    if not isinstance(backend_session_id, str):
        return False
    for row in rows:
        if not isinstance(row["backend_session_id"], (str, type(None))) \
                or not isinstance(row["source_file"], (str, type(None))) \
                or not isinstance(row["role"], (str, type(None))):
            return False
    return True


def _session_parts(rows, backend_session_id):
    """(the /send rows, the latest user row's provenance) of one session, from
    message rows read with every column: what ``_client_provenance_rows`` and
    ``_latest_user_provenance`` return for the same rows. ``rows`` are the
    session's rows or the whole agent's. None when they cannot be taken from
    the rows (``_derivable``)."""
    if not _derivable(rows, backend_session_id):
        return None
    own = [row for row in rows if row["backend_session_id"] == backend_session_id]
    sends = sorted(
        (row for row in own if _like_prefix(row["source_file"], "client:")),
        key=lambda row: (_sqlite_order(row["updated_at"]), _sqlite_order(row["seq"])))
    users = [row for row in own if row["role"] == "user"]
    latest = max(users, key=lambda row: (_sqlite_order(row["updated_at"]),
                                         _sqlite_order(row["revision"]),
                                         _sqlite_order(row["seq"]))) if users else None
    return ([_ClientProvenanceRow(row["message_id"], row["text"], row["origin"],
              row["sender_agent_id"], row["timestamp"], row["trace_id"])
             for row in sends], _user_provenance(latest))


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


class _ClientProvenanceRow(NamedTuple):
    message_id: Any
    text: Any
    origin: Any
    sender_agent_id: Any
    timestamp: Any
    trace_id: Any


def _client_provenance_rows(database, agent_id, backend_session_id) -> list[_ClientProvenanceRow]:
    """The /send rows of a session in the order they were written, as plain
    named tuples: the original five positions, then trace_id."""
    return [_ClientProvenanceRow(*row) for row in database.execute(
        """SELECT message_id, text, origin, sender_agent_id, timestamp, trace_id FROM messages
            WHERE agent_id = ? AND backend_session_id = ?
              AND source_file LIKE 'client:%'
            ORDER BY updated_at ASC, seq ASC""",
        (agent_id, backend_session_id))]


def _client_provenance(rows) -> dict[str, list[tuple[str, str, str, str]]]:
    provenance: dict[str, list[tuple[str, str, str, str]]] = {}
    for row in rows:
        # Preserve the normalizer's four-field output and the original five
        # input positions; matching alone consumes the appended named trace.
        message_id, text, origin, sender_agent_id, timestamp = row[:5]
        key = _norm_text(text)
        provenance.setdefault(key, []).append((
            message_id, origin or "user", sender_agent_id or "", timestamp))
    return provenance
