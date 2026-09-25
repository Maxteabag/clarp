"""Read-side projections over `messages`.

`/log` listing, provenance fields, one-line previews, the dashboard batch
projection, activity clocks and revision watermarks.
"""
from __future__ import annotations

import json
from typing import Any

from .db import conn
from . import origins, team_leader
from .voice_markup import clean_for_display
from .message_context import _automation_kind, _display_text_for_message
from .message_writes import _message_activity_sql


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
            "trace_id": (row["trace_id"] or ""),
            **provenance_fields(row),
            "revision": int(row["revision"]),
            "automated": bool(automation_kind),
            "automation_kind": automation_kind,
        })
    return out


def provenance_fields(row) -> dict[str, str]:
    """Split stored provenance into author versus answered sender.

    A user row written by another agent names that agent as its sender. The
    assistant row that answers it stores the same ``sender_agent_id`` so the
    trigger stays attributable, but its author is the transcript owner. Never
    project the trigger as the reply's sender: clients rendered the current
    agent's answer as if the other agent had written it.
    """
    stored = row["sender_agent_id"] or ""
    name = row["sender_name"] or ""
    session = row["sender_session"] or ""
    if (row["role"] or "") == "user":
        return {"sender_agent_id": stored, "sender_name": name, "sender_session": session,
                "reply_to_agent_id": "", "reply_to_name": "", "reply_to_session": ""}
    return {"sender_agent_id": "", "sender_name": "", "sender_session": "",
            "reply_to_agent_id": stored, "reply_to_name": name, "reply_to_session": session}


