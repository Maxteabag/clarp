"""Parse OpenCode session history into the shared turn list.

OpenCode keeps a conversation in two tables. ``message.data`` is an envelope
(role, model, time, error); what was actually said lives in ``part`` rows keyed
by ``message_id``, one per text block, tool call, reasoning block or step
marker. A parser that reads only ``message`` finds no text at all."""
from __future__ import annotations

import json
import os
import pathlib
import sqlite3
from datetime import UTC, datetime
from typing import Any

from .voice_preamble import strip_voice_preamble
from .log import log_exception
from .claude_transcript import summarise_tool
from .codex_transcript import (
    _cap_cell, _classify_exploration, _command_cell, _display_cell,
    _display_line, _generic_tool_cell, _preview_output, _safe_id,
    _web_search_cell,
)
from .text_util import truncate

# OpenCode's lowercase tool names and camelCase arguments, mapped to the shared
# vocabulary the chat already knows how to render as file, edit and shell cells.
_TOOL_NAMES = {
    "bash": "Bash", "read": "Read", "edit": "Edit", "write": "Write",
    "glob": "Glob", "grep": "Grep", "webfetch": "WebFetch",
    "websearch": "WebSearch", "todowrite": "TodoWrite", "skill": "Skill",
}
_ARG_NAMES = {"filePath": "file_path", "oldString": "old_string",
              "newString": "new_string", "replaceAll": "replace_all"}


def opencode_home() -> pathlib.Path:
    override = os.environ.get("CLAUDE_PWA_OPENCODE_HOME")
    if override:
        return pathlib.Path(override)
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return pathlib.Path(xdg) / "opencode"
    return pathlib.Path.home() / ".local" / "share" / "opencode"


def db_path(home: pathlib.Path | None = None) -> pathlib.Path:
    return (home or opencode_home()) / "opencode.db"


