"""Read-only projection of agent-to-agent exchanges as pair conversations.

Two agents that prompt each other share one conversation keyed by their stable
agent IDs, independent of direction, display names or retries. The projection
reads the canonical ``messages`` table; it never moves, copies or deletes a
transcript row and never relays text between agents. A recipient's answer is
shown with a reply marker and flagged private: it lived in that agent's own
chat and was only delivered onward if the agent sent a message itself.
"""
from __future__ import annotations

from typing import Any

from .avatar_urls import janitor_avatar_url, versioned_avatar_url
from .db import conn
from .voice_markup import clean_for_display, strip_hidden_blocks

PREFIX = "pair:"
MAX_LIMIT = 4999

_PAIR_WHERE = """COALESCE(m.origin, 'user') = 'agent'
              AND COALESCE(m.sender_agent_id, '') != ''
              AND m.sender_agent_id != m.agent_id
              AND COALESCE(m.text, '') != ''
              AND COALESCE(m.tool_name, '') = ''"""
_ACTIVITY = ("COALESCE(CAST((julianday(m.timestamp) - 2440587.5) * 86400000 AS INTEGER), "
             "m.updated_at)")


def is_pair_session(session: str) -> bool:
    return isinstance(session, str) and session.startswith(PREFIX)


def conversation_id(first_agent_id: str, second_agent_id: str) -> str:
    low, high = sorted((first_agent_id, second_agent_id))
    return f"{PREFIX}{low}:{high}"


def parse_conversation_id(value: str) -> tuple[str, str] | None:
    if not is_pair_session(value):
        return None
    parts = value[len(PREFIX):].split(":")
    if len(parts) != 2 or not all(parts) or parts[0] == parts[1]:
        return None
    low, high = sorted(parts)
    return low, high


def _participant(row) -> dict[str, Any]:
    return {
        "agent_id": row["agent_id"],
        "session": row["session"],
        "name": row["persona"],
        "avatar_url": janitor_avatar_url(bool(row["is_janitor"]))
        or versioned_avatar_url("/avatars", row["agent_id"], str(row["avatar_path"] or "")),
        "archived": row["archived_at"] is not None,
    }


def _participants(agent_ids: tuple[str, str]) -> list[dict[str, Any]] | None:
    rows = conn().execute(
        """SELECT agent_id, session, persona, avatar_path, is_janitor, archived_at
             FROM agents WHERE deleted_at IS NULL AND agent_id IN (?, ?)""",
        agent_ids).fetchall()
    found = {row["agent_id"]: _participant(row) for row in rows}
    if len(found) != 2:
        return None
    ordered = sorted(found.values(), key=lambda p: (p["name"].casefold(), p["agent_id"]))
    return ordered


def title_for(participants: list[dict[str, Any]]) -> str:
    return " & ".join(p["name"] for p in participants)


def _turn(row, agent_ids: tuple[str, str]) -> dict[str, Any]:
    incoming = row["role"] == "user"
    author_id = row["sender_agent_id"] if incoming else row["agent_id"]
    peer_id = agent_ids[1] if author_id == agent_ids[0] else agent_ids[0]
    text = strip_hidden_blocks(str(row["text"] or ""))
    turn = {
        "id": row["message_id"], "role": row["role"], "timestamp": row["timestamp"] or "",
        "text": text, "kind": row["kind"], "tool_name": None, "tools": [], "display_cells": [],
        "origin": "agent", "trace_id": row["trace_id"] or "", "revision": int(row["revision"]),
        "automated": False, "automation_kind": "",
        "sender_agent_id": author_id, "sender_name": row["author_name"] or "",
        "sender_session": row["author_session"] or "",
        "recipient_agent_id": row["agent_id"] if incoming else "",
        "reply_to_agent_id": "" if incoming else peer_id,
        "reply_to_name": "" if incoming else (row["peer_name"] or ""),
        "reply_to_session": "" if incoming else (row["peer_session"] or ""),
        # A sent row is a real delivery into the recipient's chat. An answer is
        # the recipient's private response; nothing here forwarded it onward.
        "delivery": "sent" if incoming else "private",
    }
    return turn


def _rows_sql(where_extra: str = "", order: str = "ASC") -> str:
    return f"""SELECT m.message_id, m.agent_id, m.role, m.timestamp, m.text, m.kind, m.revision,
                      m.sender_agent_id, m.trace_id, m.seq, {_ACTIVITY} AS activity,
                      author.persona AS author_name, author.session AS author_session,
                      peer.persona AS peer_name, peer.session AS peer_session
                 FROM messages m
                 JOIN agents author ON author.agent_id =
                      CASE WHEN m.role = 'user' THEN m.sender_agent_id ELSE m.agent_id END
                 JOIN agents peer ON peer.agent_id =
                      CASE WHEN m.role = 'user' THEN m.agent_id ELSE m.sender_agent_id END
                WHERE {_PAIR_WHERE}
                  AND m.role IN ('user', 'assistant')
                  AND ((m.agent_id = ? AND m.sender_agent_id = ?)
                       OR (m.agent_id = ? AND m.sender_agent_id = ?))
                  {where_extra}
                ORDER BY COALESCE(m.timestamp, '') {order}, m.seq {order}"""


