"""Parse a Codex `exec` rollout JSONL into transcript turns.

Codex persists each session to
~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl. Each line is an
envelope `{timestamp, type, payload}`. We map it onto the exact shape
lib/transcript_log.parse_turns produces for Claude, while also emitting
native Codex display cells for compact terminal-style rendering:

    {"role": "assistant", "text": str, "tools": [...],
     "display_cells": [...], "timestamp": str}

Text comes from `event_msg/user_message` + `event_msg/agent_message`
(the conversation as the user/agent see it); tools come from
`response_item/function_call` + `custom_tool_call` with their `*_output`
counterparts matched back by call_id.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import shlex
import sqlite3
from collections import Counter
from typing import Any

from .activity import summarize_tool_activity
from .log import log_exception


TOOL_OUTPUT_LINE_LIMIT = 5
TOOL_OUTPUT_CHAR_LIMIT = 220

# Belt-and-braces ceilings applied to every finished display cell, regardless
# of which builder produced it. A single untruncated tool output (a 2.5 MB
# line) once wedged the native renderer; the diff-card path also appends one
# line per raw diff line with no cap of its own. These bound any cell so no
# single one can balloon the transcript payload again.
CELL_LINE_CHAR_HARD_LIMIT = 2000    # absolute per-line text cap
CELL_LINE_COUNT_HARD_LIMIT = 600    # absolute lines-per-cell cap
CELL_BYTES_HARD_LIMIT = 64 * 1024   # absolute serialized-bytes-per-cell cap


def _codex_home() -> pathlib.Path:
    return pathlib.Path(os.environ.get("CODEX_HOME",
                        str(pathlib.Path.home() / ".codex")))


def truncate(s: Any, n: int = 600) -> str:
    if not isinstance(s, str):
        return ""
    return s if len(s) <= n else s[:n] + "…"


def _clean_user_text(msg: Any) -> str:
    """Codex echoes the user prompt prefixed with a '› ' marker and can
    double it ('› i'm planning to I am planning to…'); strip the marker."""
    if not isinstance(msg, str):
        return ""
    return msg.lstrip("›").strip()


def _exec_command_str(arguments: Any) -> str:
    """Pull a human-readable command out of an exec_command call's args."""
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            return truncate(arguments, 400)
    if isinstance(arguments, dict):
        cmd = arguments.get("cmd") or arguments.get("command")
        if isinstance(cmd, list):
            return truncate(" ".join(str(c) for c in cmd), 400)
        if isinstance(cmd, str):
            return truncate(cmd, 400)
    return ""


def _safe_id(prefix: str, value: str) -> str:
    clean = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in value)
    return f"{prefix}-{clean[:80]}" if clean else prefix


def _display_line(text: Any, label: str = "", kind: str = "detail") -> dict:
    return {
        "label": label,
        "text": str(text or ""),
        "kind": kind,
    }


def _display_cell(
    *,
    kind: str,
    title: str,
    summary: str = "",
    status: str = "recorded",
    cell_id: str = "",
    lines: list[dict] | None = None,
) -> dict:
    out: dict[str, Any] = {
        "id": cell_id or _safe_id(kind, summary or title),
        "kind": kind,
        "title": title,
        "summary": summary,
        "status": status,
        "lines": lines or [],
    }
    return out


