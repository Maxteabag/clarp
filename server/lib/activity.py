"""Readable agent activity summaries for hooks, SSE, and history UI."""
from __future__ import annotations

from typing import Any

from .protocol import ActivityStatus, AgentState, SSEType


def truncate(value: Any, limit: int = 140) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "..."


def path_tail(path: Any) -> str:
    text = truncate(path, 220)
    if not text:
        return ""
    parts = [p for p in text.replace("\\", "/").rstrip("/").split("/") if p]
    return "/".join(parts[-2:]) if parts else text


def tool_input_from_hook_payload(payload: dict[str, Any]) -> dict[str, Any]:
    value = payload.get("tool_input")
    if isinstance(value, dict):
        return value
    tool = payload.get("tool")
    if isinstance(tool, dict) and isinstance(tool.get("input"), dict):
        return tool["input"]
    return {}


def tool_status_from_hook_payload(
    payload: dict[str, Any],
    default: str = ActivityStatus.OK,
) -> str:
    response = payload.get("tool_response")
    if isinstance(response, dict):
        if response.get("is_error") is True or response.get("error"):
            return ActivityStatus.ERROR
    if payload.get("error") or payload.get("tool_error"):
        return ActivityStatus.ERROR
    return default


def summarize_tool_activity(name: str, inp: dict[str, Any] | None = None) -> dict[str, str]:
    inp = inp or {}
    name = name or "tool"
    raw_path = inp.get("file_path") or inp.get("filePath") or inp.get("path") or ""
    file_path = str(raw_path or "")
    short_path = path_tail(file_path)

    if name in {"Bash", "bash"}:
        command = truncate(inp.get("command"), 170)
        description = truncate(inp.get("description"), 120)
        return {
            "action": "running command",
            "summary": description or command or "Running shell command",
            "file_path": "",
        }
    if name in {"Read", "read_file"}:
        return {
            "action": "reading file",
            "summary": short_path or "Reading file",
            "file_path": file_path,
        }
    if name in {"Write", "write_file"}:
        return {
            "action": "writing file",
            "summary": short_path or "Writing file",
            "file_path": file_path,
        }
    if name in {"Edit", "edit_file"}:
        return {
            "action": "editing file",
            "summary": short_path or "Editing file",
            "file_path": file_path,
        }
    if name == "MultiEdit":
        edits = inp.get("edits") if isinstance(inp.get("edits"), list) else []
        summary = f"{short_path} · {len(edits)} edits" if short_path else f"{len(edits)} edits"
        return {"action": "editing file", "summary": summary, "file_path": file_path}
    if name in {"Glob", "glob_search"}:
        pattern = truncate(inp.get("pattern") or inp.get("glob"), 100)
        scope = path_tail(inp.get("path")) or "."
        return {
            "action": "finding files",
            "summary": f"{pattern or 'files'} in {scope}",
            "file_path": file_path,
        }
    if name in {"Grep", "grep_search"}:
        pattern = truncate(inp.get("pattern"), 100)
        scope = path_tail(inp.get("path")) or "."
        return {
            "action": "searching code",
            "summary": f"{pattern or 'code'} in {scope}",
            "file_path": file_path,
        }
    if name == "TodoWrite":
        todos = inp.get("todos") if isinstance(inp.get("todos"), list) else []
        current = next(
            (
                item.get("content")
                for item in todos
                if isinstance(item, dict) and item.get("status") == "in_progress"
            ),
            "",
        )
        return {
            "action": "updating plan",
            "summary": truncate(current, 120) or f"{len(todos)} items",
            "file_path": "",
        }
    if name in {"WebSearch", "web_search"}:
        return {
            "action": "searching web",
            "summary": truncate(inp.get("query"), 130) or "the web",
            "file_path": "",
        }
    if name in {"WebFetch", "web_fetch"}:
        return {
            "action": "reading web page",
            "summary": truncate(inp.get("url"), 130) or "web page",
            "file_path": "",
        }

    scalar = next(
        (truncate(v, 100) for v in inp.values() if not isinstance(v, (dict, list))),
        "",
    )
    return {"action": name, "summary": scalar or f"Using {name}", "file_path": file_path}


def state_activity_event(
    *,
    agent_id: str,
    session: str,
    persona: str,
    kind: str,
    ts: int,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    detail = detail or {}
    phase = detail.get("phase") or kind
    status = detail.get("status") or (
        ActivityStatus.RUNNING if kind in AgentState.busy_states() else ActivityStatus.OK
    )
    tool = detail.get("tool") or ""
    action = detail.get("action") or ""
    summary = detail.get("summary") or ""
    file_path = detail.get("file_path") or ""

    if kind == AgentState.TOOL:
        tool_summary = summarize_tool_activity(tool, detail.get("input") or {})
        phase = phase or "tool"
        action = action or tool_summary["action"]
        summary = summary or tool_summary["summary"]
        file_path = file_path or tool_summary["file_path"]
    elif kind == AgentState.THINKING:
        phase = "thinking"
        action = action or "thinking"
        summary = summary or "Thinking"
        status = ActivityStatus.RUNNING
    elif kind == AgentState.COMPACTING:
        phase = "compacting"
        action = action or "compacting"
        summary = summary or "Compacting context"
        status = ActivityStatus.RUNNING
    elif kind == AgentState.WAITING:
        phase = "waiting"
        action = action or "needs attention"
        summary = summary or truncate(detail.get("message") or detail.get("title"), 140) or "Needs attention"
        status = ActivityStatus.ERROR
    elif kind == AgentState.DONE:
        phase = "done"
        action = action or "done"
        summary = summary or "Done"
        status = ActivityStatus.OK
    elif kind == AgentState.INTERRUPTED:
        phase = "interrupted"
        action = action or "interrupted"
        summary = summary or truncate(detail.get("message"), 140) or "Turn interrupted"
        status = ActivityStatus.ERROR
    elif kind == AgentState.IDLE:
        phase = "idle"
        action = action or "idle"
        summary = summary or "Idle"
        status = ActivityStatus.OK
    elif kind == AgentState.STOPPED:
        phase = "stopped"
        action = action or "stopped"
        summary = summary or "Stopped"
        status = ActivityStatus.OK
    elif kind == AgentState.SPAWNED:
        phase = "spawned"
        action = action or "started"
        summary = summary or "Started"
        status = ActivityStatus.OK

    return {
        "type": SSEType.AGENT_ACTIVITY,
        "agent_id": agent_id,
        "session": session,
        "persona": persona,
        "kind": kind,
        "phase": phase,
        "status": status,
        "tool": tool,
        "action": action,
        "summary": summary,
        "file_path": file_path,
        "ts": int(ts),
    }