def _text_of(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(filter(None, (_text_of(item) for item in value)))
    if isinstance(value, dict):
        for key in ("text", "content", "delta"):
            if isinstance(value.get(key), str):
                return value[key]
        if "content" in value:
            return _text_of(value["content"])
    return ""


def _role_of(data: dict) -> str:
    role = str(data.get("role") or data.get("type") or "").lower()
    if role in {"user", "human"}:
        return "user"
    if role in {"assistant", "model", "ai"}:
        return "assistant"
    return ""


def _iso(created: Any) -> str:
    """OpenCode stores epoch milliseconds. Conversation rows are merged and
    ordered by comparing timestamps as strings against ISO values from other
    sources, so a bare integer would sort before every one of them."""
    try:
        value = float(created)
    except (TypeError, ValueError):
        return str(created or "")
    if value > 10_000_000_000:
        value /= 1000
    try:
        stamp = datetime.fromtimestamp(value, UTC)
    except (OverflowError, OSError, ValueError):
        return str(created)
    return stamp.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _state_of(part: dict) -> dict:
    state = part.get("state")
    return state if isinstance(state, dict) else {}


def _status_of(part: dict) -> str:
    """``running`` for a call still in flight; an interrupted turn leaves
    calls in that state for good, which ``_settle_stale_calls`` corrects."""
    status = str(_state_of(part).get("status") or "")
    if status == "error":
        return "error"
    if status in {"running", "pending"}:
        return "running"
    return "ok"


def _args_of(part: dict) -> dict:
    raw = part.get("input") or _state_of(part).get("input") or {}
    return ({_ARG_NAMES.get(str(k), str(k)): v for k, v in raw.items()}
            if isinstance(raw, dict) else {})


def _tool_of(part: dict) -> dict:
    args = _args_of(part)
    raw_name = str(part.get("name") or part.get("tool") or "tool")
    tool = summarise_tool(_TOOL_NAMES.get(raw_name, raw_name), args,
                          str(part.get("callID") or part.get("id") or ""))
    tool["status"] = _status_of(part)
    # Kept for the apps' generic tool card, bounded like summarise_tool's own
    # fallback: raw arguments carry whole files and scripts on every import.
    tool["input"] = {k: truncate(v, 400) if isinstance(v, str) else v
                     for k, v in args.items()}
    return tool


def _metadata_of(state: dict) -> dict:
    metadata = state.get("metadata")
    return metadata if isinstance(metadata, dict) else {}


def _output_of(state: dict) -> str:
    metadata = _metadata_of(state)
    for value in (state.get("output"), metadata.get("output")):
        if isinstance(value, str) and value:
            return value
    return ""


def _error_line(state: dict) -> list[dict]:
    error = str(state.get("error") or "").strip()
    return [_display_line(truncate(error.removeprefix("Error: "), 300),
                          kind="error")] if error else []


def _plan_lines(todos: Any) -> list[dict]:
    """The ``update_plan`` cell's lines, from OpenCode's todo list."""
    lines = []
    for todo in todos if isinstance(todos, list) else []:
        if isinstance(todo, dict) and todo.get("content"):
            lines.append(_display_line(
                todo["content"],
                str(todo.get("status") or "").replace("_", " ").title(),
                "status"))
    return lines


def _task_cell(call_id: str, args: dict, state: dict, status: str) -> dict:
    """A ``task`` call is an OpenCode sub-agent in a child session of this one;
    shown in the ``subagents`` shape the Claude ``Agent`` tool uses."""
    metadata = _metadata_of(state)
    lines = []
    if args.get("prompt"):
        lines.append(_display_line(truncate(str(args["prompt"]), 300), "Task"))
    if args.get("subagent_type"):
        lines.append(_display_line(args["subagent_type"], "Type", "muted"))
    output = _output_of(state)
    if output:
        lines.append(_display_line(truncate(output, 600), "Result", "status"))
    lines += _error_line(state)
    if metadata.get("sessionId"):
        lines.append(_display_line(metadata["sessionId"], "Session", "muted"))
    title = {"running": "Running agent", "error": "Agent failed"}.get(
        status, "Agent finished")
    return _display_cell(kind="subagents", title=title,
                         summary=str(args.get("description") or ""),
                         status=status, cell_id=call_id, lines=lines)


def _question_cell(call_id: str, args: dict, state: dict, status: str) -> dict:
    questions = [q for q in args.get("questions") or [] if isinstance(q, dict)]
    first = questions[0] if questions else {}
    lines = []
    for option in first.get("options") or []:
        if isinstance(option, dict) and option.get("label"):
            lines.append(_display_line(option["label"], "Option", "muted"))
    output = _output_of(state)
    if output:
        lines.append(_display_line(truncate(output, 300), "Answer", "status"))
    lines += _error_line(state)
    return _display_cell(
        kind="tool", title="Asked" if status != "running" else "Asking",
        summary=truncate(str(first.get("question") or first.get("header") or "question"), 240),
        status=status, cell_id=call_id, lines=lines)


def _cell_of(part: dict, tool: dict) -> dict | None:
    """The Codex-shaped display cell for one OpenCode tool call.

    Commands, file reads and searches, edits, plans and sub-agents use the
    cell kinds Codex rollouts produce so the apps render them the same way.
    Read, glob and grep become ``exploration`` lines that
    ``_coalesce_exploration`` folds into one cell per run of them."""
    state = _state_of(part)
    args = _args_of(part)
    call_id = str(tool.get("id") or "")
    status = tool["status"]
    name = str(part.get("tool") or part.get("name") or "")
    output = _output_of(state)
    if name == "bash":
        metadata = _metadata_of(state)
        exit_code = metadata.get("exit")
        if not isinstance(exit_code, int):
            exit_code = 1 if status == "error" else None
        cell = _command_cell(command=truncate(str(args.get("command") or ""), 400),
                             call_id=call_id, output=output,
                             exit_code=exit_code, running=status == "running")
        cell["lines"] = cell["lines"] + _error_line(state)
        explore = _classify_exploration(str(args.get("command") or ""))
        if explore and status != "error":
            cell["_explore"] = explore
        return cell
    if name in {"read", "glob", "grep"}:
        target = str(args.get("file_path") or args.get("path") or "")
        pattern = str(args.get("pattern") or "")
        if name == "read":
            line = {"label": "Read", "text": target}
        elif name == "glob":
            line = {"label": "List", "text": f"{pattern} in {target}" if target else pattern}
        else:
            line = {"label": "Search", "text": f"{pattern} in {target}" if target else pattern}
        cell = _display_cell(kind="exploration", title="Explored", status=status,
                             cell_id=call_id, lines=_error_line(state))
        if status != "error":
            cell["_explore"] = line
        return cell
    if name in {"edit", "write"}:
        path = str(args.get("file_path") or "")
        lines = [_display_line(path, "Edit" if name == "edit" else "Add")]
        old = str(args.get("old_string") or "") if name == "edit" else ""
        new = str(args.get("new_string") if name == "edit" else args.get("content") or "")
        lines += [_display_line(f"-{raw}", kind="diff_old") for raw in old.splitlines()[:40]]
        lines += [_display_line(f"+{raw}", kind="diff_new") for raw in new.splitlines()[:40]]
        return _display_cell(kind="patch", title="Edited", summary=path,
                             status=status, cell_id=call_id,
                             lines=lines + _error_line(state))
    if name == "webfetch":
        cell = _web_search_cell(call_id, args.get("url") or "web",
                                running=status == "running")
        cell.update(title="Fetching" if status == "running" else "Fetched",
                    status=status, lines=_error_line(state))
        return cell
    if name == "websearch":
        cell = _web_search_cell(call_id, args.get("query") or "web",
                                running=status == "running")
        cell.update(status=status, lines=_error_line(state))
        return cell
    if name == "todowrite":
        return _display_cell(kind="plan", title="Updated plan", status=status,
                             cell_id=call_id, lines=_plan_lines(args.get("todos")))
    if name == "task":
        return _task_cell(call_id, args, state, status)
    if name == "question":
        return _question_cell(call_id, args, state, status)
    if name == "skill":
        return _display_cell(kind="tool", title="Loaded skill",
                             summary=str(args.get("name") or ""), status=status,
                             cell_id=call_id, lines=_error_line(state))
    cell = _generic_tool_cell(name or "tool", {"call_id": call_id}, status=status)
    cell["status"] = status
    cell["lines"] = (_preview_output(output) if output else []) + _error_line(state)
    return cell


def _coalesce_exploration(cells: list[dict]) -> list[dict]:
    """Fold each run of read/search/list calls into one ``Explored`` cell, as
    ``codex_transcript._coalesce_exploration_cells`` does for shell reads."""
    out: list[dict] = []
    pending: list[dict] = []

    def flush() -> None:
        if not pending:
            return
        running = any(p["status"] == "running" for p in pending)
        out.append(_display_cell(
            kind="exploration", title="Exploring" if running else "Explored",
            status="running" if running else "ok",
            cell_id=pending[0]["id"] if len(pending) == 1 else _safe_id(
                "exploration", "|".join(p["id"] for p in pending)),
            lines=[_display_line(p["_explore"]["text"], p["_explore"]["label"])
                   for p in pending]))
        pending.clear()

    for cell in cells:
        if "_explore" in cell:
            pending.append(cell)
            continue
        flush()
        out.append(cell)
    flush()
    return out


def _settle_stale_calls(turns: list[dict]) -> None:
    """A call left ``running`` by an interrupted turn never finishes. Only the
    newest assistant turn can still be working; elsewhere show it as recorded
    rather than as a spinner that never stops."""
    last = next((t for t in reversed(turns) if t["role"] == "assistant"), None)
    for turn in turns:
        if turn is last:
            continue
        for item in (*turn.get("tools", []), *turn.get("display_cells", [])):
            if item.get("status") == "running":
                item["status"] = "recorded"
                if item.get("title") in {"Running", "Exploring", "Running agent",
                                         "Fetching", "Searching", "Asking", "Calling"}:
                    item["title"] = {"Running": "Ran", "Exploring": "Explored",
                                     "Running agent": "Agent stopped",
                                     "Fetching": "Fetched", "Searching": "Searched",
                                     "Asking": "Asked", "Calling": "Called"}[item["title"]]


def _kind_of(data: dict) -> str:
    """Codex's message phase, from the step's finish reason: a step that ends
    by calling tools is interim commentary; any other ending is the answer."""
    finish = str(data.get("finish") or "")
    if not finish:
        return ""
    return "commentary" if finish == "tool-calls" else "final_answer"


def _error_text(data: dict) -> str:
    error = data.get("error")
    if not isinstance(error, dict):
        return ""
    detail = error.get("data") if isinstance(error.get("data"), dict) else {}
    message = str(detail.get("message") or error.get("message") or "").strip()
    name = str(error.get("name") or "error")
    return f"OpenCode {name}: {message}" if message else f"OpenCode {name}"


def _turns_from_messages(
    rows: list[tuple[str, str]],
    parts: dict[str, list[dict]] | None = None,
    ids: list[str] | None = None,
) -> list[dict]:
    turns: list[dict] = []
    pending: list[dict] = []
    pending_cells: list[dict] = []
    # The assistant text turn later tool-only steps belong to. OpenCode writes
    # one message per step, so "I'll check the tests" and the calls that do it
    # are often separate messages; Codex shows those calls under that
    # commentary, and so do we. Steps before any text wait for the first one.
    open_turn: dict | None = None
    for index, (created, raw) in enumerate(rows):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        role = _role_of(data)
        if not role:
            continue
        inline = data.get("content") or data.get("body")
        text = _text_of(inline) if inline else ""
        stored = (parts or {}).get(ids[index], []) if ids else []
        texts: list[str] = []
        tools: list[dict] = []
        cells: list[dict] = []
        for part in list(data.get("parts") or []) + stored:
            if not isinstance(part, dict):
                continue
            kind = str(part.get("type") or "")
            if kind in {"tool", "tool_use", "toolcall"}:
                tool = _tool_of(part)
                tools.append(tool)
                cell = _cell_of(part, tool)
                if cell is not None:
                    cells.append(cell)
            elif kind in {"", "text"} and not part.get("synthetic") \
                    and not part.get("ignored"):
                # Reasoning and step markers are not part of the reply.
                texts.append(_text_of(part))
        if not text:
            text = "\n\n".join(filter(None, texts))
        failed = False
        if role == "user":
            text = strip_voice_preamble(text)
        elif not text and not tools:
            # A turn that died (suspended account, provider outage) has an
            # error and no parts; without this the chat shows an unanswered
            # prompt and nothing about why.
            text = _error_text(data)
            failed = True
        if not text and not tools:
            continue
        if role == "assistant" and not text:
            if open_turn is not None:
                open_turn["tools"].extend(tools)
                open_turn["display_cells"].extend(cells)
            else:
                pending.extend(tools)
                pending_cells.extend(cells)
            continue
        if role == "user" and pending:
            if turns and turns[-1]["role"] == "assistant":
                turns[-1]["tools"].extend(pending)
                turns[-1]["display_cells"].extend(pending_cells)
            else:
                # An interrupted reply that never wrote text; its calls must
                # not move into the answer to the next prompt.
                turns.append({"role": "assistant", "text": "", "tools": pending,
                              "display_cells": pending_cells,
                              "timestamp": turns[-1]["timestamp"] if turns else ""})
            pending, pending_cells = [], []
        if role == "assistant":
            tools, pending = pending + tools, []
            cells, pending_cells = pending_cells + cells, []
        turn = {
            "role": role, "text": text, "tools": tools, "display_cells": cells,
            "timestamp": _iso(created),
        }
        kind = _kind_of(data) if role == "assistant" and not failed else ""
        if kind:
            turn["kind"] = kind
        turns.append(turn)
        open_turn = turn if role == "assistant" and not failed else None
    if pending:
        # The reply never reached its text (interrupted, or still running).
        if turns and turns[-1]["role"] == "assistant":
            turns[-1]["tools"].extend(pending)
            turns[-1]["display_cells"].extend(pending_cells)
        else:
            turns.append({"role": "assistant", "text": "", "tools": pending,
                          "display_cells": pending_cells,
                          "timestamp": turns[-1]["timestamp"] if turns else ""})
    _settle_stale_calls(turns)
    for turn in turns:
        cells = _coalesce_exploration(turn.pop("display_cells", []))
        for cell in cells:
            cell.pop("_explore", None)
        if cells:
            turn["display_cells"] = [_cap_cell(cell) for cell in cells]
    return turns


def parse_turns(path) -> list[dict]:
    """``path`` is either a jsonl file or ``opencode.db#session_id``."""
    raw = str(path)
    if "#" in raw and not pathlib.Path(raw).is_file():
        db, _, session_id = raw.partition("#")
        return _parse_db_session(pathlib.Path(db), session_id)
    path = pathlib.Path(path)
    if path.suffix == ".db":
        return []
    turns: list[dict] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        role = _role_of(row)
        if not role:
            continue
        turns.append({
            "role": role,
            "text": _text_of(row.get("content") or row.get("part") or row),
            "tools": [],
            "timestamp": str(row.get("time") or row.get("timestamp") or ""),
        })
    return turns


def _parse_db_session(db: pathlib.Path, session_id: str) -> list[dict]:
    if not db.is_file() or not session_id:
        return []
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error as e:
        log_exception("opencodeTranscriptOpenFail", e, detail=str(db))
        return []
    try:
        rows = con.execute(
            "SELECT id, time_created, data FROM message "
            "WHERE session_id = ? ORDER BY time_created, id",
            (session_id,),
        ).fetchall()
        parts = _parts_by_message(con, session_id)
    except sqlite3.Error as e:
        log_exception("opencodeTranscriptReadFail", e, detail=session_id)
        return []
    finally:
        con.close()
    return _turns_from_messages(
        [(str(created), data) for _id, created, data in rows],
        parts, [str(message_id) for message_id, _created, _data in rows])


def _parts_by_message(con: sqlite3.Connection, session_id: str) -> dict[str, list[dict]]:
    try:
        rows = con.execute(
            "SELECT message_id, data FROM part WHERE session_id = ? "
            "ORDER BY message_id, id",
            (session_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        # Databases written before OpenCode split parts out have no such table
        # and carry the text inside message.data instead.
        return {}
    grouped: dict[str, list[dict]] = {}
    for message_id, raw in rows:
        try:
            part = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(part, dict):
            grouped.setdefault(str(message_id), []).append(part)
    return grouped


def _first_user_text(con: sqlite3.Connection, session_id: str) -> str:
    """Preview for the session picker without parsing the whole conversation;
    a listing covers up to a hundred sessions, some with thousands of parts."""
    try:
        rows = con.execute(
            "SELECT p.data FROM part p JOIN message m ON m.id = p.message_id "
            "WHERE p.session_id = ? AND json_extract(m.data, '$.role') = 'user' "
            "AND json_extract(p.data, '$.type') = 'text' "
            "ORDER BY m.time_created, m.id, p.id LIMIT 5",
            (session_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        return ""
    for (raw,) in rows:
        try:
            part = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(part, dict) and not part.get("synthetic") and not part.get("ignored"):
            text = strip_voice_preamble(_text_of(part)).strip()
            if text:
                return text
    return ""


def find_latest_jsonl(session_id: str, *, home: pathlib.Path | None = None):
    if not session_id:
        return None
    db = db_path(home)
    if not db.is_file():
        return None
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            row = con.execute(
                "SELECT id FROM session WHERE id = ?", (session_id,)
            ).fetchone()
        finally:
            con.close()
    except sqlite3.Error as e:
        log_exception("opencodeSessionLookupFail", e, detail=session_id)
        return None
    if not row:
        return None
    return pathlib.Path(f"{db}#{session_id}")


def list_sessions(
    cwd: str,
    limit: int = 20,
    *,
    all_projects: bool = False,
    home: pathlib.Path | None = None,
) -> list[dict]:
    db = db_path(home)
    if not db.is_file():
        return []
    want = str(pathlib.Path(os.path.expanduser(cwd))) if cwd else ""
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error as e:
        log_exception("opencodeSessionListOpenFail", e, detail=str(db))
        return []
    try:
        sql = ("SELECT id, directory, title, time_updated FROM session "
               "WHERE time_archived IS NULL ")
        columns = {row[1] for row in con.execute("PRAGMA table_info(session)")}
        if "parent_id" in columns:
            # A ``task`` sub-agent runs in a child session; it is part of its
            # parent's conversation, not one to resume on its own.
            sql += "AND parent_id IS NULL "
        params: list[Any] = []
        if want and not all_projects:
            sql += "AND directory = ? "
            params.append(want)
        sql += "ORDER BY time_updated DESC LIMIT ?"
        params.append(limit)
        rows = con.execute(sql, params).fetchall()
        previews = {str(row[0]): _first_user_text(con, str(row[0])) for row in rows}
    except sqlite3.Error as e:
        log_exception("opencodeSessionListFail", e, detail=str(db))
        return []
    finally:
        con.close()
    out = []
    for session_id, directory, title, updated in rows:
        preview = previews.get(str(session_id), "")
        if not preview:
            # Older databases keep the text inside message.data.
            for turn in _parse_db_session(db, str(session_id)):
                if turn["role"] == "user" and turn["text"]:
                    preview = turn["text"]
                    break
        preview = preview[:240]
        mtime = int(updated or 0)
        if mtime > 10_000_000_000:
            mtime //= 1000
        out.append({
            "id": str(session_id), "mtime": mtime, "preview": preview,
            "title": str(title or ""), "cwd": str(directory or ""),
        })
    return out
