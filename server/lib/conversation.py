"""Conversation read model service.

HTTP should not know how backend transcript files, runtime rows, and the
canonical message cache fit together. This module owns that boundary and
returns the `/log` response shape the clients already consume.
"""
from __future__ import annotations

import pathlib
import time
from typing import Any, Callable

from . import agents as agents_db
from . import backends
from . import transcript_import_cache
from . import eventlog


FindTranscript = Callable[[str], pathlib.Path | None]
ParseTranscript = Callable[[pathlib.Path], list[dict[str, Any]]]


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
                      claude_finder: FindTranscript,
                      claude_parser: ParseTranscript) -> dict[str, Any]:
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

    if backend == backends.CLAUDE:
        latest = claude_finder(backend_session_id)
    else:
        latest = backends.find_session_jsonl(backend, backend_session_id)

    if latest is not None:
        def import_latest() -> None:
            import_started = time.perf_counter()
            parse_started = import_started
            imported = (claude_parser(latest) if backend == backends.CLAUDE
                        else backends.parse_turns(backend, latest))
            parsed_at = time.perf_counter()
            agents_db.store_transcript_turns(
                agent_id=agent_id,
                backend_session_id=backend_session_id,
                source_file=str(latest),
                turns=imported,
            )
            stored_at = time.perf_counter()
            eventlog.emit(
                "transcript", "import",
                session=session, agent_id=agent_id,
                backend_session_id=backend_session_id,
                detail={
                    "interaction_id": interaction_id,
                    "rows": len(imported),
                    "parse_ms": round((parsed_at - parse_started) * 1000, 3),
                    "store_ms": round((stored_at - parsed_at) * 1000, 3),
                    "total_ms": round((stored_at - import_started) * 1000, 3),
                    "source_bytes": latest.stat().st_size,
                },
            )

        transcript_import_cache.import_if_changed(latest, import_latest)

    # SQLite is the app-facing source of truth. Backend transcript files are
    # importer inputs only; every client sees one canonical read model.
    # Fetch one extra row so the cursor tells the truth when a page is
    # truncated. For a snapshot the store returns ascending display order for
    # the newest rows, so discard the oldest extra row. For a delta it returns
    # ascending revision order, so discard the newest extra row.
    turns = agents_db.list_messages(
        agent_id=agent_id,
        backend_session_id=backend_session_id,
        after_revision=after_revision,
        before_message_id=before_message_id,
        limit=limit + 1,
        include_automated=include_automated,
    )
    has_more = len(turns) > limit
    if has_more:
        turns = turns[:limit] if after_revision else turns[-limit:]
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
                        "id", "kind", "title", "summary", "status") if k in cell},
                    "detail_count": len(cell.get("lines") or []),
                }
                for cell in cells if isinstance(cell, dict)
            ]
            # One representative collapsed card is enough to show that tool
            # activity exists. Expansion replaces it with the complete payload.
            # Sending hundreds of hidden card headers defeated lazy loading.
            turn["tools"] = compact_tools[:1]
            if compact_cells:
                representative = compact_cells[0]
                representative["detail_count"] = sum(
                    max(1, int(cell.get("detail_count") or 0))
                    for cell in compact_cells
                )
                turn["display_cells"] = [representative]
            else:
                turn["display_cells"] = []
    head_revision = agents_db.latest_message_revision(
        agent_id=agent_id, backend_session_id=backend_session_id,
    )
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
    )
    latest_ts = turns[-1].get("timestamp") if turns else ""
    return {
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
