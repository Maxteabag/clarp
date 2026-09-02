"""SQLite-backed agent teams and speak-tag update fanout."""
from __future__ import annotations

import hashlib
import re
import secrets
from typing import Any

from . import origins
from .db import conn, now_ms
from .protocol import AgentState
from .voice_markup import clean_for_display


_HEARTBEAT_WAKE_STATE_KINDS = {
    AgentState.THINKING,
    AgentState.IDLE,
    AgentState.SPAWNED,
    AgentState.STOPPED,
    AgentState.DONE,
    AgentState.COMPACTING,
    AgentState.WAITING,
    AgentState.INTERRUPTED,
    AgentState.BACKGROUND,
}

_SPEAK_BLOCK_RE = re.compile(
    r"<speak\b[^>]*>(.*?)</speak>", re.IGNORECASE | re.DOTALL
)
_TEAM_BLOCK_RE = re.compile(
    r"<team\b[^>]*>(.*?)</team>", re.IGNORECASE | re.DOTALL
)
_RESIDUAL_TAG_RE = re.compile(r"<[^>]+>")


def _new_team_id() -> str:
    return "team-" + secrets.token_hex(6)


def _new_team_message_id(team_id: str, source_message_id: str, text: str) -> str:
    raw = f"{team_id}\0{source_message_id}\0{text}"
    return "tm-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


def _blocks_from(regex: re.Pattern[str], text: str | None) -> list[str]:
    if not text:
        return []
    out: list[str] = []
    for match in regex.finditer(text):
        cleaned = clean_for_display(match.group(1), oneline=True)
        cleaned = _RESIDUAL_TAG_RE.sub(" ", cleaned)
        cleaned = " ".join(cleaned.split())
        if cleaned:
            out.append(cleaned)
    return out


def extract_speak_blocks(text: str | None) -> list[str]:
    """Return display-clean spoken blocks from assistant markup."""
    return _blocks_from(_SPEAK_BLOCK_RE, text)


def extract_team_blocks(text: str | None) -> list[str]:
    """Return display-clean <team> broadcast blocks from assistant markup.

    The team feed is driven by <team>, NOT <speak>: what an agent tells User
    and what it tells its teammates are deliberately decoupled."""
    return _blocks_from(_TEAM_BLOCK_RE, text)


def create_team(name: str, *, color: str = "") -> dict[str, Any]:
    name = " ".join(str(name or "").split())
    if not name:
        raise ValueError("team name is required")
    team_id = _new_team_id()
    ts = now_ms()
    conn().execute(
        """INSERT INTO teams (team_id, name, color, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?)""",
        (team_id, name, color or "", ts, ts),
    )
    return get_team(team_id) or {
        "team_id": team_id, "name": name, "color": color or "",
        "created_at": ts, "updated_at": ts, "archived_at": None,
        "leader_agent_id": "", "nudge_enabled": True,
        "member_agent_ids": [], "latest_message": None, "unread_count": 0,
    }


def update_team(team_id: str, *, name: str | None = None,
                color: str | None = None, archived: bool | None = None
                ) -> dict[str, Any] | None:
    fields: list[str] = ["updated_at = ?"]
    values: list[Any] = [now_ms()]
    if name is not None:
        clean_name = " ".join(str(name).split())
        if not clean_name:
            raise ValueError("team name is required")
        fields.append("name = ?")
        values.append(clean_name)
    if color is not None:
        fields.append("color = ?")
        values.append(str(color or ""))
    if archived is not None:
        fields.append("archived_at = ?")
        values.append(now_ms() if archived else None)
    values.append(team_id)
    conn().execute(
        f"UPDATE teams SET {', '.join(fields)} WHERE team_id = ?",
        tuple(values),
    )
    return get_team(team_id)