def _shell_words(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


def _strip_shell_wrapper(command: str) -> str:
    words = _shell_words(command)
    shell = pathlib.PurePosixPath(words[0]).name if words else ""
    if len(words) >= 3 and shell in {"bash", "sh", "zsh"} and words[1] == "-lc":
        return words[2]
    return command


def _looks_like_path(value: str) -> bool:
    return (
        "/" in value
        or value.startswith(".")
        or "." in pathlib.PurePosixPath(value.replace("\\", "/")).name
    )


def _classify_exploration(command: str) -> dict | None:
    display = _strip_shell_wrapper(command).strip()
    if not display:
        return None
    if "&&" in display:
        for part in reversed([p.strip() for p in display.split("&&")]):
            if part in {"pwd", "true"}:
                continue
            line = _classify_exploration(part)
            if line:
                return line
        return None
    words = _shell_words(display)
    if not words:
        return None
    base = pathlib.PurePosixPath(words[0]).name
    args = words[1:]

    if base in {"rg", "grep", "git"}:
        if base == "git" and (not args or args[0] not in {"grep", "ls-files"}):
            return None
        if base == "git" and args[0] == "ls-files":
            return {"label": "List", "text": " ".join(args) or display}
        if base == "rg" and "--files" in args:
            scope = next((a for a in reversed(args) if _looks_like_path(a)), "")
            return {"label": "List", "text": scope or "rg --files"}
        query = next((a for a in args if not a.startswith("-")), "")
        scope = next((a for a in reversed(args) if _looks_like_path(a)), "")
        text = query if query else display
        if scope and scope != query:
            text = f"{text} in {scope}"
        return {"label": "Search", "text": text}

    if base in {"ls", "find", "fd"}:
        target = next((a for a in reversed(args) if not a.startswith("-")), "")
        return {"label": "List", "text": target or display}

    if base in {"cat", "sed", "nl", "head", "tail"}:
        # Prefer path-looking args, otherwise show the command compactly.
        paths = [a for a in args if not a.startswith("-") and _looks_like_path(a)]
        target = ", ".join(dict.fromkeys(paths))
        return {"label": "Read", "text": target or display}

    return None


def _preview_output(output: Any, limit: int = TOOL_OUTPUT_LINE_LIMIT) -> list[dict]:
    text = _clean_tool_output(output)
    if not text:
        return []
    raw_lines = text.splitlines() or [text]
    head = 0
    if len(raw_lines) <= limit:
        shown = raw_lines
        omitted = 0
    else:
        head = max(1, limit // 2)
        tail = max(1, limit - head - 1)
        omitted = len(raw_lines) - head - tail
        shown = raw_lines[:head] + raw_lines[-tail:]
    lines = [_display_line(_truncate_output_line(line), kind="output")
             for line in shown]
    if omitted:
        lines.insert(head, _display_line(
            f"… +{omitted} lines (full transcript in terminal)",
            kind="omitted",
        ))
    return lines


def _truncate_output_line(line: str, limit: int = TOOL_OUTPUT_CHAR_LIMIT) -> str:
    if len(line) <= limit:
        return line
    return line[:limit - 1].rstrip() + "…"


def _cap_cell(cell: dict) -> dict:
    """Bound a finished display cell's footprint: each line's length, the
    number of lines, and the total serialized bytes. Belt-and-braces over the
    per-builder caps so no single cell — a giant diff, an untruncated tool
    output, a future cell kind — can wedge the client renderer."""
    lines = cell.get("lines")
    if not isinstance(lines, list) or not lines:
        return cell
    capped: list[dict] = []
    budget = CELL_BYTES_HARD_LIMIT
    changed = truncated = False
    for line in lines:
        if len(capped) >= CELL_LINE_COUNT_HARD_LIMIT:
            truncated = changed = True
            break
        text = str(line.get("text") or "")
        if len(text) > CELL_LINE_CHAR_HARD_LIMIT:
            text = text[:CELL_LINE_CHAR_HARD_LIMIT - 1].rstrip() + "…"
            line = {**line, "text": text}
            changed = True
        budget -= len(text) + 64
        if budget < 0:
            truncated = changed = True
            break
        capped.append(line)
    if not changed:
        return cell
    if truncated:
        capped.append(_display_line(
            "… output truncated (full transcript in terminal)",
            kind="omitted",
        ))
    return {**cell, "lines": capped}


def _clean_tool_output(output: Any) -> str:
    if isinstance(output, list):
        parts = []
        for item in output:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
            elif isinstance(item, str):
                parts.append(item)
        text = "".join(parts) if parts else json.dumps(output)
    else:
        text = output if isinstance(output, str) else json.dumps(output)
    if not text:
        return ""
    marker = "\nOutput:\n"
    if marker in text:
        return text.split(marker, 1)[1]
    return text


def _tool_output_exit_code(output: Any) -> int | None:
    text = output if isinstance(output, str) else ""
    marker = "\nProcess exited with code "
    if marker not in text:
        return None
    tail = text.split(marker, 1)[1].splitlines()[0].strip()
    try:
        return int(tail)
    except ValueError:
        return None


def _command_cell(
    *,
    command: str,
    call_id: str = "",
    output: Any = "",
    exit_code: int | None = None,
    running: bool = False,
) -> dict:
    display = _strip_shell_wrapper(command)
    status = "running" if running else ("error" if exit_code not in (None, 0) else "ok")
    title = "Running" if running else "Ran"
    lines = _preview_output(output) if output else []
    if output == "" and not running:
        lines = [_display_line("(no output)", kind="muted")]
    return _display_cell(
        kind="command",
        title=title,
        summary=display,
        status=status,
        cell_id=call_id or _safe_id("command", display),
        lines=lines,
    )


def _patch_cell(call_id: str, patch: Any, status: str = "recorded") -> dict:
    if isinstance(patch, dict):
        nested = patch.get("changes") or patch.get("patch") or patch.get("input")
        if nested is not None:
            patch = nested
        else:
            # Codex's patch_apply_end event uses a path-keyed changes map:
            # {"path": {"type": "update", "unified_diff": "..."}}.
            # Preserve both the file heading and the actual diff so native
            # clients can render the same red/green edit cards as Claude.
            lines: list[dict] = []
            for path, change in patch.items():
                if not isinstance(change, dict):
                    continue
                kind = str(change.get("type") or change.get("kind") or "update")
                label = {
                    "add": "Add", "create": "Add",
                    "delete": "Delete", "remove": "Delete",
                    "update": "Edit",
                }.get(kind.lower(), "Edit")
                lines.append(_display_line(str(path), label))
                diff = str(change.get("unified_diff") or change.get("diff") or "")
                for raw in diff.splitlines():
                    if raw.startswith("@@"):
                        lines.append(_display_line(raw, kind="diff_header"))
                    elif raw.startswith("+") and not raw.startswith("+++"):
                        lines.append(_display_line(raw, kind="diff_new"))
                    elif raw.startswith("-") and not raw.startswith("---"):
                        lines.append(_display_line(raw, kind="diff_old"))
                    elif raw.startswith(" "):
                        lines.append(_display_line(raw, kind="diff_context"))
            return _display_cell(
                kind="patch", title="Edited", summary="apply_patch",
                status=status, cell_id=call_id or _safe_id("patch", "changes"),
                lines=lines,
            )
    if isinstance(patch, list):
        lines = []
        for change in patch:
            if not isinstance(change, dict):
                continue
            path = str(change.get("path") or "")
            kind = str(change.get("kind") or "update")
            label = {
                "add": "Add",
                "create": "Add",
                "delete": "Delete",
                "remove": "Delete",
                "update": "Edit",
            }.get(kind.lower(), "Edit")
            if path:
                lines.append(_display_line(path, label))
        return _display_cell(
            kind="patch",
            title="Edited",
            summary="apply_patch",
            status=status,
            cell_id=call_id or _safe_id("patch", json.dumps(patch)[:80]),
            lines=lines,
        )
    patch_text = patch if isinstance(patch, str) else ""
    lines = []
    for raw in patch_text.splitlines():
        if raw.startswith("*** Add File: "):
            lines.append(_display_line(raw.removeprefix("*** Add File: "), "Add"))
        elif raw.startswith("*** Update File: "):
            lines.append(_display_line(raw.removeprefix("*** Update File: "), "Edit"))
        elif raw.startswith("*** Delete File: "):
            lines.append(_display_line(raw.removeprefix("*** Delete File: "), "Delete"))
        elif raw.startswith("@@"):
            lines.append(_display_line(raw, kind="diff_header"))
        elif raw.startswith("+") and not raw.startswith("+++"):
            lines.append(_display_line(raw, kind="diff_new"))
        elif raw.startswith("-") and not raw.startswith("---"):
            lines.append(_display_line(raw, kind="diff_old"))
        elif raw.startswith(" "):
            lines.append(_display_line(raw, kind="diff_context"))
    if not lines and patch_text:
        lines = _preview_output(patch_text, limit=3)
    return _display_cell(
        kind="patch",
        title="Edited",
        summary="apply_patch",
        status=status,
        cell_id=call_id or _safe_id("patch", patch_text[:80]),
        lines=lines,
    )


def _patch_file_paths(patch_text: str) -> list[str]:
    paths: list[str] = []
    for raw in patch_text.splitlines():
        if raw.startswith("*** Add File: "):
            paths.append(raw.removeprefix("*** Add File: "))
        elif raw.startswith("*** Update File: "):
            paths.append(raw.removeprefix("*** Update File: "))
        elif raw.startswith("*** Delete File: "):
            paths.append(raw.removeprefix("*** Delete File: "))
    return list(dict.fromkeys(paths))


def _patch_edits(patch_text: str) -> list[dict[str, str]]:
    edits: list[dict[str, str]] = []
    old_lines: list[str] = []
    new_lines: list[str] = []

    def flush() -> None:
        nonlocal old_lines, new_lines
        if old_lines or new_lines:
            edits.append({
                "old": "\n".join(old_lines),
                "new": "\n".join(new_lines),
            })
        old_lines = []
        new_lines = []

    for raw in patch_text.splitlines():
        if raw.startswith("*** ") or raw.startswith("@@"):
            flush()
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            new_lines.append(raw[1:])
        elif raw.startswith("-") and not raw.startswith("---"):
            old_lines.append(raw[1:])
        elif raw.startswith(" "):
            context = raw[1:]
            old_lines.append(context)
            new_lines.append(context)

    flush()
    return [edit for edit in edits if edit["old"] != edit["new"]]


def _generic_tool_cell(name: str, payload: dict, status: str = "recorded") -> dict:
    call_id = str(payload.get("call_id") or payload.get("id") or "")
    return _display_cell(
        kind="tool",
        title="Called" if status != "running" else "Calling",
        summary=name,
        status=status,
        cell_id=call_id or _safe_id("tool", name),
    )


def _mcp_cell(item: dict, running: bool = False) -> dict:
    call_id = str(item.get("id") or item.get("call_id") or "")
    server = str(item.get("server") or "")
    tool = str(item.get("tool") or item.get("name") or "tool")
    args = item.get("arguments")
    args_text = ""
    if args not in (None, "", {}):
        try:
            args_text = json.dumps(args, separators=(",", ":"))
        except TypeError:
            args_text = str(args)
    invocation = f"{server + '.' if server else ''}{tool}({args_text})"
    status = "running" if running else ("error" if item.get("error") else "ok")
    lines: list[dict] = []
    result = item.get("result")
    if item.get("error"):
        err = item.get("error")
        lines = [_display_line(
            err.get("message") if isinstance(err, dict) else str(err),
            kind="error",
        )]
    elif result not in (None, ""):
        lines = _preview_output(result)
    return _display_cell(
        kind="mcp",
        title="Calling" if running else "Called",
        summary=invocation,
        status=status,
        cell_id=call_id or _safe_id("mcp", invocation),
        lines=lines,
    )


def _web_search_cell(call_id: str, query: Any = "", running: bool = False) -> dict:
    return _display_cell(
        kind="web_search",
        title="Searching" if running else "Searched",
        summary=str(query or "web"),
        status="running" if running else "ok",
        cell_id=call_id or _safe_id("web", str(query)),
    )


def _json_obj(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _token(value: Any) -> str:
    raw = str(value or "")
    out = []
    for i, ch in enumerate(raw):
        if ch in "- ":
            out.append("_")
        elif ch.isupper() and i > 0 and raw[i - 1] not in "_- ":
            out.extend(["_", ch.lower()])
        else:
            out.append(ch.lower())
    return "".join(out).strip("_")


def _get_any(mapping: dict, *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return default


def _status_text(status: Any) -> tuple[str, str]:
    """Normalize Codex subagent status variants.

    Upstream protocol status can be a plain snake_case string in projected
    items, or a message-bearing enum shape such as {"completed": "..."} in
    lower-level protocol events.
    """
    if isinstance(status, dict) and status:
        key, value = next(iter(status.items()))
        text = str(key or "").replace("_", " ").capitalize()
        if isinstance(value, str) and value.strip():
            return text, truncate(" ".join(value.split()), 240)
        return text, ""
    if isinstance(status, str):
        return _token(status).replace("_", " ").capitalize(), ""
    return "Recorded", ""


def _status_cell_state(status: Any, call_status: Any = None) -> str:
    status_key = ""
    if isinstance(status, dict) and status:
        status_key = _token(next(iter(status.keys())))
    elif isinstance(status, str):
        status_key = _token(status)
    call_key = _token(call_status)
    if status_key in {"errored", "error", "failed", "not_found"} or call_key == "failed":
        return "error"
    if status_key in {"running", "pending_init"} or call_key in {"in_progress", "running"}:
        return "running"
    if status_key in {"completed", "shutdown", "interrupted"} or call_key == "completed":
        return "ok"
    return "recorded"


def _agent_label(
    thread_id: Any = "",
    nickname: Any = "",
    role: Any = "",
) -> str:
    nickname_s = str(nickname or "").strip()
    role_s = str(role or "").strip()
    if nickname_s and role_s:
        return f"{nickname_s} [{role_s}]"
    if nickname_s:
        return nickname_s
    if role_s:
        return f"[{role_s}]"
    thread_s = str(thread_id or "").strip()
    return thread_s[:8] if thread_s else "Agent"


def _agent_state_lines(agents_states: Any) -> list[dict]:
    if not isinstance(agents_states, dict):
        return []
    lines: list[dict] = []
    for thread_id, state in sorted(agents_states.items(), key=lambda item: str(item[0])):
        if not isinstance(state, dict):
            state = {"status": state}
        status_label, message = _status_text(_get_any(state, "status"))
        state_message = _get_any(state, "message")
        if not message and isinstance(state_message, str) and state_message.strip():
            message = truncate(" ".join(state_message.split()), 240)
        label = _agent_label(
            thread_id,
            _get_any(state, "agent_nickname", "agentNickname", "nickname"),
            _get_any(state, "agent_role", "agentRole", "agent_type", "agentType", "role"),
        )
        text = status_label if not message else f"{status_label} - {message}"
        lines.append(_display_line(text, label, "status"))
    return lines


def _subagent_tool_cell(name: str, payload: dict, running: bool = False) -> dict | None:
    arguments = _json_obj(payload.get("arguments"))
    call_id = str(payload.get("call_id") or payload.get("id") or "")
    if name == "spawn_agent":
        role = str(arguments.get("agent_type") or arguments.get("agent_role") or "").strip()
        task = str(arguments.get("message") or arguments.get("prompt") or "").strip()
        lines = []
        if task:
            lines.append(_display_line(truncate(" ".join(task.split()), 240), "Task"))
        return _display_cell(
            kind="subagents",
            title="Spawning agent" if running else "Spawned agent",
            summary=role or "agent",
            status="running" if running else "recorded",
            cell_id=call_id,
            lines=lines,
        )
    # `wait_agent` is collaboration activity.  The outer `functions.wait`
    # helper instead resumes a yielded command cell and has no agent target.
    if name == "wait_agent" or (
        name == "wait" and ("target" in arguments or "targets" in arguments)
    ):
        targets = arguments.get("targets") or arguments.get("target") or []
        if isinstance(targets, str):
            targets = [targets]
        count = len(targets) if isinstance(targets, list) else 0
        return _display_cell(
            kind="subagents",
            title="Waiting for agents" if running else "Finished waiting",
            summary=f"{count} agent{'s' if count != 1 else ''}" if count else "agents",
            status="running" if running else "recorded",
            cell_id=call_id,
            lines=[
                _display_line(str(target)[:36], "Agent", "muted")
                for target in (targets if isinstance(targets, list) else [])
            ],
        )
    if name in {"send_input", "resume_agent", "close_agent"}:
        target = arguments.get("target") or arguments.get("id") or ""
        action = {
            "send_input": "Sent input to agent",
            "resume_agent": "Resumed agent",
            "close_agent": "Closed agent",
        }.get(name, "Updated agent")
        task = str(arguments.get("message") or "").strip()
        lines = []
        if target:
            lines.append(_display_line(str(target)[:36], "Agent", "muted"))
        if task:
            lines.append(_display_line(truncate(" ".join(task.split()), 240), "Input"))
        return _display_cell(
            kind="subagents",
            title=action,
            summary=str(target)[:8] if target else "agent",
            status="running" if running else "recorded",
            cell_id=call_id,
            lines=lines,
        )
    return None


def _subagent_output_cell(existing: dict, output: Any) -> dict:
    name = str(existing.get("title") or "")
    data = _json_obj(output)
    cell = dict(existing)
    lines = list(cell.get("lines") or [])

    if "agent_id" in data or "nickname" in data:
        label = _agent_label(
            data.get("agent_id"),
            data.get("nickname"),
            data.get("agent_role") or data.get("agent_type"),
        )
        cell["title"] = "Spawned agent"
        cell["summary"] = label
        cell["status"] = "ok"
        if data.get("agent_id"):
            lines.append(_display_line(str(data["agent_id"]), "Thread", "muted"))
    elif "status" in data:
        status_label, message = _status_text(data.get("status"))
        cell["status"] = _status_cell_state(data.get("status"))
        if "Waiting" in name:
            cell["title"] = "Finished waiting"
        text = status_label if not message else f"{status_label} - {message}"
        lines.append(_display_line(text, "Status", "status"))
    elif "completed" in data:
        cell["status"] = "ok"
        lines.append(_display_line(truncate(str(data["completed"]), 240), "Result", "status"))
    elif isinstance(output, str) and output.strip():
        cell["status"] = "ok"
        lines.append(_display_line(truncate(" ".join(output.split()), 240), "Result", "status"))
    else:
        cell["status"] = "ok"

    cell["lines"] = lines
    return cell


def _collab_details(item: dict) -> dict:
    details = item.get("details")
    if isinstance(details, dict):
        return details
    return item


def _collab_tool_cell(item: dict, running: bool = False) -> dict | None:
    details = _collab_details(item)
    itype = _token(details.get("type") or item.get("type"))
    if itype not in {"collab_tool_call", "collab_agent_tool_call"}:
        return None

    tool = _token(_get_any(details, "tool"))
    call_status = _get_any(details, "status")
    receivers = _get_any(details, "receiver_thread_ids", "receiverThreadIds", default=[]) or []
    if isinstance(receivers, str):
        receivers = [receivers]
    agents_states = _get_any(details, "agents_states", "agentsStates", default={}) or {}
    first_receiver = receivers[0] if receivers else ""
    status = "running" if running else _status_cell_state(None, call_status)
    prompt = str(_get_any(details, "prompt") or "").strip()

    title = {
        "spawn_agent": "Spawning agent" if running else "Spawned agent",
        "send_input": "Sending input to agent" if running else "Sent input to agent",
        "wait": "Waiting for agents" if running else "Finished waiting",
        "resume_agent": "Resuming agent" if running else "Resumed agent",
        "close_agent": "Closing agent" if running else "Closed agent",
    }.get(tool, "Agent activity")

    lines: list[dict] = []
    if prompt:
        lines.append(_display_line(truncate(" ".join(prompt.split()), 240), "Task"))
    lines.extend(_agent_state_lines(agents_states))
    if first_receiver and not lines:
        lines.append(_display_line(str(first_receiver), "Agent", "muted"))

    summary = ""
    if tool == "wait" and len(receivers) > 1:
        summary = f"{len(receivers)} agents"
    elif first_receiver:
        summary = str(first_receiver)[:8]

    return _display_cell(
        kind="subagents",
        title=title,
        summary=summary,
        status=status,
        cell_id=str(_get_any(item, "id") or _get_any(details, "id", "call_id", "callId") or ""),
        lines=lines,
    )


def _collab_protocol_cell(event_type: str, payload: dict, running: bool = False) -> dict | None:
    call_id = str(payload.get("call_id") or "")
    if event_type in {"collab_agent_spawn_begin", "collab_agent_spawn_end"}:
        thread_id = payload.get("new_thread_id")
        nickname = payload.get("new_agent_nickname")
        role = payload.get("new_agent_role") or payload.get("agent_type")
        status = payload.get("status") if "end" in event_type else "running"
        prompt = str(payload.get("prompt") or "").strip()
        lines = []
        if prompt:
            lines.append(_display_line(truncate(" ".join(prompt.split()), 240), "Task"))
        if thread_id:
            lines.append(_display_line(str(thread_id), "Thread", "muted"))
        return _display_cell(
            kind="subagents",
            title="Spawning agent" if event_type.endswith("_begin") else "Spawned agent",
            summary=_agent_label(thread_id, nickname, role),
            status="running" if event_type.endswith("_begin") else _status_cell_state(status),
            cell_id=call_id,
            lines=lines,
        )
    if event_type in {"collab_agent_interaction_begin", "collab_agent_interaction_end"}:
        thread_id = payload.get("receiver_thread_id")
        nickname = payload.get("receiver_agent_nickname")
        role = payload.get("receiver_agent_role") or payload.get("agent_type")
        status = payload.get("status") if "end" in event_type else "running"
        prompt = str(payload.get("prompt") or "").strip()
        lines = []
        if prompt:
            lines.append(_display_line(truncate(" ".join(prompt.split()), 240), "Input"))
        return _display_cell(
            kind="subagents",
            title="Sending input to agent" if event_type.endswith("_begin") else "Sent input to agent",
            summary=_agent_label(thread_id, nickname, role),
            status="running" if event_type.endswith("_begin") else _status_cell_state(status),
            cell_id=call_id,
            lines=lines,
        )
    if event_type in {"collab_waiting_begin", "collab_waiting_end"}:
        receivers = payload.get("receiver_thread_ids") or []
        if isinstance(receivers, str):
            receivers = [receivers]
        lines: list[dict] = []
        if event_type.endswith("_begin"):
            receiver_agents = payload.get("receiver_agents") or []
            for i, thread_id in enumerate(receivers):
                meta = receiver_agents[i] if (
                    isinstance(receiver_agents, list)
                    and i < len(receiver_agents)
                    and isinstance(receiver_agents[i], dict)
                ) else {}
                lines.append(_display_line(
                    str(thread_id),
                    _agent_label(
                        thread_id,
                        meta.get("agent_nickname") or meta.get("agentNickname"),
                        meta.get("agent_role") or meta.get("agentRole") or meta.get("agent_type"),
                    ),
                    "muted",
                ))
            status = "running"
        else:
            statuses = payload.get("statuses") or {}
            agent_statuses = payload.get("agent_statuses") or payload.get("agentStatuses") or []
            if isinstance(agent_statuses, list) and agent_statuses:
                for entry in agent_statuses:
                    if not isinstance(entry, dict):
                        continue
                    thread_id = entry.get("thread_id") or entry.get("threadId")
                    status_label, message = _status_text(entry.get("status"))
                    text = status_label if not message else f"{status_label} - {message}"
                    lines.append(_display_line(
                        text,
                        _agent_label(
                            thread_id,
                            entry.get("agent_nickname") or entry.get("agentNickname"),
                            entry.get("agent_role") or entry.get("agentRole") or entry.get("agent_type"),
                        ),
                        "status",
                    ))
            elif isinstance(statuses, dict):
                lines.extend(_agent_state_lines({
                    thread_id: {"status": state}
                    for thread_id, state in statuses.items()
                }))
            status = "error" if any(
                line.get("text", "").lower().startswith(("errored", "not found", "error"))
                for line in lines
            ) else "ok"
        count = len(receivers) or len(lines)
        return _display_cell(
            kind="subagents",
            title="Waiting for agents" if event_type.endswith("_begin") else "Finished waiting",
            summary=f"{count} agent{'s' if count != 1 else ''}" if count else "agents",
            status=status,
            cell_id=call_id,
            lines=lines,
        )
    if event_type in {
        "collab_close_begin", "collab_close_end",
        "collab_resume_begin", "collab_resume_end",
    }:
        thread_id = payload.get("receiver_thread_id")
        nickname = payload.get("receiver_agent_nickname")
        role = payload.get("receiver_agent_role") or payload.get("agent_type")
        status = payload.get("status") if event_type.endswith("_end") else "running"
        action = "Close" if event_type.startswith("collab_close") else "Resume"
        title = {
            ("Close", True): "Closing agent",
            ("Close", False): "Closed agent",
            ("Resume", True): "Resuming agent",
            ("Resume", False): "Resumed agent",
        }[(action, event_type.endswith("_begin"))]
        lines = []
        if thread_id:
            lines.append(_display_line(str(thread_id), "Thread", "muted"))
        return _display_cell(
            kind="subagents",
            title=title,
            summary=_agent_label(thread_id, nickname, role),
            status="running" if event_type.endswith("_begin") else _status_cell_state(status),
            cell_id=call_id,
            lines=lines,
        )
    if event_type == "sub_agent_activity":
        kind = _token(payload.get("kind"))
        path = str(payload.get("agent_path") or payload.get("agentPath") or "").strip()
        title = {
            "started": "Started subagent",
            "interacted": "Interacted with subagent",
            "interrupted": "Interrupted subagent",
        }.get(kind, "Subagent activity")
        return _display_cell(
            kind="subagents",
            title=title,
            summary=path,
            status="error" if kind == "interrupted" else "ok",
            cell_id=str(payload.get("event_id") or payload.get("eventId") or call_id),
            lines=[],
        )
    return None


def _replace_or_append_cell(turn: dict, cell: dict) -> None:
    cells = turn.setdefault("display_cells", [])
    cell_id = cell.get("id")
    if cell_id:
        for i, existing in enumerate(cells):
            if existing.get("id") == cell_id:
                cells[i] = cell
                return
    cells.append(cell)


def _codex_tool(name: str, payload: dict) -> dict:
    """Build a UI tool dict from a codex function_call / custom_tool_call."""
    call_id = payload.get("call_id") or payload.get("id") or ""
    out: dict[str, Any] = {
        "name": name,
        "summary": name,
        "action": name,
        "file_path": "",
        "status": "recorded",
    }
    if call_id:
        out["id"] = call_id
    if name in ("exec_command", "shell", "local_shell"):
        cmd = _exec_command_str(payload.get("arguments"))
        out.update({"name": "Bash", "command": cmd,
                    "summary": cmd[:80], "action": "run"})
    elif name == "apply_patch":
        patch = payload.get("input")
        if isinstance(patch, str):
            paths = _patch_file_paths(patch)
            edits = _patch_edits(patch)
            out.update({"name": "Edit", "summary": "apply_patch",
                        "action": "edit",
                        "content": truncate(patch, 600)})
            if paths:
                out["file_path"] = paths[0]
                out["summary"] = ", ".join(paths[:3])
            if len(edits) == 1:
                out["old"] = edits[0]["old"]
                out["new"] = edits[0]["new"]
            elif len(edits) > 1:
                out["name"] = "MultiEdit"
                out["edits"] = edits
                out["edit_count"] = len(edits)
    elif name in ("web_search", "web_search_call"):
        out.update({"name": "WebSearch", "action": "search"})
    else:
        # Generic: surface a compact summary via the shared summariser.
        try:
            readable = summarize_tool_activity(name, {})
            out["summary"] = readable.get("summary") or name
            out["action"] = readable.get("action") or name
        except Exception:                                  # noqa: BLE001
            pass
    return out


def _attach_output(turns: list[dict], call_id: str, output: Any) -> None:
    text = _clean_tool_output(output)
    exit_code = _tool_output_exit_code(output)
    for turn in reversed(turns):
        for tool in reversed(turn.get("tools") or []):
            if call_id and tool.get("id") != call_id:
                continue
            if tool.get("status") in ("ok", "error"):
                continue
            tool["status"] = "ok"
            tool["result"] = truncate(text, 300)
            break
        for i, cell in enumerate(reversed(turn.get("display_cells") or [])):
            if call_id and cell.get("id") != call_id:
                continue
            real_index = len(turn.get("display_cells") or []) - i - 1
            if cell.get("kind") == "command":
                turn["display_cells"][real_index] = _command_cell(
                    command=str(cell.get("summary") or ""),
                    call_id=str(cell.get("id") or call_id),
                    output=text,
                    exit_code=exit_code,
                )
            elif cell.get("kind") == "patch":
                cell = dict(cell)
                cell["status"] = "ok"
                turn["display_cells"][real_index] = cell
            elif cell.get("kind") == "subagents":
                turn["display_cells"][real_index] = _subagent_output_cell(cell, output)
            else:
                cell = dict(cell)
                cell["status"] = "ok"
                cell["title"] = "Called" if cell.get("title") == "Calling" else cell.get("title", "Called")
                cell["lines"] = _preview_output(text)
                turn["display_cells"][real_index] = cell
            return


def _last_assistant_turn(turns: list[dict], timestamp: str) -> dict:
    """Return the latest assistant turn, creating an empty one if the most
    recent turn is a user turn (tool calls need somewhere to hang)."""
    if turns and turns[-1].get("role") == "assistant":
        return turns[-1]
    turn = {
        "role": "assistant", "text": "", "tools": [],
        "display_cells": [], "timestamp": timestamp,
    }
    turns.append(turn)
    return turn


def _coalesce_exploration_cells(turns: list[dict]) -> None:
    for turn in turns:
        cells = turn.get("display_cells")
        if not isinstance(cells, list) or not cells:
            continue
        out: list[dict] = []
        pending: list[dict] = []

        def flush_pending() -> None:
            nonlocal pending
            if not pending:
                return
            running = any(line.get("status") == "running" for line in pending)
            out.append(_display_cell(
                kind="exploration",
                title="Exploring" if running else "Explored",
                status="running" if running else "ok",
                cell_id=_safe_id("exploration", "|".join(
                    str(p.get("text") or "") for p in pending
                )),
                lines=[
                    _display_line(
                        p.get("text") or "",
                        str(p.get("label") or ""),
                    )
                    for p in pending
                ],
            ))
            pending = []

        for cell in cells:
            if cell.get("kind") == "command":
                command = str(cell.get("summary") or "")
                line = _classify_exploration(command)
                if line:
                    line["status"] = cell.get("status")
                    pending.append(line)
                    continue
            flush_pending()
            out.append(cell)
        flush_pending()
        turn["display_cells"] = out


def _handle_modern_item(turns: list[dict], etype: str, item: dict, ts: str) -> None:
    item = _normalize_official_item(item)
    itype = str(item.get("type") or "")
    if itype in {"user_message", "agent_message"}:
        if etype != "item.completed":
            return
        text = (
            item.get("text") or item.get("message")
            or _official_item_text(item.get("content"))
        ).strip()
        if text:
            _append_visible_message(
                turns,
                role="user" if itype == "user_message" else "assistant",
                text=text,
                timestamp=ts,
                kind=item.get("phase"),
            )
        return
    if itype == "reasoning":
        summary = item.get("summary") or item.get("summary_text") or []
        if isinstance(summary, str):
            summary = [summary]
        lines = [
            _display_line(part, kind="status")
            for part in summary if isinstance(part, str) and part.strip()
        ]
        if lines:
            _replace_or_append_cell(
                _last_assistant_turn(turns, ts),
                _display_cell(
                    kind="reasoning", title="Reasoned", status="ok",
                    cell_id=str(item.get("id") or ""), lines=lines,
                ),
            )
        return
    if itype == "plan":
        text = str(item.get("text") or "").strip()
        if text:
            _replace_or_append_cell(
                _last_assistant_turn(turns, ts),
                _display_cell(
                    kind="plan", title="Plan", summary=text.splitlines()[0],
                    status="ok", cell_id=str(item.get("id") or ""),
                    lines=[_display_line(line) for line in text.splitlines()],
                ),
            )
        return
    if itype == "error":
        return

    turn = _last_assistant_turn(turns, ts)
    running = etype != "item.completed"
    call_id = str(item.get("id") or item.get("call_id") or "")

    if itype in {"command_execution", "local_shell_call"}:
        command_value = item.get("command") or ""
        command = (
            shlex.join(str(part) for part in command_value)
            if isinstance(command_value, list)
            else str(command_value)
        )
        output = item.get("aggregated_output")
        if output is None:
            output = item.get("output") or item.get("stdout") or item.get("stderr") or ""
        exit_code = item.get("exit_code")
        try:
            exit_code_int = int(exit_code) if exit_code is not None else None
        except (TypeError, ValueError):
            exit_code_int = None
        _replace_or_append_cell(turn, _command_cell(
            command=command,
            call_id=call_id,
            output=output,
            exit_code=exit_code_int,
            running=running,
        ))
        return

    if itype == "mcp_tool_call":
        _replace_or_append_cell(turn, _mcp_cell(item, running=running))
        return

    if itype == "dynamic_tool_call":
        name = str(item.get("tool") or item.get("name") or "dynamic tool")
        status = str(item.get("status") or "").lower()
        result = item.get("content_items") or item.get("result") or ""
        cell = _generic_tool_cell(
            name, item,
            status="running" if running else (
                "error" if item.get("success") is False or status == "failed" else "ok"
            ),
        )
        if result:
            cell["lines"] = _preview_output(result)
        _replace_or_append_cell(turn, cell)
        return

    if itype in {"web_search", "web_search_call"}:
        query = item.get("query") or item.get("action") or ""
        _replace_or_append_cell(turn, _web_search_cell(call_id, query, running=running))
        return

    if itype in {"file_change", "patch_apply"}:
        patch = item.get("patch") or item.get("changes") or item.get("input") or ""
        _replace_or_append_cell(turn, _patch_cell(
            call_id,
            patch,
            status="running" if running else (
                "error" if str(item.get("status") or "").lower() == "failed" else "ok"
            ),
        ))
        return

    if itype == "image_view":
        path = str(item.get("path") or "")
        _replace_or_append_cell(turn, _display_cell(
            kind="image", title="Viewed image", summary=path,
            status="ok", cell_id=call_id or _safe_id("image", path),
        ))
        return

    if itype == "image_generation":
        path = str(item.get("saved_path") or "")
        status = str(item.get("status") or "").lower()
        _replace_or_append_cell(turn, _display_cell(
            kind="image", title="Generated image", summary=path,
            status="error" if status == "failed" else ("running" if running else "ok"),
            cell_id=call_id or _safe_id("image-generation", path),
        ))
        return

    collab_cell = _collab_tool_cell(item, running=running)
    if collab_cell:
        _replace_or_append_cell(turn, collab_cell)
        return

    if _token(itype) == "sub_agent_activity":
        activity_cell = _collab_protocol_cell("sub_agent_activity", item)
        if activity_cell:
            _replace_or_append_cell(turn, activity_cell)
            return

    if itype in {"function_call", "tool_call"}:
        name = str(item.get("name") or item.get("tool") or "tool")
        subagent_cell = _subagent_tool_cell(name, item, running=running)
        if subagent_cell:
            _replace_or_append_cell(turn, subagent_cell)
            return
        _replace_or_append_cell(turn, _generic_tool_cell(
            name, item, status="running" if running else "ok",
        ))


_OFFICIAL_ITEM_TYPES = {
    "UserMessage": "user_message",
    "AgentMessage": "agent_message",
    "CommandExecution": "command_execution",
    "FileChange": "file_change",
    "McpToolCall": "mcp_tool_call",
    "DynamicToolCall": "dynamic_tool_call",
    "CollabToolCall": "collab_tool_call",
    "CollabAgentToolCall": "collab_agent_tool_call",
    "WebSearch": "web_search",
    "ImageView": "image_view",
    "ImageGeneration": "image_generation",
    "Reasoning": "reasoning",
    "Plan": "plan",
}


def _normalize_official_item(item: dict) -> dict:
    out = dict(item)
    details = out.get("details")
    if isinstance(details, dict):
        out = {**out, **details}
    raw_type = str(out.get("type") or "")
    out["type"] = _OFFICIAL_ITEM_TYPES.get(raw_type, _token(raw_type))
    aliases = {
        "process_id": "processId", "parsed_cmd": "parsedCmd",
        "aggregated_output": "aggregatedOutput", "exit_code": "exitCode",
        "duration_ms": "durationMs", "command_actions": "commandActions",
        "sender_thread_id": "senderThreadId",
        "receiver_thread_ids": "receiverThreadIds",
        "new_thread_id": "newThreadId", "agents_states": "agentsStates",
        "content_items": "contentItems", "saved_path": "savedPath",
        "summary_text": "summaryText", "raw_content": "rawContent",
    }
    for snake, camel in aliases.items():
        if snake not in out and camel in out:
            out[snake] = out[camel]
    return out


def _official_item_text(content: Any) -> str:
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        if _token(part.get("type")) not in {"text", "input_text", "output_text"}:
            continue
        text = part.get("text")
        if isinstance(text, str) and text:
            parts.append(text)
    return "\n".join(parts).strip()


def _official_item_category(item: dict) -> str:
    return {
        "command_execution": "command",
        "local_shell_call": "command",
        "file_change": "patch",
        "patch_apply": "patch",
        "mcp_tool_call": "mcp",
        "dynamic_tool_call": "tool",
        "collab_tool_call": "subagents",
        "collab_agent_tool_call": "subagents",
        "web_search": "web_search",
        "web_search_call": "web_search",
        "image_view": "image",
        "image_generation": "image",
        "plan": "plan",
        "reasoning": "reasoning",
    }.get(str(item.get("type") or ""), "")


_NESTED_TOOL_RE = re.compile(r"\btools\.([A-Za-z_][A-Za-z0-9_]*)\s*\(")
_JSON_CMD_RE = re.compile(
    r'(?:"(?:cmd|command)"|\b(?:cmd|command))\s*:\s*("(?:\\.|[^"\\])*")'
)


def _legacy_tool_category(name: str, payload: dict) -> tuple[str, str]:
    normalized = _token(name)
    if normalized in {"exec_command", "shell", "local_shell"}:
        return "command", normalized
    if normalized == "apply_patch":
        return "patch", normalized
    if normalized in {"web_search", "web_search_call", "web__run"}:
        return "web_search", normalized
    if normalized in {"view_image", "image_gen__imagegen"}:
        return "image", normalized
    if normalized in {"wait", "write_stdin"}:
        return "status", normalized
    if normalized in {
        "spawn_agent", "send_message", "followup_task", "wait_agent",
        "list_agents", "interrupt_agent",
    }:
        return "subagents", normalized
    if normalized != "exec":
        return "tool", normalized
    source = str(payload.get("input") or payload.get("arguments") or "")
    nested = [_token(match) for match in _NESTED_TOOL_RE.findall(source)]
    priority = [
        ("apply_patch", "patch"),
        ("exec_command", "command"),
        ("web__run", "web_search"),
        ("view_image", "image"),
        ("image_gen__imagegen", "image"),
        ("write_stdin", "status"),
        ("wait", "status"),
        ("spawn_agent", "subagents"),
        ("send_message", "subagents"),
        ("followup_task", "subagents"),
        ("wait_agent", "subagents"),
    ]
    for tool, category in priority:
        if tool in nested:
            return category, tool
    return "tool", (nested[0] if nested else "exec")


def _nested_exec_cell(payload: dict, call_id: str) -> dict:
    category, nested = _legacy_tool_category("exec", payload)
    source = str(payload.get("input") or payload.get("arguments") or "")
    if category == "command":
        match = _JSON_CMD_RE.search(source)
        command = ""
        if match:
            try:
                command = json.loads(match.group(1))
            except json.JSONDecodeError:
                command = ""
        return _command_cell(
            command=command or "command", call_id=call_id, running=True)
    if category == "patch":
        return _display_cell(
            kind="patch", title="Editing", summary="apply_patch",
            status="running", cell_id=call_id,
        )
    if category == "web_search":
        return _web_search_cell(call_id, "web", running=True)
    if category == "image":
        return _display_cell(
            kind="image", title="Viewing image", summary=nested,
            status="running", cell_id=call_id,
        )
    if category == "subagents":
        return _display_cell(
            kind="subagents", title="Agent activity", summary=nested,
            status="running", cell_id=call_id,
        )
    if category == "status":
        return _display_cell(
            kind="status",
            title="Continuing command" if nested == "write_stdin" else "Waiting",
            summary="background command", status="running", cell_id=call_id,
        )
    return _generic_tool_cell(nested, payload, status="running")


def _legacy_function_cell(name: str, payload: dict, call_id: str) -> dict:
    token = _token(name)
    namespace = _token(payload.get("namespace"))
    arguments = _json_obj(payload.get("arguments"))
    if token == "update_plan":
        plan = arguments.get("plan") or []
        lines = []
        if isinstance(plan, list):
            for entry in plan:
                if not isinstance(entry, dict):
                    continue
                lines.append(_display_line(
                    entry.get("step") or "",
                    str(entry.get("status") or "").replace("_", " ").title(),
                    "status",
                ))
        return _display_cell(
            kind="plan", title="Updated plan", status="ok",
            cell_id=call_id, lines=lines,
        )
    if token in {"get_goal", "create_goal", "update_goal"}:
        return _display_cell(
            kind="plan",
            title={
                "get_goal": "Checked goal",
                "create_goal": "Created goal",
                "update_goal": "Updated goal",
            }[token],
            summary=str(arguments.get("objective") or arguments.get("status") or ""),
            status="ok", cell_id=call_id,
        )
    if token == "wait":
        return _display_cell(
            kind="status", title="Waiting", summary="background work",
            status="running", cell_id=call_id,
        )
    if token == "write_stdin":
        return _display_cell(
            kind="status", title="Continuing command",
            summary=str(arguments.get("session_id") or arguments.get("cell_id") or ""),
            status="running", cell_id=call_id,
        )
    if token in {"run", "web__run"} and namespace in {"web", ""}:
        query = ""
        raw = arguments or _json_obj(payload.get("input"))
        searches = raw.get("search_query") if isinstance(raw, dict) else None
        if isinstance(searches, list) and searches and isinstance(searches[0], dict):
            query = str(searches[0].get("q") or "")
        return _web_search_cell(call_id, query or "web", running=True)
    if token in {"view_image", "image_gen__imagegen"}:
        return _display_cell(
            kind="image",
            title="Viewing image" if token == "view_image" else "Generating image",
            summary=str(arguments.get("path") or arguments.get("prompt") or "")[:240],
            status="running", cell_id=call_id,
        )
    if token in {
        "spawn_agent", "send_message", "followup_task", "wait_agent",
        "list_agents", "interrupt_agent",
    }:
        cell = _subagent_tool_cell(name, payload, running=True)
        if cell:
            return cell
    return _generic_tool_cell(name, payload)


def _turn_id(payload: dict) -> str:
    metadata = payload.get("internal_chat_message_metadata_passthrough")
    if not isinstance(metadata, dict):
        metadata = {}
    return str(payload.get("turn_id") or metadata.get("turn_id") or "")


def _mark_legacy(value: dict, *, turn_id: str, category: str) -> dict:
    value["_legacy_turn_id"] = turn_id
    value["_legacy_category"] = category
    return value


def _drop_superseded_legacy_items(
    turns: list[dict], structured: Counter[tuple[str, str]],
) -> None:
    for turn in turns:
        for bucket in ("display_cells", "tools"):
            remaining = structured.copy()
            kept = []
            for value in turn.get(bucket) or []:
                key = (
                    str(value.get("_legacy_turn_id") or ""),
                    str(value.get("_legacy_category") or ""),
                )
                if value.get("_legacy_turn_id") and remaining[key] > 0:
                    remaining[key] -= 1
                    continue
                kept.append(value)
            turn[bucket] = kept
        for value in [*(turn.get("display_cells") or []), *(turn.get("tools") or [])]:
            value.pop("_legacy_turn_id", None)
            value.pop("_legacy_category", None)


def _response_message_text(payload: dict) -> str:
    content = payload.get("content")
    if not isinstance(content, list):
        return ""
    parts = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") not in {"input_text", "output_text", "text"}:
            continue
        text = item.get("text")
        if isinstance(text, str) and text:
            parts.append(text)
    return "\n".join(parts).strip()


def _is_injected_user_context(text: str) -> bool:
    """True for Codex bootstrap/turn context recorded with role=user.

    These blocks are model input supplied by the host, not chat authored by
    User. A real question that quotes one remains visible because it starts
    with the user's own prose rather than an injected block marker.
    """
    return text.startswith((
        "# AGENTS.md instructions for ",
        "<environment_context>",
        "<recommended_plugins>",
    ))


def _append_visible_message(turns: list[dict], *, role: str, text: str,
                            timestamp: str, kind: Any = None) -> None:
    """Append one visible chat message, deduplicating dual event formats."""
    if not text or role not in {"user", "assistant"}:
        return
    if turns and turns[-1].get("role") == role and turns[-1].get("text") == text:
        if kind and not turns[-1].get("kind"):
            turns[-1]["kind"] = kind
        return
    turns.append({
        "role": role, "text": text, "tools": [], "display_cells": [],
        "timestamp": timestamp, **({"kind": kind} if kind else {}),
    })


def parse_turns(path: pathlib.Path) -> list[dict]:
    """Read a Codex rollout JSONL and return user/assistant turns.

    Raises OSError if the file can't be read."""
    turns: list[dict] = []
    structured_items: Counter[tuple[str, str]] = Counter()
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError as e:
                log_exception("codexTranscriptLineSkip", e, detail=str(path))
                continue
            if not isinstance(ev, dict):
                continue
            ts = str(ev.get("timestamp") or "")
            payload = ev.get("payload")
            if not isinstance(payload, dict):
                payload = {}
            inner = payload.get("type") or ev.get("type") or ""

            if inner == "message":
                role = str(payload.get("role") or "")
                text = _response_message_text(payload)
                # Codex records the synthetic AGENTS/environment bootstrap as
                # a user-role message. It is backend context, not a message
                # User authored, and must stay out of chat history.
                if role == "user" and _is_injected_user_context(text):
                    continue
                _append_visible_message(
                    turns, role=role, text=text, timestamp=ts,
                    kind=payload.get("phase"),
                )
                continue

            if inner in ("item.started", "item.updated", "item.completed"):
                item = ev.get("item") or payload.get("item")
                if isinstance(item, dict):
                    _handle_modern_item(turns, inner, item, ts)
                continue

            if inner in ("item_started", "item_updated", "item_completed"):
                item = payload.get("item") or ev.get("item")
                if isinstance(item, dict):
                    normalized_item = _normalize_official_item(item)
                    category = _official_item_category(normalized_item)
                    if (
                        inner == "item_completed" and category
                        and _turn_id(payload)
                    ):
                        structured_items[(_turn_id(payload), category)] += 1
                    _handle_modern_item(
                        turns, inner.replace("_", "."), normalized_item, ts)
                continue

            if isinstance(inner, str) and (
                inner.startswith("collab_agent_")
                or inner in {
                    "collab_waiting_begin",
                    "collab_waiting_end",
                    "collab_close_begin",
                    "collab_close_end",
                    "collab_resume_begin",
                    "collab_resume_end",
                    "sub_agent_activity",
                }
            ):
                cell = _collab_protocol_cell(
                    inner,
                    payload if payload else ev,
                    running=inner.endswith("_begin"),
                )
                if cell:
                    _replace_or_append_cell(_last_assistant_turn(turns, ts), cell)
                continue

            if inner == "user_message":
                from .codex_runner import strip_voice_preamble
                text = strip_voice_preamble(
                    _clean_user_text(payload.get("message") or ""))
                _append_visible_message(
                    turns, role="user", text=text, timestamp=ts)
            elif inner == "agent_message":
                text = (payload.get("message") or "").strip()
                _append_visible_message(
                    turns, role="assistant", text=text, timestamp=ts,
                    kind=payload.get("phase"),
                )
            elif inner in ("function_call", "custom_tool_call"):
                name = str(payload.get("name") or "tool")
                turn = _last_assistant_turn(turns, ts)
                category, nested_name = _legacy_tool_category(name, payload)
                legacy_turn_id = _turn_id(payload)
                tool_payload = dict(payload)
                if _token(name) == "exec" and nested_name != "exec":
                    tool_payload["name"] = nested_name
                turn.setdefault("tools", []).append(_mark_legacy(
                    _codex_tool(
                        nested_name if _token(name) == "exec" else name,
                        tool_payload,
                    ),
                    turn_id=legacy_turn_id,
                    category=category,
                ))
                call_id = str(payload.get("call_id") or payload.get("id") or "")
                if name in ("exec_command", "shell", "local_shell"):
                    cmd = _exec_command_str(payload.get("arguments"))
                    cell = _command_cell(
                        command=cmd,
                        call_id=call_id,
                        running=True,
                    )
                elif name == "apply_patch":
                    cell = None
                elif name in ("web_search", "web_search_call"):
                    cell = _web_search_cell(
                        call_id,
                        payload.get("query") or payload.get("input") or "web",
                        running=True,
                    )
                elif (subagent_cell := _subagent_tool_cell(
                    name, payload, running=True
                )):
                    cell = subagent_cell
                elif _token(name) == "exec":
                    cell = _nested_exec_cell(payload, call_id)
                else:
                    cell = _legacy_function_cell(name, payload, call_id)
                if cell is not None:
                    _replace_or_append_cell(turn, _mark_legacy(
                        cell, turn_id=legacy_turn_id, category=category))
            elif inner == "patch_apply_end":
                turn = _last_assistant_turn(turns, ts)
                if _turn_id(payload):
                    structured_items[(_turn_id(payload), "patch")] += 1
                call_id = str(payload.get("call_id") or "")
                changes = payload.get("changes")
                if isinstance(changes, dict) and changes:
                    _replace_or_append_cell(turn, _patch_cell(
                        call_id,
                        changes,
                        status="ok" if payload.get("success", True) else "error",
                    ))
                _attach_output(turns, call_id,
                               payload.get("output") or payload.get("stdout") or "")
            elif inner in ("function_call_output", "custom_tool_call_output"):
                _attach_output(turns, payload.get("call_id") or "",
                               payload.get("output") or payload.get("stdout") or "")
    _drop_superseded_legacy_items(turns, structured_items)
    _coalesce_exploration_cells(turns)
    for turn in turns:
        cells = turn.get("display_cells")
        if isinstance(cells, list) and cells:
            turn["display_cells"] = [_cap_cell(c) for c in cells]
    return turns


def _head_session_meta_and_preview(
    path: pathlib.Path, max_lines: int = 80
) -> tuple[str, str, str]:
    """Read the head of a rollout file and return (session_id, cwd, preview).

    session_id + cwd come from the `session_meta` line; preview is the
    first user message. Bounded to `max_lines` so listing many sessions
    stays cheap."""
    sid = cwd = preview = ""
    try:
        with path.open(encoding="utf-8") as fp:
            for i, line in enumerate(fp):
                if i >= max_lines:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(ev, dict):
                    continue
                payload = ev.get("payload")
                if not isinstance(payload, dict):
                    payload = {}
                inner = payload.get("type") or ev.get("type") or ""
                if inner == "session_meta" or ev.get("type") == "session_meta":
                    sid = str(payload.get("id") or ev.get("id") or "")
                    cwd = str(payload.get("cwd") or ev.get("cwd") or "")
                elif inner == "user_message" and not preview:
                    preview = _clean_user_text(payload.get("message") or "")
                if sid and cwd and preview:
                    break
    except OSError as e:
        log_exception("codexHeadReadFail", e, detail=str(path))
    return sid, cwd, preview


def list_sessions(
    cwd: str, limit: int = 20,
    sessions_root: pathlib.Path | None = None,
    *,
    all_projects: bool = False,
    state_db: pathlib.Path | None = None,
) -> list[dict]:
    """List recent Codex sessions whose working dir matches `cwd`.

    Returns newest-first `[{id, mtime, preview}]` — the same shape the
    Claude past-sessions endpoint returns, so the resume/fork picker is
    backend-agnostic on the client. `id` is the Codex conversation UUID
    (what `codex exec resume <id>` expects)."""
    root = sessions_root or (_codex_home() / "sessions")
    if not root.is_dir():
        return []
    want = str(pathlib.Path(os.path.expanduser(cwd))) if cwd else ""
    db_path = state_db
    if db_path is None and sessions_root is None:
        db_path = _codex_home() / "state_5.sqlite"
    indexed: dict[str, dict] = {}
    if db_path is not None and db_path.is_file():
        try:
            uri = f"file:{db_path}?mode=ro"
            with sqlite3.connect(uri, uri=True, timeout=1) as conn:
                conn.row_factory = sqlite3.Row
                where = "WHERE has_user_event = 1"
                params: list[Any] = []
                if not all_projects and want:
                    where += " AND cwd = ?"
                    params.append(want)
                rows = conn.execute(
                    f"""SELECT id,cwd,name,title,preview,first_user_message,
                               updated_at,updated_at_ms,rollout_path
                          FROM threads {where}
                         ORDER BY recency_at_ms DESC,updated_at_ms DESC
                         LIMIT ?""",
                    (*params, limit),
                ).fetchall()
            for row in rows:
                path = pathlib.Path(str(row["rollout_path"] or ""))
                if path and not path.is_file():
                    continue
                updated_ms = int(row["updated_at_ms"] or 0)
                item = {
                    "id": str(row["id"] or ""),
                    "mtime": updated_ms // 1000 if updated_ms else int(row["updated_at"] or 0),
                    "preview": str(row["preview"] or row["first_user_message"] or "")[:240],
                    "title": str(row["name"] or row["title"] or "")[:160],
                    "cwd": str(row["cwd"] or ""),
                }
                if item["id"]:
                    indexed[item["id"]] = item
        except (OSError, sqlite3.Error) as e:
            log_exception("codexSessionIndexReadFail", e, detail=str(db_path))
    try:
        files = sorted(root.rglob("rollout-*.jsonl"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError as e:
        log_exception("codexListSessionsFail", e, detail=cwd)
        return []
    out: dict[str, dict] = dict(indexed)
    # Merge a bounded rollout window so partial/migrating SQLite indexes never
    # hide resumable sessions that are still present on disk.
    for f in files[:max(200, limit * 4)]:
        sid, file_cwd, preview = _head_session_meta_and_preview(f)
        if not sid or sid in out:
            continue
        if not all_projects and want and str(pathlib.Path(file_cwd)) != want:
            continue
        try:
            mtime = int(f.stat().st_mtime)
        except OSError:
            continue
        out[sid] = {
            "id": sid, "mtime": mtime, "preview": preview[:240],
            "title": "", "cwd": file_cwd,
        }
    return sorted(out.values(), key=lambda item: item["mtime"], reverse=True)[:limit]


def find_latest_jsonl(
    session_id: str,
    sessions_root: pathlib.Path | None = None,
) -> pathlib.Path | None:
    """Find the rollout JSONL for a Codex session UUID.

    Codex names files rollout-<ISO-ts>-<uuid>.jsonl, so we match the uuid
    suffix. Returns the newest match (a session is normally one file, but
    guard against duplicates). Empty session_id → None (honest empty
    history for an agent that hasn't talked yet)."""
    if not session_id:
        return None
    root = sessions_root or (_codex_home() / "sessions")
    if not root.is_dir():
        return None
    best: pathlib.Path | None = None
    best_mtime = -1.0
    try:
        for p in root.rglob(f"*{session_id}.jsonl"):
            try:
                m = p.stat().st_mtime
            except OSError:
                continue
            if m > best_mtime:
                best, best_mtime = p, m
    except OSError as e:
        log_exception("codexFindRolloutFail", e, detail=session_id)
        return None
    return best
