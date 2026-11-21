"""Read-side projections over `messages`.

`/log` listing, provenance fields, one-line previews, the dashboard batch
projection, activity clocks and revision watermarks.
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from . import db as _db
from .clock import iso_from_ms
from .db import change_stamp, conn
from . import coordination, origins, team_leader
from . import log as cache_log
from .revisioned_cache import RevisionedCache
from .voice_markup import clean_for_display
from .message_context import _automation_kind, _display_text_for_message
from .message_writes import _message_activity_sql

if TYPE_CHECKING:
    from .identity import AgentRef


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


class UnknownHistoryCursor(LookupError):
    """An older-history request named a message this conversation does not
    store. Answering with an empty page would tell the client the history
    has ended, so callers report it as a conflict instead."""


def list_messages(*, agent_id: str, backend_session_id: str = "",
                  after_revision: int = 0,
                  before_message_id: str = "",
                  limit: int = 100,
                  include_automated: bool = True,
                  extra_backend_session_id: str = "",
                  hide_coordination: bool = False) -> list[dict[str, Any]]:
    params: list[Any] = [agent_id]
    where = "m.agent_id = ?"
    sessions = list(dict.fromkeys(v for v in (backend_session_id, extra_backend_session_id) if v))
    if sessions:
        where += " AND m.backend_session_id IN (" + ",".join("?" for _ in sessions) + ")"
        params.extend(sessions)
    if after_revision:
        where += " AND m.revision > ?"
        params.append(int(after_revision))
    if before_message_id:
        cursor_scope = " AND backend_session_id IN (" + ",".join("?" for _ in sessions) + ")" if sessions else ""
        cursor = conn().execute(
            "SELECT COALESCE(timestamp, '') AS timestamp,seq,message_id FROM messages WHERE message_id=? AND agent_id=?" + cursor_scope,
            (before_message_id, agent_id, *sessions),
        ).fetchone()
        if cursor is None:
            raise UnknownHistoryCursor(before_message_id)
        # The plain `timestamp <= ?` in front lets SQLite read idx_messages_log_order as a range
        # that starts at the cursor; the OR of the three tie cases alone cannot, and an older page
        # then walked down from the newest row, costing more the further back it was.
        where += """ AND COALESCE(m.timestamp, '') <= ? AND (
            COALESCE(m.timestamp, '') < ?
            OR (COALESCE(m.timestamp, '') = ? AND m.seq < ?)
            OR (COALESCE(m.timestamp, '') = ? AND m.seq = ? AND m.message_id < ?)
        )"""
        params.extend([cursor["timestamp"], cursor["timestamp"], cursor["timestamp"], int(cursor["seq"]),
                       cursor["timestamp"], int(cursor["seq"]), cursor["message_id"]])
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
    if hide_coordination and coordination.can_leave_note(agent_id):
        # Before LIMIT, as the automated filter: a page of N is N rows the user sees.
        where += " AND NOT " + coordination.HIDDEN_ROW_SQL
    params.append(max(1, min(limit, 5000)))
    # Display order is by timestamp (tie-break seq). User rows live in a
    # negative seq band and transcript rows in 0..N-1, so ordering by seq would
    # be wrong; timestamp interleaves the client-recorded user turns with the
    # transcript's assistant turns correctly and removes any seq-collision
    # coupling between the two sources.
    if after_revision:
        order = "m.revision ASC, COALESCE(m.timestamp, '') ASC, m.seq ASC, m.message_id ASC"
        query = f"""SELECT m.message_id, m.role, m.timestamp, m.text, m.kind,
                           m.tool_name, m.tools_json, m.display_cells_json,
                           m.revision, m.origin, m.sender_agent_id, m.trace_id,
                           m.phase, m.turn_json, m.sent_at,
                           sender.persona AS sender_name,
                           sender.session AS sender_session,
                           {coordination.TRACE_AUDIENCE_SQL} AS trace_audience
                      FROM messages m
                      LEFT JOIN agents sender
                        ON sender.agent_id = m.sender_agent_id
                      LEFT JOIN coordination_traces ct
                        ON ct.agent_id = m.agent_id AND ct.trace_id = m.trace_id
                     WHERE {where}
                     ORDER BY {order}
                     LIMIT ?"""
    else:
        query = f"""SELECT m.message_id, m.role, m.timestamp, m.text, m.kind,
                           m.tool_name, m.tools_json, m.display_cells_json,
                           m.revision, m.origin, m.sender_agent_id, m.trace_id,
                           m.phase, m.turn_json, m.sent_at,
                           sender.persona AS sender_name,
                           sender.session AS sender_session,
                           {coordination.TRACE_AUDIENCE_SQL} AS trace_audience
                      FROM (
                            SELECT message_id, agent_id, role, timestamp, text, kind,
                                   tool_name, tools_json, display_cells_json,
                                   seq, revision, origin, sender_agent_id, trace_id,
                                   phase, turn_json, sent_at
                              FROM messages m
                             WHERE {where}
                             ORDER BY COALESCE(timestamp, '') DESC, seq DESC, message_id DESC
                             LIMIT ?
                      ) m
                      LEFT JOIN agents sender
                        ON sender.agent_id = m.sender_agent_id
                      LEFT JOIN coordination_traces ct
                        ON ct.agent_id = m.agent_id AND ct.trace_id = m.trace_id
                     ORDER BY COALESCE(m.timestamp, '') ASC, m.seq ASC, m.message_id ASC"""
    rows = conn().execute(query, tuple(params)).fetchall()
    # An agent that cannot leave a note is never hidden (lib.coordination, F6-b).
    hideable = coordination.can_leave_note(agent_id)
    out: list[dict[str, Any]] = []
    for row in rows:
        automation_kind = _automation_kind(
            role=row["role"], origin=row["origin"], text=row["text"],
        )
        display_text = _display_text_for_message(
            role=row["role"], origin=row["origin"], text=row["text"],
            message_id=row["message_id"],
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
            # 'agents': a row of a turn another agent's message started
            # (lib.coordination); every other row, and a note, is 'user'.
            "audience": coordination.row_audience(
                kind=row["kind"], trace_audience=row["trace_audience"] if hideable else None),
            "automated": bool(automation_kind),
            "automation_kind": automation_kind,
            "phase": row["phase"],
            # The settled turn's summary, on its last row only
            # (docs/live-items.md §9).
            **_turn_field(row["turn_json"]),
            # A user message that waited for a running turn: when it was sent.
            # Its timestamp is when its own turn started (issue 491).
            **({"sent_at": iso_from_ms(int(row["sent_at"]))}
               if row["sent_at"] is not None else {}),
        })
    return out


# ---- coordination hidden from the chat: where it was, and how much (issue 478) -------------

_KEY_SQL = "COALESCE(m.timestamp, '')"


def _key_cmp(op: str, key: tuple) -> tuple[str, list]:
    """Rows strictly ``op`` ('<' or '>') the display key (timestamp, seq, message_id).

    The plain ``timestamp {op}= ?`` in front is what lets SQLite read idx_messages_log_order as
    a range: an OR of the three cases alone cannot use it, and every look-back or count then
    walked the whole conversation (300 ms a page on 300k rows)."""
    t, seq, mid = key
    return (f"""({_KEY_SQL} {op}= ? AND ({_KEY_SQL} {op} ? OR ({_KEY_SQL} = ? AND m.seq {op} ?)
               OR ({_KEY_SQL} = ? AND m.seq = ? AND m.message_id {op} ?)))""",
            [t, t, t, seq, t, seq, mid])


def _automated_sql() -> tuple[str, list]:
    return ("""NOT (COALESCE(m.origin, 'user') IN ('watcher', 'heartbeat', 'leader_tick', 'dreaming')
               OR (m.role = 'user' AND COALESCE(m.origin, 'user') = 'schedule'
                   AND TRIM(COALESCE(m.text, '')) = ?))""", [team_leader.TICK_PROMPT])


def coordination_gaps(*, agent_id: str, sessions: list[str], rows: list[dict[str, Any]],
                      include_automated: bool, contiguous: bool,
                      tail: bool) -> tuple[dict[str, dict[str, Any]], dict[str, Any] | None]:
    """Where hidden coordination stood among the rows a page gives, so a client draws the
    separator where the exchange happened: {row id: hidden_before} counted from the previous
    VISIBLE row of the chat (not of the page), and hidden_after for what follows the chat's
    newest visible row (on a tail page or a delta; None otherwise). Each count is
    {messages, exchanges, agents: [{agent_id, name}], capped}: it reads at most
    coordination.COUNT_CAP messages (COUNT_ROW_CAP rows) of the run, nearest the row it belongs
    to, and says ``capped`` when the run goes on, so no count waits on a long run. Counted from
    the row, the same run gives the same count on the same row whatever the page size."""
    database = conn()
    scope = "m.agent_id = ?" + (
        " AND m.backend_session_id IN (" + ",".join("?" for _ in sessions) + ")" if sessions else "")
    scope_params: list[Any] = [agent_id, *sessions]
    hidden_sql = coordination.HIDDEN_ROW_SQL
    visible = "NOT " + hidden_sql
    counted_sql = f"({hidden_sql} AND {coordination.COUNTED_SQL})"
    visible_params: list[Any] = []
    if not include_automated:
        sql, extra = _automated_sql()
        visible += " AND " + sql
        visible_params += extra

    def previous_visible(key: tuple | None) -> tuple[tuple | None, bool]:
        """The previous visible row before ``key`` (None: the chat's end), looking back at most
        COUNT_ROW_CAP + 1 rows of any kind by the display-order index: (its key, False). When none
        of those is visible, (the farthest of them, True): the count reads no further than that
        anyway and says capped. (None, False) when the chat starts within them. Without the bound
        a stretch with no visible row was walked to the chat's start, judging every row (a delta
        poll on a mostly-coordination chat: 356 ms of counts at 300k rows; the review's weigh point)."""
        where, params = scope, list(scope_params)
        if key is not None:
            cmp_sql, cmp_params = _key_cmp("<", key)
            where, params = where + " AND " + cmp_sql, params + cmp_params
        rows = database.execute(
            f"""SELECT {_KEY_SQL} AS t, m.seq, m.message_id, CASE WHEN {visible} THEN 1 ELSE 0 END AS shown
                  FROM messages m WHERE {where}
                 ORDER BY {_KEY_SQL} DESC, m.seq DESC, m.message_id DESC LIMIT ?""",
            [*visible_params, *params, coordination.COUNT_ROW_CAP + 1]).fetchall()
        for row in rows:
            if row["shown"]:
                return (row["t"], int(row["seq"]), row["message_id"]), False
        if len(rows) > coordination.COUNT_ROW_CAP:
            far = rows[-1]
            return (far["t"], int(far["seq"]), far["message_id"]), True
        return None, False

    def run(low: tuple | None, high: tuple | None, *, from_high: bool) -> dict[str, Any] | None:
        """The hidden rows strictly between two keys (None: open), read nearest ``high`` (or
        ``low``) first and at most up to the caps."""
        where, params = f"{scope} AND {hidden_sql}", list(scope_params)
        if low is not None:
            cmp_sql, cmp_params = _key_cmp(">", low)
            where, params = where + " AND " + cmp_sql, params + cmp_params
        if high is not None:
            cmp_sql, cmp_params = _key_cmp("<", high)
            where, params = where + " AND " + cmp_sql, params + cmp_params
        direction = "DESC" if from_high else "ASC"
        found = database.execute(
            f"""SELECT {_KEY_SQL} AS t, m.seq, m.message_id, m.trace_id,
                       CASE WHEN {counted_sql} THEN 1 ELSE 0 END AS counted
                  FROM messages m WHERE {where}
                 ORDER BY {_KEY_SQL} {direction}, m.seq {direction}, m.message_id {direction}
                 LIMIT ?""", [*params, coordination.COUNT_ROW_CAP + 1]).fetchall()
        if not found:
            return None
        # ``capped`` says that another MESSAGE was left out beyond what is counted: a run of
        # exactly COUNT_CAP messages followed by wordless rows is not "100+".
        messages, traces, capped, last = 0, [], False, None
        for r in found[:coordination.COUNT_ROW_CAP]:
            if r["counted"] and messages == coordination.COUNT_CAP:
                capped = True
                break
            messages += int(r["counted"])
            if r["trace_id"] not in traces:
                traces.append(r["trace_id"])
            last = r
        else:
            if len(found) > coordination.COUNT_ROW_CAP:
                # The row cap stopped the reading: capped if a message is among the rows beyond.
                beyond_sql, beyond_params = _key_cmp("<" if from_high else ">",
                                                     (last["t"], int(last["seq"]), last["message_id"]))
                capped = database.execute(
                    f"SELECT 1 FROM messages m WHERE {where} AND {counted_sql} AND {beyond_sql} LIMIT 1",
                    [*params, *beyond_params]).fetchone() is not None
        senders: list[str] = []
        for start in range(0, len(traces), 500):
            chunk = traces[start:start + 500]
            for r in database.execute(
                    "SELECT trace_id, sender_agent_id FROM coordination_traces WHERE agent_id = ? AND trace_id IN ("
                    + ",".join("?" for _ in chunk) + ")", [agent_id, *chunk]):
                if r["sender_agent_id"] and r["sender_agent_id"] not in senders:
                    senders.append(r["sender_agent_id"])
        names = {r["agent_id"]: r["persona"] for r in database.execute(
            "SELECT agent_id, persona FROM agents WHERE agent_id IN (" + ",".join("?" for _ in senders) + ")",
            senders)} if senders else {}
        return {"messages": messages, "exchanges": len(traces),
                "agents": [{"agent_id": a, "name": names.get(a, "")} for a in senders],
                "capped": capped}

    ids = [row["id"] for row in rows]
    keys: dict[str, tuple] = {}
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        for r in database.execute(
                f"SELECT message_id, COALESCE(timestamp, '') AS t, seq FROM messages WHERE message_id IN ("
                + ",".join("?" for _ in chunk) + ")", chunk):
            keys[r["message_id"]] = (r["t"], int(r["seq"]), r["message_id"])
    ordered = sorted((keys[i] for i in ids if i in keys))
    busy = _gaps_with_rows(database, scope, scope_params, ordered)
    before: dict[str, dict[str, Any]] = {}
    after_tail: dict[str, Any] | None = None
    for index, key in enumerate(ordered):
        if index and busy is not None and index not in busy:
            continue        # no row at all between this row and the one before it: nothing was left out
        # In a contiguous page the previous row of the page is the previous visible row of the chat.
        low, cut = (ordered[index - 1], False) if contiguous and index else previous_visible(key)
        if c := run(low, key, from_high=True):
            c["capped"] = c["capped"] or cut
            before[key[2]] = c
    if tail or not contiguous:
        newest, cut = (ordered[-1], False) if contiguous and ordered else previous_visible(None)
        after_tail = run(newest, None, from_high=False)
        if after_tail is not None:
            after_tail["capped"] = after_tail["capped"] or cut
    return before, after_tail


# How many rows beyond the page's own one read may look at to tell which gaps hold rows.
GAP_SCAN_SLACK = 2000


def _gaps_with_rows(database, scope: str, scope_params: list[Any], ordered: list[tuple]) -> set[int] | None:
    """The indexes i of ``ordered`` (the page's rows, in display order) with some other row of the
    chat between ordered[i-1] and ordered[i]: only those gaps can hold rows that were left out, so
    an ordinary chat read under 'hide' costs one read of its page's keys, not a count per row.
    None when there are more than GAP_SCAN_SLACK such rows (every gap is then counted)."""
    if len(ordered) < 2:
        return None
    low_sql, low_params = _key_cmp(">", ordered[0])
    high_sql, high_params = _key_cmp("<", ordered[-1])
    limit = len(ordered) + GAP_SCAN_SLACK
    found = database.execute(
        f"""SELECT {_KEY_SQL} AS t, m.seq, m.message_id FROM messages m
             WHERE {scope} AND {low_sql} AND {high_sql}
             ORDER BY {_KEY_SQL}, m.seq, m.message_id LIMIT ?""",
        [*scope_params, *low_params, *high_params, limit + 1]).fetchall()
    if len(found) > limit:
        return None
    page = {key[2] for key in ordered}
    busy: set[int] = set()
    position = 0
    for r in found:
        if r["message_id"] in page:
            continue
        key = (r["t"], int(r["seq"]), r["message_id"])
        while position < len(ordered) and ordered[position] < key:
            position += 1
        busy.add(position)
    return busy


def _turn_field(raw: Any) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        turn = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return {"turn": turn} if isinstance(turn, dict) else {}


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
        message_id=row["message_id"],
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
# Keep bounded headroom above the 1059-agent roster: an ordered scan larger
# than the old 1024-entry LRU evicted every entry before its next read.
_PREVIEW_CACHE: RevisionedCache[str, dict[str, Any]] = RevisionedCache(
    "message_previews.dashboard", max_entries=4096)
# The revision map below is read from the conversation heads, one small row a
# conversation. It only moves on a committed write, so polls between writes
# reuse the last map.
_REVISION_CACHE: RevisionedCache[str, tuple[dict, dict, list]] = RevisionedCache(
    "message_previews.dashboard_revisions", max_entries=1)
# The conversations whose messages outrank their head, read once for a
# database (``_floor``).
_FLOOR_CACHE: RevisionedCache[str, dict] = RevisionedCache(
    "message_previews.dashboard_floor", max_entries=1)


# How many of a hiding chat's newest candidate rows its preview looks through for one the user
# sees. A chat whose newest 200 are all coordination shows no preview, as its /log page would
# open on rows from long before; reading further would walk a helper's whole chat per snapshot.
HIDDEN_PREVIEW_ROWS = 200


def _agent_preview(agent_id: str, max_len: int, routine: tuple[str, ...],
                   hide: bool = False) -> dict[str, Any]:
    """head, completed_head and activity for one agent via idx_messages_dashboard_activity.
    ``hide``: the chat leaves coordination out (lib.coordination.hiding_agents), so the preview
    is its newest row the user sees there, as GET /log coordination=auto shows it."""
    marks = ','.join('?' for _ in routine)
    hidden = f"CASE WHEN {coordination.HIDDEN_ROW_SQL} THEN 1 ELSE 0 END" if hide else "0"
    entry: dict[str, Any] = {}
    for field, completed_filter in (
        ('head', ''),
        ('completed_head', "AND NOT (m.role = 'assistant' AND COALESCE(m.source_file, '') LIKE 'live:%')"),
    ):
        rows = conn().execute(f"""
            SELECT m.agent_id, m.message_id, m.backend_session_id, m.revision, m.role, m.text, m.origin,
                   {_message_activity_sql()} AS message_activity, {hidden} AS hidden
              FROM messages m
             WHERE m.agent_id = ?
               AND COALESCE(m.text, '') != '' AND COALESCE(m.tool_name, '') = ''
               {completed_filter}
               AND COALESCE(m.origin, 'user') NOT IN ({marks})
             ORDER BY {_message_activity_sql()} DESC, m.seq DESC, m.updated_at DESC
             LIMIT ?
        """, (agent_id, *routine, HIDDEN_PREVIEW_ROWS if hide else 50))
        for row in rows:
            if row['hidden']:
                continue
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


_LIVE_AGENTS = "SELECT agent_id FROM agents WHERE deleted_at IS NULL"
# The same, among the agent ids of one JSON array.
_LIVE_AMONG = ("SELECT a.agent_id FROM agents a JOIN json_each(?) j ON j.value = a.agent_id"
               " WHERE a.deleted_at IS NULL")


def _read_floor() -> dict[tuple[str, str | None], int]:
    """The conversations whose messages carry a higher revision than their
    head, or that have no head: {(agent, conversation): that revision}.

    Every writer raises its conversation's head to the revision it gives a
    row, in the same transaction, so on a database written by this code the
    answer is empty (both Hosts, 16 Nov 2026: 0 of 369 and 0 of 2,121
    conversations). It is here for rows from before the heads were kept. This
    is the walk of the whole index that the agent list used to make on every
    request (issue 371); it is made once for a database."""
    return {(row['agent_id'], row['backend_session_id']): int(row['revision'] or 0)
            for row in conn().execute("""
        SELECT m.agent_id, m.backend_session_id, m.revision
          FROM (SELECT agent_id, backend_session_id, MAX(revision) AS revision
                  FROM messages GROUP BY agent_id, backend_session_id) m
          LEFT JOIN conversation_heads h
            ON h.agent_id = m.agent_id AND h.backend_session_id IS m.backend_session_id
         WHERE h.revision IS NULL OR h.revision < m.revision""")}


def _floor() -> dict[tuple[str, str | None], int]:
    """``_read_floor`` for this database, kept for the life of the process. A
    conversation in it changes only by a write, and a write raises its head
    above what is kept here. Inside a caller's transaction it is read without
    being kept: uncommitted rows must not outlive it."""
    database = str(_db.DB_PATH)
    if conn().in_transaction:
        hit, kept = _FLOOR_CACHE.get("all", database)
        return kept if hit else _read_floor()
    return _FLOOR_CACHE.get_or_compute("all", database, _kept_floor)


def _kept_floor() -> dict[tuple[str, str | None], int]:
    """The floor as it is kept, and a line in the journal when it holds
    anything. This read is the only check there is that the heads cover
    their messages: a head left behind afterwards, in a running Host, shows
    nowhere, and the agent list alone then gives that conversation its old
    revision and preview until its next write or a restart. A count, no ids."""
    found = _read_floor()
    if found:
        without = sum(1 for _key, _revision in found.items() if _key[1] is None)
        cache_log.log("dashboardHeadsBehind",
                      f"conversations={len(found)} without_session={without}")
    return found


def _revision_maps(agents_sql: str, params: tuple = ()) -> tuple[dict[str, dict[str, int]], dict[str, dict[str, int]]]:
    """Highest revision per agent and conversation, for the agents
    ``agents_sql`` selects, and beside it the heads as they are stored.

    The revision is the conversation's head, which every writer raises to the
    revision it gives a row (a new row, an edited one, one rewritten in place
    by a transcript import, a delete), in the same transaction. Reading the
    heads costs what there are conversations; the grouped walk of the
    messages index it replaces cost what there are messages, 173 MB a request
    on a Host with 2.3 million (issue 371). Where a conversation's messages
    outrank its head (``_floor``) the revision is theirs; the head itself is
    still what a write moves, so a cached preview is keyed by both."""
    revisions: dict[str, dict[str, int]] = {}
    heads: dict[str, dict[str, int]] = {}
    wanted = set()
    # One statement: the agents asked for, each with its heads if it has any.
    for row in conn().execute(f"""
        SELECT a.agent_id, h.backend_session_id, h.revision
          FROM ({agents_sql}) a
          LEFT JOIN conversation_heads h ON h.agent_id = a.agent_id
    """, params):
        wanted.add(row['agent_id'])
        if row['revision'] is not None:
            revisions.setdefault(row['agent_id'], {})[row['backend_session_id']] = int(row['revision'])
            heads.setdefault(row['agent_id'], {})[row['backend_session_id']] = int(row['revision'])
    for (aid, bsid), rev in _floor().items():
        if aid in wanted:
            agent_revs = revisions.setdefault(aid, {})
            if bsid not in agent_revs or rev > agent_revs[bsid]:
                agent_revs[bsid] = rev
    return revisions, heads


def _dashboard_revisions() -> tuple[dict[str, dict[str, int]], dict[str, dict[str, int]], list[str]]:
    revisions, heads = _revision_maps(_LIVE_AGENTS)
    live = [row['agent_id'] for row in conn().execute(_LIVE_AGENTS)]
    return revisions, heads, live


def _preview_key(agent_id: str, revisions: dict, heads: dict) -> tuple:
    """What an agent's cached preview is valid for: every conversation of the
    agent with its head's revision as it is stored (-1: it has none). Not the
    conversation's revision in ``revisions``: for a conversation whose
    messages outrank its head that is the messages' number, which a write
    below it leaves where it was, while the head is what every write moves.

    Every writer in this code base that changes what a preview shows raises
    its conversation's head: a new, edited or rewritten row is given the next
    number of the message clock and the head is raised to it, and a delete
    raises the head, each in the transaction that writes the row
    (message_writes._row_with_its_head where the writer opens none of its
    own; test_heads_at_every_commit asks it of every commit). The key used to carry
    the messages' highest updated_at as a second witness, for a transcript
    import that rewrote a row in place under its old revision; an import
    gives a row it changes a new revision (message_import_rows), and no
    statement moves updated_at without one (pinned by
    test_dashboard_cache_guards). That is a property of those writers, not of
    the table: raw SQL that changes a row and leaves the head moves nothing
    here and is not noticed."""
    return tuple(sorted((str(session or ''), heads.get(agent_id, {}).get(session, -1))
                        for session in revisions.get(agent_id, {})))


def _current_keys(agent_ids: list[str]) -> dict[str, tuple]:
    """The keys those agents have now, in one grouped statement."""
    wanted = json.dumps(agent_ids)
    revisions, heads = _revision_maps(_LIVE_AMONG, (wanted,))
    hiding = coordination.hiding_agents()
    return {agent_id: (*_preview_key(agent_id, revisions, heads), agent_id in hiding)
            for agent_id in agent_ids}


def _cache_stamp():
    try:
        stamp = change_stamp()
    except Exception as exc:  # noqa: BLE001 - cache failure cannot hide the projection
        cache_log.log("dashboardCacheStampFail", f"error={type(exc).__name__}")
        return None
    if stamp[0] < 0:
        cache_log.log("dashboardCacheStampFail", "code=unavailable")
        return None
    return stamp


def _cache_read(cache, key, revision, failed):
    if cache.name in failed:
        return False, None
    try:
        return cache.get(key, revision)
    except Exception as exc:  # noqa: BLE001 - recompute from SQLite
        failed.add(cache.name)
        cache_log.log("dashboardCacheReadFail", f"cache={cache.name} error={type(exc).__name__}")
        return False, None


def _cache_write(cache, key, revision, value, failed):
    if cache.name in failed:
        return
    try:
        cache.put(key, revision, value)
    except Exception as exc:  # noqa: BLE001 - the fresh result is already available
        failed.add(cache.name)
        cache_log.log("dashboardCacheWriteFail", f"cache={cache.name} error={type(exc).__name__}")


def _preview_copy(entry):
    # The two head dictionaries are flat; isolate returned values from cached
    # heads on both misses and hits. Revisions are attached as a separate copy.
    return {key: dict(value) if isinstance(value, dict) else value
            for key, value in entry.items()}


def dashboard_messages(max_len: int = 80, *, agent_ids=None) -> dict[str, dict[str, Any]]:
    """Project every live agent, publishing cache fills only after validation.

    ``agent_ids`` (a set) projects only those agents: a preview that misses
    the cache costs three statements, so a snapshot that leaves agents out
    must not pay for theirs. Such a partial projection fills the cache for
    its own agents and never prunes it, because the other agents' cached
    previews are still good for the next caller that wants them.

    A caller's transaction may see uncommitted rows: never read or populate
    shared caches there. Otherwise one database stamp is sampled before and
    after the whole batch. Stamp queries do not grow with the roster.

    Unchanged stamp: nothing was committed while the batch ran, so the
    revision map and every preview computed in it are one generation and are
    all published.

    Changed stamp: something was committed meanwhile, by any connection. The
    revision map is not published (it is keyed by the stamp). A preview is
    still published if its own agent's key is the same after the batch as it
    was before: that agent's rows did not change while its three statements
    ran, so the preview is the one the key stands for. An agent whose key
    moved is left out and computed again next time, so a preview older than
    the rows it describes is never stored. The keys are read again for the
    computed agents only, in one grouped statement. Before this, any commit
    at all discarded the whole batch; on a Host that is always being written
    a cold cache (after a restart: three statements for every agent) then
    never filled, and each agent list took seconds until a batch happened to
    run undisturbed.

    Shared caching requires callers to exhaust prior implicit read cursors.
    The snapshot and warm-up caller tests pin this existing boundary; an old
    borrowed SQLite snapshot is not certified by in_transaction=False.
    General borrowed-cursor support remains parked under lead780/backlog33.
    This guard does not make the returned multi-SELECT map an atomic snapshot.
    """
    database = conn()
    stamp = None if database.in_transaction else _cache_stamp()
    routine = tuple(sorted(origins.ROUTINE_AUTOMATION_ORIGINS))
    failed: set[str] = set()
    map_hit, projection = ((False, None) if stamp is None else
                          _cache_read(_REVISION_CACHE, "all", stamp, failed))
    if not map_hit:
        projection = _dashboard_revisions()
    revisions, heads, live = projection
    result: dict[str, dict[str, Any]] = {}
    pending = []
    # A setting change writes nothing to a conversation, so whether the chat hides coordination
    # is part of what a cached preview is valid for.
    hiding = coordination.hiding_agents(database=database)
    for agent_id in live:
        if agent_ids is not None and agent_id not in agent_ids:
            continue
        key = (*_preview_key(agent_id, revisions, heads), agent_id in hiding)
        hit, cached = ((False, None) if stamp is None or max_len != 80 else
                       _cache_read(_PREVIEW_CACHE, agent_id, key, failed))
        entry = _preview_copy(cached) if hit else _agent_preview(agent_id, max_len, routine,
                                                                 agent_id in hiding)
        if not hit and stamp is not None and max_len == 80:
            pending.append((agent_id, key, _preview_copy(entry)))
        if agent_id in revisions:
            entry['revisions'] = dict(revisions[agent_id])
        result[agent_id] = entry
    if stamp is not None and not database.in_transaction:
        after = _cache_stamp()
        if stamp == after:
            if not map_hit:
                _cache_write(_REVISION_CACHE, "all", stamp, projection, failed)
            for agent_id, key, entry in pending:
                _cache_write(_PREVIEW_CACHE, agent_id, key, entry, failed)
            if agent_ids is None and _PREVIEW_CACHE.name not in failed:
                try:
                    _PREVIEW_CACHE.retain(result)
                except Exception as exc:  # noqa: BLE001 - bounded keys are never projected as a roster
                    cache_log.log("dashboardCachePruneFail", f"cache={_PREVIEW_CACHE.name} error={type(exc).__name__}")
        elif after is not None and pending:
            _publish_unchanged(pending, failed)
    return result


def _publish_unchanged(pending: list, failed: set[str]) -> None:
    """Store the computed previews of the agents whose key did not move
    during the batch (see dashboard_messages). Never prunes: this batch's
    roster may already be out of date."""
    try:
        current = _current_keys([agent_id for agent_id, _key, _entry in pending])
    except Exception as exc:  # noqa: BLE001 - the projection is already made; only the cache fill is lost
        cache_log.log("dashboardCacheCertifyFail", f"error={type(exc).__name__}")
        return
    for agent_id, key, entry in pending:
        if current.get(agent_id) == key:
            _cache_write(_PREVIEW_CACHE, agent_id, key, entry, failed)


def last_message_activity(*, agent_id: str) -> int:
    """Epoch ms of the latest actual message, not its latest cache import."""
    row = conn().execute(
        f"""SELECT MAX({_message_activity_sql()}) AS t
              FROM messages
             WHERE agent_id = ?""",
        (agent_id,),
    ).fetchone()
    return int(row["t"] or 0)


def message_tool_details(*, agent: "AgentRef", message_id: str) -> dict[str, Any] | None:
    """Return the heavy tool payload for one message, scoped to its agent."""
    row = conn().execute(
        """SELECT tools_json, display_cells_json FROM messages
            WHERE agent_id = ? AND message_id = ?""",
        (agent.agent_id, message_id),
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


def latest_revision(*, agent_id: str, backend_session_id: str = "", extra_backend_session_id: str = "") -> int:
    params: list[Any] = [agent_id]
    where = "agent_id = ?"
    sessions = list(dict.fromkeys(v for v in (backend_session_id, extra_backend_session_id) if v))
    if sessions:
        where += " AND backend_session_id IN (" + ",".join("?" for _ in sessions) + ")"
        params.extend(sessions)
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
                     after_revision: int = 0, extra_backend_session_id: str = "") -> bool:
    params: list[Any] = [agent_id]
    where = "agent_id = ?"
    sessions = list(dict.fromkeys(v for v in (backend_session_id, extra_backend_session_id) if v))
    if sessions:
        where += " AND backend_session_id IN (" + ",".join("?" for _ in sessions) + ")"
        params.extend(sessions)
    row = conn().execute(
        f"""SELECT COALESCE(MAX(replace_revision), 0) AS revision
              FROM conversation_heads WHERE {where}""",
        tuple(params),
    ).fetchone()
    return int(row["revision"] or 0) > int(after_revision or 0)


def has_system_notices(*, agent_id: str, backend_session_id: str) -> bool:
    return bool(conn().execute("SELECT 1 FROM messages WHERE agent_id=? AND backend_session_id=? AND origin='system' AND message_id LIKE 'notice:%' LIMIT 1",
                               (agent_id, backend_session_id)).fetchone())
