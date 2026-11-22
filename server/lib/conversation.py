"""Conversation read model service.

HTTP should not know how backend transcript files, runtime rows, and the
canonical message cache fit together. This module owns that boundary and
returns the `/log` response shape the clients already consume.
"""
from __future__ import annotations

import pathlib
from typing import Any

from . import agents as agents_db
from . import coordination, message_previews
from . import backends
from . import transcript_import_cache
from . import transcript_incremental
from . import eventlog

ACTIVE_SNAPSHOT_IMPORT_TIMEOUT_SECONDS = 2.25


def _compact_text(value: Any, limit: int = 240) -> Any:
    if not isinstance(value, str) or len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def _activity_count(tools: list, cells: list) -> int:
    """Mirror the native ActivityPresentation card count before compaction."""
    if not cells:
        return len(tools)
    rich_edits = [tool for tool in tools if isinstance(tool, dict)
                  and tool.get("name") in {"Edit", "MultiEdit", "Write"}]
    visible_cells = cells if not rich_edits else [
        cell for cell in cells
        if not isinstance(cell, dict) or cell.get("kind") != "patch"]
    return len(visible_cells) + len(rich_edits)


def session_cwd(session: str) -> pathlib.Path:
    agent = agents_db.get_by_session(session)
    if agent and agent.get("cwd"):
        path = pathlib.Path(agent["cwd"]).expanduser()
        if path.is_dir():
            return path
    return pathlib.Path.home()


def _live_backend_session(agent_id: str) -> str:
    row = agents_db.conn().execute(
        """SELECT backend_session_id FROM runtimes
            WHERE agent_id = ? AND ended_at IS NULL
            ORDER BY started_at DESC LIMIT 1""",
        (agent_id,),
    ).fetchone()
    return (row and row["backend_session_id"]) or ""