def set_leader(team_id: str, agent_id: str | None) -> dict[str, Any] | None:
    """Designate (or clear, with falsy agent_id) a team's leader. The leader
    must be a member. A role, not a separate agent type."""
    agent_id = (agent_id or "").strip() or None
    if agent_id is not None:
        is_member = conn().execute(
            "SELECT 1 FROM team_members WHERE team_id = ? AND agent_id = ?",
            (team_id, agent_id),
        ).fetchone()
        if not is_member:
            raise ValueError("leader must be a member of the team")
    conn().execute(
        "UPDATE teams SET leader_agent_id = ?, updated_at = ? WHERE team_id = ?",
        (agent_id, now_ms(), team_id),
    )
    return get_team(team_id)


def set_nudge_enabled(team_id: str, enabled: bool) -> dict[str, Any] | None:
    """Enable/disable autonomous leader nudges for a team."""
    cur = conn().execute(
        "UPDATE teams SET nudge_enabled = ?, updated_at = ? WHERE team_id = ?",
        (1 if enabled else 0, now_ms(), team_id),
    )
    if cur.rowcount == 0:
        return None
    return get_team(team_id)


def get_leader(team_id: str) -> str:
    row = conn().execute(
        "SELECT leader_agent_id FROM teams WHERE team_id = ?", (team_id,),
    ).fetchone()
    return (row and row["leader_agent_id"]) or ""


def get_team(team_id: str) -> dict[str, Any] | None:
    rows = list_teams(include_archived=True)
    return next((team for team in rows if team["team_id"] == team_id), None)


def list_teams(*, include_archived: bool = False) -> list[dict[str, Any]]:
    where = "" if include_archived else "WHERE t.archived_at IS NULL"
    rows = conn().execute(f"""
        SELECT t.team_id, t.name, t.color, t.created_at, t.updated_at,
               t.archived_at, t.leader_agent_id, t.nudge_enabled,
               (
                 SELECT json_group_array(tm.agent_id)
                   FROM team_members tm
                  WHERE tm.team_id = t.team_id
                  ORDER BY tm.position, tm.added_at
               ) AS member_agent_ids_json,
               (
                 SELECT msg.text
                   FROM team_messages msg
                  WHERE msg.team_id = t.team_id
                  ORDER BY msg.created_at DESC
                  LIMIT 1
               ) AS latest_message,
               (
                 SELECT msg.created_at
                   FROM team_messages msg
                  WHERE msg.team_id = t.team_id
                  ORDER BY msg.created_at DESC
                  LIMIT 1
               ) AS latest_message_at,
               (
                 SELECT COUNT(*)
                   FROM team_inbox inbox
                   JOIN team_messages msg
                     ON msg.team_message_id = inbox.team_message_id
                  WHERE msg.team_id = t.team_id
                    AND inbox.status = 'unread'
               ) AS unread_count
          FROM teams t
          {where}
         ORDER BY COALESCE(latest_message_at, t.updated_at) DESC, t.name ASC
    """).fetchall()
    return [_team_payload(row) for row in rows]


def _team_payload(row) -> dict[str, Any]:
    import json

    try:
        members = json.loads(row["member_agent_ids_json"] or "[]")
    except json.JSONDecodeError:
        members = []
    return {
        "team_id": row["team_id"],
        "name": row["name"],
        "color": row["color"] or "",
        "created_at": int(row["created_at"] or 0),
        "updated_at": int(row["updated_at"] or 0),
        "archived_at": row["archived_at"],
        "leader_agent_id": row["leader_agent_id"] or "",
        "nudge_enabled": bool(row["nudge_enabled"]),
        "member_agent_ids": [str(m) for m in members if m],
        "latest_message": row["latest_message"] or "",
        "latest_message_at": row["latest_message_at"],
        "unread_count": int(row["unread_count"] or 0),
    }


def add_member(team_id: str, agent_id: str) -> bool:
    if not _team_exists(team_id) or not _agent_exists(agent_id):
        return False
    row = conn().execute(
        "SELECT COALESCE(MAX(position), -1) + 1 AS pos FROM team_members WHERE team_id = ?",
        (team_id,),
    ).fetchone()
    conn().execute(
        """INSERT INTO team_members (team_id, agent_id, position, added_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(team_id, agent_id) DO NOTHING""",
        (team_id, agent_id, int(row["pos"] or 0), now_ms()),
    )
    return True


