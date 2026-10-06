"""Parse an antigravity (agy) conversation transcript into the PWA's turn
list, and locate/list conversations.

agy is a Gemini/Cloud-Code agent. It persists each conversation under
~/.gemini/antigravity-cli/brain/<conversation-id>/.system_generated/logs/
transcript.jsonl — a typed, incrementally-written event log. We map it onto
the same {role, text, tools, timestamp} shape used for Claude and Codex.

Event types we care about:
  USER_INPUT        → user turn (content wrapped in <USER_REQUEST>…</…>)
  PLANNER_RESPONSE  → assistant text (only some carry `content`; the rest
                      carry tool_calls and are planning steps)
  RUN_COMMAND / LIST_DIRECTORY / VIEW_FILE / GREP_SEARCH / … → tool steps

cwd → most-recent conversation id comes from
~/.gemini/antigravity-cli/cache/last_conversations.json.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
from typing import Any

from .log import log_exception
from .text_util import truncate

# Map agy's event types onto the tool names the UI already styles.
_TOOL_TYPES = {
    "RUN_COMMAND": "Bash",
    "LIST_DIRECTORY": "LS",
    "VIEW_FILE": "Read",
    "VIEW_CODE_ITEM": "Read",
    "GREP_SEARCH": "Grep",
    "FIND_FILES": "Glob",
    "EDIT_FILE": "Edit",
    "WRITE_FILE": "Write",
    "WRITE_TO_FILE": "Write",
    "SEARCH_WEB": "WebSearch",
    "READ_URL_CONTENT": "WebFetch",
}

_USER_REQUEST_RE = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>",
                              re.DOTALL | re.IGNORECASE)


def _agy_home(brain_root: pathlib.Path | None = None) -> pathlib.Path:
    if brain_root is not None:
        return brain_root
    base = os.environ.get("CLAUDE_PWA_AGY_HOME") or str(
        pathlib.Path.home() / ".gemini" / "antigravity-cli")
    return pathlib.Path(base) / "brain"


def _cache_file() -> pathlib.Path:
    base = os.environ.get("CLAUDE_PWA_AGY_HOME") or str(
        pathlib.Path.home() / ".gemini" / "antigravity-cli")
    return pathlib.Path(base) / "cache" / "last_conversations.json"



def _extract_user_text(content: Any) -> str:
    """Pull the real prompt out of agy's <USER_REQUEST> wrapper, then strip
    our voice preamble (so the history shows what the user actually said)."""
    if not isinstance(content, str):
        return ""
    m = _USER_REQUEST_RE.search(content)
    text = (m.group(1) if m else content).strip()
    try:
        from .voice_preamble import strip_voice_preamble
        text = strip_voice_preamble(text)
    except Exception:                                      # noqa: BLE001
        pass
    return text


def _tool_from(ev: dict) -> dict:
    etype = str(ev.get("type") or "tool")
    name = _TOOL_TYPES.get(etype, etype.replace("_", " ").title())
    content = ev.get("content") or ""
    # agy tool content is prefixed with "Created At:…/Completed At:…"; the
    # useful part (Output / result) follows. Keep a short tail for the UI.
    out: dict[str, Any] = {
        "name": name,
        "summary": etype.replace("_", " ").lower(),
        "action": name.lower(),
        "file_path": "",
        "status": "error" if ev.get("error") else "ok",
    }
    if isinstance(content, str) and content:
        out["result"] = truncate(content, 300)
    return out


# agy 1.2+/1.3: tool calls are `tool_calls` [{name, args}] on PLANNER_RESPONSE
# steps, each answered in order by one GENERIC step holding its result.
_CALL_NAMES = {
    "run_command": "Bash", "view_file": "Read", "list_dir": "LS",
    "grep_search": "Grep", "find_by_name": "Glob", "write_to_file": "Write",
    "replace_file_content": "Edit", "multi_replace_file_content": "Edit",
    "search_web": "WebSearch", "read_url_content": "WebFetch",
}
_CALL_ARGS = {
    "CommandLine": "command", "Cwd": "cwd", "AbsolutePath": "file_path",
    "TargetFile": "file_path", "SearchPath": "path", "DirectoryPath": "path",
    "Query": "pattern", "Url": "url",
}
_RESULT_HEADER_RE = re.compile(r"\A(?:(?:Created|Completed) At:[^\n]*\n)*\n?")
_EXIT_RE = re.compile(r"exited with code (-?\d+)")


def _plain(value: Any) -> Any:
    """agy writes some string arguments JSON-encoded ('"/home/..."')."""
    if isinstance(value, str) and len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _call_tool(call: dict, call_id: str) -> dict:
    from .claude_transcript import summarise_tool
    raw = str(call.get("name") or "tool")
    args_in = call.get("args") if isinstance(call.get("args"), dict) else {}
    args = {_CALL_ARGS.get(k, k): _plain(v) for k, v in args_in.items()
            if k not in ("toolAction", "toolSummary")}
    name = _CALL_NAMES.get(raw, raw.replace("_", " ").title())
    tool = summarise_tool(name, args, call_id)
    own = _plain(args_in.get("toolSummary"))
    if isinstance(own, str) and own.strip():
        tool["summary"] = own.strip()   # agy's own words for what the call does
    tool["status"] = "running"
    tool["input"] = {k: truncate(v, 400) if isinstance(v, str) else v for k, v in args.items()}
    tool["_raw"] = raw
    return tool


def _settle(tool: dict, content: str) -> None:
    body = _RESULT_HEADER_RE.sub("", content or "", count=1)
    code = _EXIT_RE.search(body[:200])
    tool["exit_code"] = int(code.group(1)) if code else None
    if tool["_raw"] == "run_command" and "Output:\n" in body:
        body = body.split("Output:\n", 1)[1]
    tool["result"] = truncate(body.strip(), 300)
    tool["_output"] = body
    tool["status"] = "error" if tool["exit_code"] not in (None, 0) else "ok"


def _cell(tool: dict) -> dict:
    from .codex_transcript import (_classify_exploration, _command_cell,
                                   _display_cell, _generic_tool_cell)
    call_id, status = str(tool.get("id") or ""), tool["status"]
    if tool["_raw"] == "run_command":
        command = str(tool["input"].get("command") or "")
        cell = _command_cell(command=command, call_id=call_id, output=tool.get("_output", ""),
                             exit_code=tool.get("exit_code"), running=status == "running")
        explore = _classify_exploration(command)
        if explore and status != "error":
            cell["_explore"] = explore
        return cell
    if tool["_raw"] in ("view_file", "list_dir", "grep_search", "find_by_name"):
        target = str(tool["input"].get("file_path") or tool["input"].get("path") or "")
        label = {"view_file": "Read", "list_dir": "List"}.get(tool["_raw"], "Search")
        cell = _display_cell(kind="exploration", title="Explored", status=status, cell_id=call_id)
        if status != "error":
            cell["_explore"] = {"label": label, "text": target}
        return cell
    return _generic_tool_cell(tool["name"], {"call_id": call_id}, status)


def _finish_tools(turns: list[dict]) -> None:
    """Display cells for each assistant turn's calls, then drop the scratch keys."""
    from .opencode_transcript import _coalesce_exploration
    for turn in turns:
        calls = [t for t in turn.get("tools", []) if "_raw" in t]
        if calls:
            turn["display_cells"] = _coalesce_exploration(
                list(turn.get("display_cells", [])) + [_cell(t) for t in calls])
        for tool in turn.get("tools", []):
            for key in ("_raw", "_output"):
                tool.pop(key, None)