def list_conversations(limit: int = 200) -> list[dict[str, Any]]:
    """Every agent pair with at least one delivered agent-origin message."""
    pairs = conn().execute(f"""
        SELECT MIN(m.agent_id, m.sender_agent_id) AS low, MAX(m.agent_id, m.sender_agent_id) AS high,
               COUNT(*) AS message_count, MAX(m.revision) AS latest_revision, MAX({_ACTIVITY}) AS latest_activity
          FROM messages m
          JOIN agents a ON a.agent_id = m.agent_id AND a.deleted_at IS NULL
          JOIN agents s ON s.agent_id = m.sender_agent_id AND s.deleted_at IS NULL
         WHERE {_PAIR_WHERE} AND m.role IN ('user', 'assistant')
           AND EXISTS (SELECT 1 FROM messages sent WHERE sent.role = 'user'
                        AND COALESCE(sent.origin, 'user') = 'agent'
                        AND ((sent.agent_id = m.agent_id AND sent.sender_agent_id = m.sender_agent_id)
                             OR (sent.agent_id = m.sender_agent_id AND sent.sender_agent_id = m.agent_id)))
         GROUP BY low, high
         ORDER BY latest_activity DESC
         LIMIT ?""", (max(1, min(int(limit), 1000)),)).fetchall()
    out = []
    for pair in pairs:
        agent_ids = (pair["low"], pair["high"])
        participants = _participants(agent_ids)
        if participants is None:
            continue
        latest = conn().execute(_rows_sql(order="DESC") + " LIMIT 1",
                                (agent_ids[0], agent_ids[1], agent_ids[1], agent_ids[0])).fetchone()
        preview = None
        if latest is not None:
            turn = _turn(latest, agent_ids)
            text = clean_for_display(turn["text"], oneline=True)
            if len(text) > 120:
                text = text[:119].rstrip() + "…"
            preview = {"message_id": turn["id"], "text": text, "timestamp": turn["timestamp"],
                       "sender_agent_id": turn["sender_agent_id"], "sender_name": turn["sender_name"],
                       "delivery": turn["delivery"], "revision": turn["revision"]}
        out.append({
            "conversation_id": conversation_id(*agent_ids),
            "agent_ids": list(agent_ids),
            "participants": participants,
            "title": title_for(participants),
            "message_count": int(pair["message_count"] or 0),
            "latest_revision": int(pair["latest_revision"] or 0),
            "latest_activity": int(pair["latest_activity"] or 0),
            "latest_message": preview,
        })
    return out


def load_timeline(session: str, *, after_revision: int = 0, before_message_id: str = "",
                  limit: int = 100) -> dict[str, Any]:
    """Merged both-direction timeline in the ``GET /log`` response shape."""
    limit = max(1, min(int(limit or 100), MAX_LIMIT))
    agent_ids = parse_conversation_id(session)
    participants = _participants(agent_ids) if agent_ids else None
    if agent_ids is None or participants is None:
        return {"cwd": "", "file": None, "turns": [], "missing": True, "latest_ts": "",
                "latest_revision": 0, "replace_required": False, "conversation_id": session,
                "has_more": False, "includes_automated": False, "participants": participants or [],
                "title": title_for(participants) if participants else ""}
    params: list[Any] = [agent_ids[0], agent_ids[1], agent_ids[1], agent_ids[0]]
    extra = ""
    if after_revision:
        extra = "AND m.revision > ?"
        params.append(int(after_revision))
    elif before_message_id:
        cursor = conn().execute(
            "SELECT COALESCE(timestamp, '') AS timestamp, seq FROM messages WHERE message_id = ?",
            (before_message_id,)).fetchone()
        if cursor is None:
            return {"cwd": "", "file": None, "turns": [], "missing": False, "latest_ts": "",
                    "latest_revision": 0, "replace_required": False, "conversation_id": session,
                    "has_more": False, "includes_automated": False, "participants": participants,
                    "title": title_for(participants)}
        extra = "AND (COALESCE(m.timestamp, '') < ? OR (COALESCE(m.timestamp, '') = ? AND m.seq < ?))"
        params.extend([cursor["timestamp"], cursor["timestamp"], int(cursor["seq"])])
    if after_revision:
        sql = f"""SELECT * FROM ({_rows_sql(extra, 'ASC')}) ORDER BY revision ASC, timestamp ASC, seq ASC LIMIT ?"""
    else:
        sql = f"""SELECT * FROM ({_rows_sql(extra, 'DESC')} LIMIT ?) ORDER BY COALESCE(timestamp, '') ASC, seq ASC"""
    params.append(limit + 1)
    rows = conn().execute(sql, tuple(params)).fetchall()
    turns = [_turn(row, agent_ids) for row in rows]
    has_more = len(turns) > limit
    if has_more:
        turns = turns[:limit] if after_revision else turns[-limit:]
    head = conn().execute(f"""SELECT MAX(m.revision) FROM messages m WHERE {_PAIR_WHERE}
        AND m.role IN ('user', 'assistant')
        AND ((m.agent_id = ? AND m.sender_agent_id = ?) OR (m.agent_id = ? AND m.sender_agent_id = ?))""",
        (agent_ids[0], agent_ids[1], agent_ids[1], agent_ids[0])).fetchone()[0] or 0
    revision = (max((turn["revision"] for turn in turns), default=after_revision)
                if has_more and after_revision else int(head))
    return {
        "cwd": "", "file": None, "turns": turns, "missing": False,
        "latest_ts": turns[-1]["timestamp"] if turns else "",
        "latest_revision": revision, "replace_required": False,
        "conversation_id": session, "has_more": has_more, "includes_automated": False,
        "participants": participants, "title": title_for(participants),
    }