def remove_member(team_id: str, agent_id: str) -> None:
    c = conn()
    c.execute(
        "DELETE FROM team_members WHERE team_id = ? AND agent_id = ?",
        (team_id, agent_id),
    )
    # Clear this agent's inbox for the team. Leaving (or being removed) must
    # stop further digest injection, and a later re-add must not resurface the
    # old unread backlog — that was the "still getting team context after I was
    # removed" bug.
    c.execute(
        """DELETE FROM team_inbox
            WHERE agent_id = ?
              AND team_message_id IN (
                  SELECT team_message_id FROM team_messages WHERE team_id = ?
              )""",
        (agent_id, team_id),
    )


def delete_team(team_id: str) -> bool:
    """Hard-delete a team and everything attached to it: inbox rows, messages,
    memberships, then the team row. Works on archived teams too. Returns False
    if the team didn't exist."""
    c = conn()
    exists = c.execute(
        "SELECT 1 FROM teams WHERE team_id = ?", (team_id,),
    ).fetchone()
    if exists is None:
        return False
    c.execute(
        """DELETE FROM team_inbox
            WHERE team_message_id IN (
                SELECT team_message_id FROM team_messages WHERE team_id = ?
            )""",
        (team_id,),
    )
    c.execute("DELETE FROM team_messages WHERE team_id = ?", (team_id,))
    c.execute("DELETE FROM team_members WHERE team_id = ?", (team_id,))
    c.execute("DELETE FROM teams WHERE team_id = ?", (team_id,))
    return True


def teams_for_agent(agent_id: str) -> list[dict[str, Any]]:
    rows = conn().execute("""
        SELECT t.team_id, t.name, t.color, t.created_at, t.updated_at,
               t.archived_at, t.leader_agent_id
          FROM teams t
          JOIN team_members tm ON tm.team_id = t.team_id
         WHERE tm.agent_id = ? AND t.archived_at IS NULL
         ORDER BY t.name ASC
    """, (agent_id,)).fetchall()
    return [dict(row) for row in rows]


AUTOMATION_LEADER_ORIGINS = frozenset({"heartbeat", "leader_tick"})


def team_protocol_instruction(agent_id: str, *, turn_origin: str = "") -> str:
    """Standing per-turn team brief. Empty for solo agents."""
    teams = teams_for_agent(agent_id)
    if not teams:
        return ""
    names = ", ".join(t["name"] for t in teams)
    led = [t for t in teams if (t.get("leader_agent_id") or "") == agent_id]
    if led and turn_origin in AUTOMATION_LEADER_ORIGINS:
        return _lean_leader_section(agent_id, led, turn_origin=turn_origin)
    brief = (
        f"Team feed: you are on {names}. Teammates share private updates here, "
        "separate from the user's conversation.\n"
        "- Post useful coordination as <team>short first-person update</team>; "
        "it is not spoken, and teammates never see <speak>.\n"
        "- Broadcast starts, finishes, blockers, handoffs, and decisions that "
        "affect others. Keep it short; skip routine noise.\n"
        "- Recent team updates are injected below when present. Read them and "
        "coordinate; do not fetch them yourself."
    )
    direct_report = _direct_report_section(agent_id, teams)
    if direct_report:
        brief = brief + "\n" + direct_report
    if led:
        brief = brief + "\n\n" + _leader_section(agent_id, led)
    return brief