def parse_turns(path: pathlib.Path) -> list[dict]:
    """Read an agy transcript.jsonl into user/assistant turns.

    Raises OSError if unreadable."""
    rows: list[dict] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as e:
                log_exception("agyTranscriptLineSkip", e, detail=str(path))
    rows.sort(key=lambda r: r.get("step_index", 0)
              if isinstance(r.get("step_index"), int) else 0)

    turns: list[dict] = []
    pending_tools: list[dict] = []
    awaiting: list[dict] = []   # calls in order, each answered by the next GENERIC

    def _flush_tools_onto_assistant(ts: str) -> None:
        if not pending_tools:
            return
        if turns and turns[-1].get("role") == "assistant":
            turns[-1].setdefault("tools", []).extend(pending_tools)
        else:
            # Older step-typed tools always made their own turn. agy 1.2+ calls
            # never do: the old parser emitted no turn for them, and a new one
            # would shift the assistant ordinals stored for earlier turns.
            older = [t for t in pending_tools if "_raw" not in t]
            if older:
                turns.append({"role": "assistant", "text": "", "tools": older,
                              "timestamp": ts})
        pending_tools.clear()

    for ev in rows:
        etype = str(ev.get("type") or "")
        ts = str(ev.get("created_at") or "")
        if etype == "USER_INPUT":
            _flush_tools_onto_assistant(ts)
            text = _extract_user_text(ev.get("content") or "")
            if text:
                turns.append({"role": "user", "text": text, "tools": [],
                              "timestamp": ts})
        elif etype == "PLANNER_RESPONSE":
            content = (ev.get("content") or "").strip()
            # 1.2+ calls carry their args; 1.1 listed bare names and recorded
            # each tool as its own typed step instead (handled below).
            calls = [_call_tool(c, f"agy-{ev.get('step_index', 0)}-{i}")
                     for i, c in enumerate(ev.get("tool_calls") or [])
                     if isinstance(c, dict) and isinstance(c.get("args"), dict)]
            awaiting.extend(calls)
            # One assistant turn per step with text, exactly as before, so the
            # stored authority ordinals of earlier turns keep pointing right;
            # calls from text-less steps ride on the next text step.
            pending_tools.extend(calls)
            if content:
                turns.append({"role": "assistant", "text": content,
                              "tools": list(pending_tools), "timestamp": ts})
                pending_tools.clear()
        elif etype == "GENERIC" and awaiting:
            _settle(awaiting.pop(0), str(ev.get("content") or ""))
        elif etype in _TOOL_TYPES or (
                etype not in ("CONVERSATION_HISTORY", "SYSTEM_MESSAGE",
                              "GENERIC", "ERROR_MESSAGE") and ev.get("content")
                and "Created At:" in str(ev.get("content"))):
            pending_tools.append(_tool_from(ev))
    _flush_tools_onto_assistant(str(rows[-1].get("created_at") or "") if rows else "")
    _finish_tools(turns)
    return turns