def last_message_head(*, agent_id: str, max_len: int = 80) -> dict[str, Any]:
    """One-line preview of the agent's most recent real non-tool message —
    for the agent-list overview, so the client needn't open each chat to see it.
    Strips <speak> voice tags and collapses whitespace. '' when nothing to show."""
    routine_origins = tuple(sorted(origins.ROUTINE_AUTOMATION_ORIGINS))
    routine_placeholders = ",".join("?" for _ in routine_origins)
    rows = conn().execute(
        f"""SELECT message_id, backend_session_id, revision, role, text, origin,
                     {_message_activity_sql()} AS message_activity
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
    return _preview_head(rows, max_len)


def _preview_head(rows, max_len: int) -> dict[str, Any]:
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
    return _format_preview(row, max_len)


def _format_preview(row, max_len: int) -> dict[str, Any]:
    if not row:
        return {"preview": "", "message_id": "", "revision": 0,
                "conversation_id": "", "activity": 0}
    raw_text = _display_text_for_message(
        role=row["role"], origin=row["origin"], text=row["text"],
    )
    text = clean_for_display(raw_text, oneline=True)
    if not text:
        return {"preview": "", "message_id": "", "revision": 0,
                "conversation_id": "", "activity": 0}
    preview = ("You: " if row["role"] == "user" else "") + text
    if len(preview) > max_len:
        preview = preview[: max_len - 1].rstrip() + "…"
    return {
        "preview": preview,
        "message_id": row["message_id"],
        "revision": int(row["revision"] or 0),
        "conversation_id": row["backend_session_id"] or "",
        "activity": int(row["message_activity"] or 0),
    }


def last_message_preview(*, agent_id: str, max_len: int = 80) -> str:
    return str(last_message_head(agent_id=agent_id, max_len=max_len)["preview"])


# Per-agent preview entries keyed by the agent's message revisions. A snapshot
# during quiet minutes reuses every entry; while agents stream, only the
# agents whose rows changed run their three indexed queries again.
_PREVIEW_CACHE: dict[str, tuple[tuple[tuple[str, int], ...], dict[str, Any]]] = {}


def _agent_preview(agent_id: str, max_len: int, routine: tuple[str, ...]) -> dict[str, Any]:
    """head, completed_head and activity for one agent via idx_messages_dashboard_activity."""
    marks = ','.join('?' for _ in routine)
    entry: dict[str, Any] = {}
    for field, completed_filter in (
        ('head', ''),
        ('completed_head', "AND NOT (m.role = 'assistant' AND COALESCE(m.source_file, '') LIKE 'live:%')"),
    ):
        rows = conn().execute(f"""
            SELECT m.agent_id, m.message_id, m.backend_session_id, m.revision, m.role, m.text, m.origin,
                   {_message_activity_sql()} AS message_activity
              FROM messages m
             WHERE m.agent_id = ?
               AND COALESCE(m.text, '') != '' AND COALESCE(m.tool_name, '') = ''
               {completed_filter}
               AND COALESCE(m.origin, 'user') NOT IN ({marks})
             ORDER BY {_message_activity_sql()} DESC, m.seq DESC, m.updated_at DESC
             LIMIT 50
        """, (agent_id, *routine))
        for row in rows:
            if not _automation_kind(role=row['role'], origin=row['origin'], text=row['text']):
                entry[field] = _format_preview(row, max_len)
                break
    row = conn().execute(f"""
        SELECT {_message_activity_sql()} AS activity FROM messages m
         WHERE m.agent_id = ?
           AND COALESCE(m.origin, 'user') = 'user'
           AND COALESCE(m.text, '') != '' AND COALESCE(m.tool_name, '') = ''
         ORDER BY {_message_activity_sql()} DESC, m.seq DESC, m.updated_at DESC
         LIMIT 1
    """, (agent_id,)).fetchone()
    entry['activity'] = int((row['activity'] if row else 0) or 0)
    # Date/order must describe the same visible message as the preview.
    # Prompt origin still controls scheduler engagement above.
    entry['chat_activity'] = int(entry.get('head', {}).get('activity', 0))
    return entry


def dashboard_messages(max_len: int = 80) -> dict[str, dict[str, Any]]:
    """The dashboard projection for every live agent.

    Revisions come first (one indexed GROUP BY); they key a per-agent cache so
    an unchanged agent costs nothing and a changed one costs three index walks
    that stop after at most 50 rows. The previous window-function query ranked
    every message of every live agent on each call (100–230 ms each, twice).
    """
    routine = tuple(sorted(origins.ROUTINE_AUTOMATION_ORIGINS))
    revisions: dict[str, dict[str, int]] = {}
    for row in conn().execute("""
        SELECT agent_id, backend_session_id, MAX(revision) AS revision FROM (
            SELECT agent_id, backend_session_id, revision FROM messages
            UNION ALL
            SELECT agent_id, backend_session_id, revision FROM conversation_heads
        ) WHERE agent_id IN (SELECT agent_id FROM agents WHERE deleted_at IS NULL)
        GROUP BY agent_id, backend_session_id
    """):
        revisions.setdefault(row['agent_id'], {})[row['backend_session_id']] = int(row['revision'] or 0)
    live = [row['agent_id'] for row in conn().execute("SELECT agent_id FROM agents WHERE deleted_at IS NULL")]
    result: dict[str, dict[str, Any]] = {}
    for agent_id in live:
        key = tuple(sorted((str(session or ''), rev) for session, rev in revisions.get(agent_id, {}).items()))
        cached = _PREVIEW_CACHE.get(agent_id)
        if cached is not None and cached[0] == key and max_len == 80:
            entry = dict(cached[1])
        else:
            entry = _agent_preview(agent_id, max_len, routine)
            if max_len == 80:
                _PREVIEW_CACHE[agent_id] = (key, dict(entry))
        if agent_id in revisions:
            entry['revisions'] = dict(revisions[agent_id])
        result[agent_id] = entry
    for stale in [agent_id for agent_id in _PREVIEW_CACHE if agent_id not in result]:
        _PREVIEW_CACHE.pop(stale, None)
    return result


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


def last_chat_message_activity(*, agent_id: str) -> int:
    """Semantic timestamp of the exact message chosen for the overview preview.

    A visible completion may originate from an automated continuation or a
    scheduled task. Its preview and date must agree regardless of that origin.
    The scheduler's user-engagement clock remains last_real_message_activity.
    """
    return int(last_message_head(agent_id=agent_id).get('activity', 0))


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