def _direct_report_section(agent_id: str, teams: list[dict[str, Any]]) -> str:
    """Worker-only direct-report rule for the first led team."""
    from . import agents as agents_db

    team = next(
        (t for t in teams if (t.get("leader_agent_id") or "")
         and (t.get("leader_agent_id") or "") != agent_id),
        None,
    )
    if not team:
        return ""
    leader_id = team.get("leader_agent_id") or ""
    leader = agents_db.get_by_agent_id(leader_id) or {}
    leader_session = (leader.get("session") or "").strip()
    if not leader_session:
        return ""
    me = agents_db.get_by_agent_id(agent_id) or {}
    my_session = (me.get("session") or "").strip() or "<your_session>"
    leader_name = (leader.get("persona") or leader_session).strip()
    return (
        f"- Report TERMINAL results directly to leader {leader_name} via "
        f"self-prompt --from {my_session} --to {leader_session} "
        "(done+proof, blocked+need, or failed). Use <team> for progress, "
        "handoffs, file touches, and awareness; if a finish affects teammates, "
        "do both. Rule: leader needs to act -> direct; awareness -> <team>."
    )


def _leader_section(agent_id: str, led_teams: list[dict[str, Any]]) -> str:
    """Leader brief plus live member state."""
    from . import agents as agents_db
    from . import leader_memory
    me = agents_db.get_by_agent_id(agent_id) or {}
    my_session = me.get("session", "")
    out = [
        "Leader role: decide, delegate, track, and learn; workers execute.",
        f"Delegate as yourself with self-prompt --from {my_session}. Track "
        "owner, objective, proof, risk, time budget, and terminal result.",
        leader_memory.leader_context_instruction(leader_session=my_session),
    ]
    for t in led_teams:
        rows = conn().execute(
            """SELECT agent_id FROM team_members
                WHERE team_id = ? ORDER BY position, added_at""",
            (t["team_id"],),
        ).fetchall()
        statuses: list[str] = []
        for r in rows:
            mid = r["agent_id"]
            if mid == agent_id:
                continue
            m = agents_db.get_by_agent_id(mid) or {}
            st = agents_db.latest_state(mid) or {}
            statuses.append(
                f"  - {m.get('persona') or mid} "
                f"({m.get('session') or '?'}): {st.get('kind') or 'unknown'}"
            )
        if statuses:
            out.append(f"Team '{t['name']}' members and their current state:")
            out.extend(statuses)
    return "\n".join(out)


def _lean_leader_section(
    agent_id: str,
    led_teams: list[dict[str, Any]],
    *,
    turn_origin: str = "",
) -> str:
    from . import agents as agents_db

    reminder = (
        "You lead this team; decide/delegate/track. "
        "Reply LEADER_NOOP if nothing needs action."
    )
    if turn_origin == "heartbeat":
        reminder = (
            "You lead this team; decide/delegate/track. "
            "Reply HEARTBEAT_OK if nothing needs action."
        )
    out = [reminder]
    for t in led_teams:
        out.append(f"Team '{t['name']}' live member states:")
        rows = conn().execute(
            """SELECT agent_id FROM team_members
                WHERE team_id = ? ORDER BY position, added_at""",
            (t["team_id"],),
        ).fetchall()
        for r in rows:
            mid = r["agent_id"]
            if mid == agent_id:
                continue
            m = agents_db.get_by_agent_id(mid) or {}
            st = agents_db.latest_state(mid) or {}
            out.append(
                f"- {m.get('persona') or mid} "
                f"({m.get('session') or '?'}): {st.get('kind') or 'unknown'}"
            )
    return "\n".join(out)


def capture_assistant_message(*, agent_id: str, source_message_id: str,
                              trace_id: str = "", text: str = "") -> int:
    blocks = extract_team_blocks(text)
    if not blocks:
        return 0
    teams = teams_for_agent(agent_id)
    if not teams:
        return 0
    inserted = 0
    ts = now_ms()
    c = conn()
    for team in teams:
        team_id = team["team_id"]
        members = [
            row["agent_id"] for row in c.execute(
                "SELECT agent_id FROM team_members WHERE team_id = ?",
                (team_id,),
            ).fetchall()
        ]
        for block in blocks:
            team_message_id = _new_team_message_id(
                team_id, source_message_id, block
            )
            cur = c.execute(
                """INSERT INTO team_messages (
                       team_message_id, team_id, source_agent_id,
                       source_message_id, trace_id, text, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(team_id, source_message_id, text) DO NOTHING""",
                (
                    team_message_id, team_id, agent_id, source_message_id,
                    trace_id or "", block, ts,
                ),
            )
            if cur.rowcount <= 0:
                continue
            inserted += 1
            for member_id in members:
                if member_id == agent_id:
                    continue
                c.execute(
                    """INSERT INTO team_inbox (
                           team_message_id, agent_id, status
                       ) VALUES (?, ?, 'unread')
                       ON CONFLICT(team_message_id, agent_id) DO NOTHING""",
                    (team_message_id, member_id),
                )
    return inserted