def find_latest_jsonl(
    conversation_id: str,
    brain_root: pathlib.Path | None = None,
) -> pathlib.Path | None:
    """Locate the transcript.jsonl for an agy conversation id."""
    if not conversation_id:
        return None
    root = _agy_home(brain_root)
    p = (root / conversation_id / ".system_generated" / "logs"
         / "transcript.jsonl")
    return p if p.is_file() else None


def list_sessions(
    cwd: str,
    limit: int = 20,
    cache_file: pathlib.Path | None = None,
    brain_root: pathlib.Path | None = None,
) -> list[dict]:
    """List resumable agy conversations for a cwd.

    agy's last_conversations.json maps each cwd to its most-recent
    conversation id, so we surface that one (matching `--continue`
    semantics). Returns [{id, mtime, preview}] or []."""
    cf = cache_file or _cache_file()
    want = str(pathlib.Path(os.path.expanduser(cwd))) if cwd else ""
    try:
        mapping = json.loads(cf.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(mapping, dict):
        return []
    choices = [(want, mapping.get(want))] if want else list(mapping.items())
    out = []
    for session_cwd, conv_id in choices:
        if not conv_id:
            continue
        jsonl = find_latest_jsonl(str(conv_id), brain_root)
        if jsonl is None:
            continue
        try:
            mtime = int(jsonl.stat().st_mtime)
        except OSError:
            mtime = 0
        preview = ""
        try:
            turns = parse_turns(jsonl)
            for turn in turns:
                if turn["role"] == "user" and turn["text"]:
                    preview = turn["text"][:240]
                    break
        except OSError:
            pass
        out.append({
            "id": str(conv_id), "mtime": mtime, "preview": preview,
            "title": "", "cwd": str(session_cwd),
        })
    out.sort(key=lambda item: item["mtime"], reverse=True)
    return out[:limit]