def load_conversation(*, session: str, after_revision: int = 0,
                      limit: int = 100,
                      include_automated: bool = True,
                      include_tool_details: bool = True,
                      before_message_id: str = "",
                      interaction_id: str = "",
                      background_import: bool = False,
                      coordination_mode: str = "",
                      coordination_read: str = "",
                      view: str = "") -> dict[str, Any]:
    # The message store caps reads at 5,000 rows. Reserve one row for the
    # pagination lookahead so `has_more` remains truthful at the public maximum.
    limit = max(1, min(int(limit or 100), 4999))
    cwd = session_cwd(session)
    agent = agents_db.get_by_session(session)
    if not agent:
        return {"cwd": str(cwd), "turns": [], "missing": True,
                "latest_ts": "", "latest_revision": 0,
                "conversation_id": "", "has_more": False,
                "includes_automated": include_automated}

    agent_id = agent["agent_id"]
    backend = backends.normalize(agent.get("backend"))
    backend_session_id = _live_backend_session(agent_id)
    service_namespace = ""
    service_conversation = False
    if agent.get("is_janitor"):
        from .janitor_notices import conversation_id, has_notices
        if backend_session_id:
            service_namespace = conversation_id(agent_id)
        elif has_notices(agent_id):
            service_conversation = True
            backend_session_id = conversation_id(agent_id)

    # No live UUID yet — the agent was just (re)launched and hasn't fired
    # its first hook, so the runtime row's backend_session_id is still NULL.
    # Honour the same contract as find_latest_jsonl: empty UUID → empty
    # pane. Without this short-circuit the read-model queries below run
    # unfiltered (backend_session_id="" drops the WHERE clause) and bleed
    # the *previous* conversation's turns until the first response stamps
    # the new UUID in.
    if not backend_session_id:
        return {"cwd": str(cwd), "file": None, "turns": [],
                "missing": True, "latest_ts": "", "latest_revision": 0,
                "replace_required": False, "conversation_id": "",
                "has_more": False,
                "includes_automated": include_automated}

    # The backend locates and parses its own transcript.
    latest = None if service_conversation else backends.find_session_jsonl(backend, backend_session_id)

    if latest is not None:
        def import_latest():
            materialized = (background_import and has_stored_history and not after_revision and not before_message_id
                            and transcript_incremental._materialized_history(agent_id, backend_session_id))
            return transcript_incremental.import_transcript(
                backend=backend, path=latest, agent_id=agent_id,
                backend_session_id=backend_session_id, _defer_full=materialized)

        def import_completed(imported):
            eventlog.emit(
                "transcript", "import",
                session=session, agent_id=agent_id,
                backend_session_id=backend_session_id,
                detail={
                    "interaction_id": interaction_id,
                    "rows": imported["rows"],
                    "parse_ms": round(imported["parse_s"] * 1000, 3),
                    "store_ms": round(imported["store_s"] * 1000, 3),
                    "total_ms": round((imported["parse_s"] + imported["store_s"]) * 1000, 3),
                    "source_bytes": transcript_import_cache.source_size(latest),
                },
            )

        importer = transcript_incremental._completion_import(
            import_latest, backend=backend, path=latest, agent_id=agent_id,
            backend_session_id=backend_session_id, after_complete=import_completed)

        # Preserve first-open history for adopted/restored agents. Once the
        # canonical store has rows, refreshes serve that snapshot immediately.
        # Cold imports still use the same time-budgeted, yielding write batches.
        has_stored_history = agents_db.conn().execute(
            "SELECT 1 FROM messages WHERE agent_id=? AND backend_session_id=? AND seq>=0 LIMIT 1",
            (agent_id, backend_session_id)).fetchone() is not None
        if (background_import and has_stored_history and not after_revision
                and not before_message_id and agents_db.is_busy(agent_id)):
            transcript_import_cache.import_for_snapshot(
                latest, importer, owner=agent_id,
                timeout=ACTIVE_SNAPSHOT_IMPORT_TIMEOUT_SECONDS)
        else:
            importing = (transcript_import_cache.schedule_import
                         if background_import and has_stored_history
                         else transcript_import_cache.import_if_changed)
            importing(latest, importer, owner=agent_id,
                      **({"startup_exempt": True} if background_import and has_stored_history else {}))

    # SQLite is the app-facing source of truth. Backend transcript files are
    # importer inputs only; every client sees one canonical read model.
    # Fetch one extra row so the cursor tells the truth when a page is
    # truncated. For a snapshot the store returns ascending display order for
    # the newest rows, so discard the oldest extra row. For a delta it returns
    # ascending revision order, so discard the newest extra row.
    # Coordination between agents (issue 478): only a request that names a mode is filtered
    # and answered with the policy it was read under; without one, the answer is today's.
    policy = coordination.read_policy(agent_id, coordination_mode) if coordination_mode else None
    # The head BEFORE the rows. Read after them, in a statement of its own, a row
    # committed in between (a steered message lands within milliseconds of the
    # next) had a revision at or below the cursor this answer returned, and the
    # client's next delta (after_revision) never sent it. Read first, the cursor
    # never passes a row this answer did not see: a row committed in between is
    # sent now, and again in the next delta, which clients merge by id. Writers
    # are serialized, so every row committed after this read has a higher
    # revision.
    head_revision = agents_db.latest_message_revision(
        agent_id=agent_id, backend_session_id=backend_session_id,
        extra_backend_session_id=service_namespace,
    )
    turns = agents_db.list_messages(
        agent_id=agent_id,
        backend_session_id=backend_session_id,
        extra_backend_session_id=service_namespace,
        after_revision=after_revision,
        before_message_id=before_message_id,
        limit=limit + 1,
        include_automated=include_automated,
        hide_coordination=bool(policy and policy["hidden"]),
        hide_form_answers=True,
        view=view,
    )
    has_more = len(turns) > limit
    if has_more:
        turns = turns[:limit] if after_revision else turns[-limit:]
    hidden_after = None
    if policy and policy["hidden"] and coordination.can_leave_note(agent_id):
        sessions = list(dict.fromkeys(v for v in (backend_session_id, service_namespace) if v))
        before, hidden_after = message_previews.coordination_gaps(
            agent_id=agent_id, sessions=sessions, rows=turns, include_automated=include_automated,
            contiguous=not after_revision,
            tail=not after_revision and not before_message_id)
        for turn in turns:
            if turn["id"] in before:
                turn["hidden_before"] = before[turn["id"]]
    if not include_tool_details:
        for turn in turns:
            tools = turn.get("tools") if isinstance(turn.get("tools"), list) else []
            cells = turn.get("display_cells") if isinstance(
                turn.get("display_cells"), list) else []
            turn["tool_details_available"] = bool(tools or cells)
            turn["activity_count"] = _activity_count(tools, cells)
            compact_tools = [
                {k: _compact_text(tool[k]) for k in (
                    "id", "name", "summary", "action", "status")
                 if k in tool}
                for tool in tools if isinstance(tool, dict)
            ]
            compact_cells = [
                {
                    **{k: _compact_text(cell[k]) for k in (
                        "id", "kind", "title", "summary", "status",
                        "ephemeral") if k in cell},
                    "detail_count": len(cell.get("lines") or []),
                }
                for cell in cells if isinstance(cell, dict)
            ]
            # One representative collapsed card is enough to show that tool
            # activity exists. Expansion replaces it with the complete payload.
            # Sending hundreds of hidden card headers defeated lazy loading.
            # A failed call stays too, after the representative, as for display cells below
            # (the quiet chat, #653: failures are never folded away by a reload).
            turn["tools"] = compact_tools[:1] + [
                tool for tool in compact_tools[1:] if tool.get("status") == "error"]
            if compact_cells:
                # A failed call is never compacted away: it stays in its own row, as it does
                # live, and is not counted among the folded calls (the quiet chat, #653).
                folded = [cell for cell in compact_cells if cell.get("status") != "error"]
                failed = [cell for cell in compact_cells if cell.get("status") == "error"]
                if folded:
                    representative = folded[0]
                    representative["detail_count"] = sum(
                        max(1, int(cell.get("detail_count") or 0))
                        for cell in folded
                    )
                    turn["display_cells"] = [
                        cell for cell in compact_cells
                        if cell is representative or cell.get("status") == "error"
                    ]
                else:
                    turn["display_cells"] = failed
            else:
                turn["display_cells"] = []
    # Never advance an incremental cursor beyond rows actually delivered.
    # Doing so permanently skips the remainder of a backlog larger than limit.
    revision = (
        max((int(turn.get("revision") or 0) for turn in turns), default=after_revision)
        if has_more and after_revision
        else head_revision
    )
    replace_required = bool(after_revision) and agents_db.conversation_requires_replace(
        agent_id=agent_id,
        backend_session_id=backend_session_id,
        after_revision=after_revision,
        extra_backend_session_id=service_namespace,
    )
    if (policy and after_revision and coordination_read
            and coordination_read != policy["visibility"]):
        # The client's cache was read under another policy (the setting changed, here or on
        # another device): it reloads; nothing was written to the conversation for it.
        replace_required = True
    latest_ts = max((row.get("timestamp") or "" for row in turns), default="")
    extra: dict[str, Any] = {}
    if view:
        extra["view"] = view      # the view this answer was read under (issue 702)
    if policy:
        extra["coordination"] = {"hidden": policy["hidden"], "source": policy["source"],
                                 "visibility": policy["visibility"]}
        if hidden_after:
            extra["hidden_after"] = hidden_after
    return {**extra, 
        "cwd": str(cwd),
        "file": latest.name if latest is not None else None,
        "turns": turns,
        "missing": latest is None and not turns,
        "latest_ts": latest_ts or "",
        "latest_revision": revision,
        "replace_required": replace_required,
        "conversation_id": backend_session_id,
        "has_more": has_more,
        "includes_automated": include_automated,
    }