def list_team_messages(team_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit or 100), 500))
    rows = conn().execute("""
        SELECT msg.team_message_id, msg.team_id, msg.source_agent_id,
               msg.source_message_id, msg.trace_id, msg.text, msg.created_at,
               a.persona AS source_name, a.session AS source_session
          FROM team_messages msg
          LEFT JOIN agents a ON a.agent_id = msg.source_agent_id
         WHERE msg.team_id = ?
         ORDER BY msg.created_at DESC, msg.team_message_id DESC
         LIMIT ?
    """, (team_id, limit)).fetchall()
    return [_message_payload(row) for row in reversed(rows)]


def _message_payload(row) -> dict[str, Any]:
    return {
        "team_message_id": row["team_message_id"],
        "team_id": row["team_id"],
        "source_agent_id": row["source_agent_id"],
        "source_message_id": row["source_message_id"],
        "trace_id": row["trace_id"] or "",
        "text": row["text"] or "",
        "created_at": int(row["created_at"] or 0),
        "source_name": row["source_name"] or "",
        "source_session": row["source_session"] or "",
    }


def pending_digest(agent_id: str, *, limit: int = 5) -> tuple[str, list[str]]:
    """Build the per-turn team digest for an agent.

    Bounded by design so a team can grow to thousands of messages without ever
    flooding a turn: only the most recent `limit` unread updates are surfaced
    (recent matters most), but the *entire* unread backlog is drained in one
    turn — the returned id list covers every unread message, shown or not, so
    the caller marks them all injected. A behind agent never crawls through
    ancient backchat five-at-a-time; it sees the latest handful plus a count of
    what it skipped, and starts clean next turn.
    """
    limit = max(1, min(int(limit or 5), 20))
    c = conn()
    # The whole unread backlog (non-archived teams) — every id we drain.
    all_ids = [
        row["team_message_id"] for row in c.execute("""
            SELECT inbox.team_message_id
              FROM team_inbox inbox
              JOIN team_messages msg ON msg.team_message_id = inbox.team_message_id
              JOIN teams t ON t.team_id = msg.team_id
             WHERE inbox.agent_id = ?
               AND inbox.status = 'unread'
               AND t.archived_at IS NULL
        """, (agent_id,)).fetchall()
    ]
    if not all_ids:
        return "", []
    # The most recent `limit`, with text — selected newest-first, shown oldest
    # to newest so the snippet reads chronologically.
    rows = list(reversed(c.execute("""
        SELECT msg.text, t.name AS team_name, a.persona AS source_name
          FROM team_inbox inbox
          JOIN team_messages msg ON msg.team_message_id = inbox.team_message_id
          JOIN teams t ON t.team_id = msg.team_id
          LEFT JOIN agents a ON a.agent_id = msg.source_agent_id
         WHERE inbox.agent_id = ?
           AND inbox.status = 'unread'
           AND t.archived_at IS NULL
         ORDER BY msg.created_at DESC, msg.rowid DESC
         LIMIT ?
    """, (agent_id, limit)).fetchall()))
    skipped = len(all_ids) - len(rows)
    lines = ["Team updates since your last turn:"]
    if skipped > 0:
        lines.append(
            f"(+{skipped} earlier update{'s' if skipped != 1 else ''} skipped — "
            f"showing the {len(rows)} most recent)"
        )
    for row in rows:
        source = row["source_name"] or "A teammate"
        team = row["team_name"] or "Team"
        lines.append(f"- [{team}] {source}: {row['text']}")
    lines.extend([
        "",
        ("This is background awareness. Keep broadcasting your OWN progress with "
         "<team> as you work — that's expected. Just don't get pulled into "
         "back-and-forth replies to teammates unless the user asks or it directly "
         "affects your current task."),
    ])
    return "\n".join(lines), all_ids


def mark_injected(agent_id: str, team_message_ids: list[str] | tuple[str, ...]) -> None:
    if not team_message_ids:
        return
    ts = now_ms()
    conn().executemany(
        """UPDATE team_inbox
              SET status = 'injected', injected_at = ?
            WHERE agent_id = ? AND team_message_id = ? AND status = 'unread'""",
        [(ts, agent_id, msg_id) for msg_id in team_message_ids],
    )


def latest_activity_for_agent(agent_id: str) -> int:
    """Latest team-level activity that should wake a dormant heartbeat.

    Team broadcasts, delivered/injected team prompts, and teammate state
    changes all mean the agent's world changed. The agent's own state changes
    are excluded so a heartbeat cannot wake itself.
    """
    state_placeholders = ",".join("?" for _ in _HEARTBEAT_WAKE_STATE_KINDS)
    routine_placeholders = ",".join("?" for _ in origins.ROUTINE_AUTOMATION_ORIGINS)
    row = conn().execute(
        f"""
        SELECT MAX(ts) AS ts
          FROM (
                SELECT MAX(msg.created_at) AS ts
                  FROM team_members mine
                  JOIN teams t ON t.team_id = mine.team_id
                  JOIN team_messages msg ON msg.team_id = mine.team_id
                 WHERE mine.agent_id = ?
                   AND t.archived_at IS NULL
                UNION ALL
                SELECT MAX(COALESCE(inbox.read_at, inbox.injected_at, msg.created_at)) AS ts
                  FROM team_inbox inbox
                  JOIN team_messages msg
                    ON msg.team_message_id = inbox.team_message_id
                  JOIN teams t ON t.team_id = msg.team_id
                 WHERE inbox.agent_id = ?
                   AND t.archived_at IS NULL
                UNION ALL
                SELECT MAX(st.ts) AS ts
                  FROM team_members mine
                  JOIN teams t ON t.team_id = mine.team_id
                  JOIN team_members peer ON peer.team_id = mine.team_id
                  JOIN state_log st ON st.agent_id = peer.agent_id
                 WHERE mine.agent_id = ?
                   AND peer.agent_id != mine.agent_id
                   AND st.kind IN ({state_placeholders})
                   AND COALESCE(json_extract(st.detail, '$.origin'), '')
                       NOT IN ({routine_placeholders})
                   AND t.archived_at IS NULL
          )
        """,
        (
            agent_id,
            agent_id,
            agent_id,
            *_HEARTBEAT_WAKE_STATE_KINDS,
            *origins.ROUTINE_AUTOMATION_ORIGINS,
        ),
    ).fetchone()
    return int((row and row["ts"]) or 0)


def memberships_by_agent() -> dict[str, list[str]]:
    rows = conn().execute("""
        SELECT tm.agent_id, tm.team_id
          FROM team_members tm
          JOIN teams t ON t.team_id = tm.team_id
         WHERE t.archived_at IS NULL
         ORDER BY tm.position, tm.added_at
    """).fetchall()
    out: dict[str, list[str]] = {}
    for row in rows:
        out.setdefault(row["agent_id"], []).append(row["team_id"])
    return out


def _team_exists(team_id: str) -> bool:
    return conn().execute(
        "SELECT 1 FROM teams WHERE team_id = ? AND archived_at IS NULL",
        (team_id,),
    ).fetchone() is not None


def _agent_exists(agent_id: str) -> bool:
    return conn().execute(
        "SELECT 1 FROM agents WHERE agent_id = ? AND deleted_at IS NULL",
        (agent_id,),
    ).fetchone() is not None
