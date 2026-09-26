"""Parse Grok Build session transcripts into the shared turn list."""
from __future__ import annotations

import json
import os
import pathlib
import re
import urllib.parse
from typing import Any

from .voice_preamble import strip_voice_preamble
from .claude_transcript import summarise_tool, truncate


def grok_home() -> pathlib.Path:
    override = os.environ.get("CLAUDE_PWA_GROK_HOME")
    if override:
        return pathlib.Path(override)
    return pathlib.Path.home() / ".grok"


def sessions_root(home: pathlib.Path | None = None) -> pathlib.Path:
    return (home or grok_home()) / "sessions"


def _encode_cwd(cwd: str) -> str:
    path = str(pathlib.Path(os.path.expanduser(cwd)))
    return urllib.parse.quote(path, safe="")


def _text_of(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(filter(None, (_text_of(item) for item in value)))
    if isinstance(value, dict):
        if isinstance(value.get("text"), str):
            return value["text"]
        if isinstance(value.get("content"), (str, list, dict)):
            return _text_of(value["content"])
    return ""


# Grok Build tool names → the Claude-shaped names the clients already render
# with rich cards (claude_transcript.summarise_tool). Argument names are mapped
# alongside so the summariser sees the fields it expects.
_TOOL_NAMES = {
    "run_terminal_command": "Bash",
    "read_file": "Read",
    "write": "Write",
    "search_replace": "Edit",
    "grep": "Grep",
    "list_dir": "Glob",
    "todo_write": "TodoWrite",
    "web_search": "WebSearch",
    "web_fetch": "WebFetch",
}
_ARG_NAMES = {
    "target_file": "file_path",
    "target_directory": "path",
}
_USER_QUERY_RE = re.compile(r"^\s*<user_query>\s*(.*?)\s*</user_query>\s*$", re.DOTALL)
# Grok seeds every session with a ``<user_info>`` bootstrap row and injects
# ``<system-reminder>`` rows (marked ``synthetic_reason``) between prompts.
# Neither is something the user typed.
_BOOTSTRAP_PREFIXES = ("<user_info>", "<system-reminder>")


def _arguments(call: dict) -> dict:
    raw = call.get("arguments")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return {"arguments": raw}
    if not isinstance(raw, dict):
        return {}
    return {_ARG_NAMES.get(str(k), str(k)): v for k, v in raw.items()}


def _tool_from_call(call: dict) -> dict:
    raw_name = str(call.get("name") or "tool")
    name = _TOOL_NAMES.get(raw_name, raw_name)
    tool = summarise_tool(name, _arguments(call), str(call.get("id") or ""))
    tool["status"] = "pending"
    return tool


def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(part for part in parts if part)
    return ""


_EXIT_RE = re.compile(r"^exit: (-?\d+)\n?")


def _apply_tool_result(turns: list[dict], row: dict) -> None:
    call_id = str(row.get("tool_call_id") or row.get("tool_use_id") or "")
    text = _result_text(row.get("content"))
    is_error = bool(row.get("is_error") or row.get("error"))
    match = _EXIT_RE.match(text)
    if match:
        is_error = is_error or match.group(1) != "0"
        text = text[match.end():]
    for turn in reversed(turns):
        for tool in reversed(turn.get("tools") or []):
            if call_id and tool.get("id") != call_id:
                continue
            if tool.get("status") in ("ok", "error"):
                continue
            tool["status"] = "error" if is_error else "ok"
            if text.strip():
                tool["result"] = truncate(text, 300)
            return


def _user_text(row: dict) -> str:
    if row.get("synthetic_reason"):
        return ""
    text = _text_of(row.get("content")).strip()
    if not text or text.startswith(_BOOTSTRAP_PREFIXES):
        return ""
    match = _USER_QUERY_RE.match(text)
    if match:
        text = match.group(1)
    return strip_voice_preamble(text).strip()


def parse_turns(path) -> list[dict]:
    """Read a Grok Build ``chat_history.jsonl`` into user/assistant turns.

    One ``assistant`` row per model call (text plus ``tool_calls``);
    ``tool_result`` rows carry the output back by ``tool_call_id``. Reasoning
    rows and the system/bootstrap rows are not part of the conversation.
    """
    path = pathlib.Path(path)
    turns: list[dict] = []
    pending_tools: list[dict] = []
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
        kind = str(row.get("type") or row.get("role") or "")
        if kind == "user":
            if pending_tools and turns and turns[-1]["role"] == "assistant":
                turns[-1]["tools"].extend(pending_tools)
                pending_tools = []
            text = _user_text(row)
            if text:
                turns.append({"role": "user", "text": text, "tools": [],
                              "timestamp": str(row.get("ts") or "")})
            continue
        if kind == "assistant":
            text = _text_of(row.get("content"))
            tools = list(pending_tools)
            pending_tools = []
            for call in row.get("tool_calls") or []:
                if isinstance(call, dict):
                    tools.append(_tool_from_call(call))
            turns.append({"role": "assistant", "text": text, "tools": tools,
                          "timestamp": str(row.get("ts") or "")})
            continue
        if kind == "tool_result":
            _apply_tool_result(turns, row)
            continue
        if kind in {"backend_tool_call", "tool_use"}:
            pending_tools.append({
                "name": str((row.get("kind") or {}).get("tool_type")
                            if isinstance(row.get("kind"), dict)
                            else row.get("name") or "tool"),
                "status": "ok",
                "input": {},
            })
    if pending_tools and turns and turns[-1]["role"] == "assistant":
        turns[-1]["tools"].extend(pending_tools)
    return turns


def find_latest_jsonl(session_id: str, *, home: pathlib.Path | None = None):
    if not session_id:
        return None
    root = sessions_root(home)
    if not root.is_dir():
        return None
    direct = root / session_id / "chat_history.jsonl"
    if direct.is_file():
        return direct
    try:
        for cwd_dir in root.iterdir():
            if not cwd_dir.is_dir():
                continue
            candidate = cwd_dir / session_id / "chat_history.jsonl"
            if candidate.is_file():
                return candidate
    except OSError:
        return None
    return None


def list_sessions(
    cwd: str,
    limit: int = 20,
    *,
    all_projects: bool = False,
    home: pathlib.Path | None = None,
) -> list[dict]:
    root = sessions_root(home)
    if not root.is_dir():
        return []
    dirs: list[pathlib.Path] = []
    if all_projects or not cwd:
        try:
            dirs = [p for p in root.iterdir() if p.is_dir()]
        except OSError:
            return []
    else:
        encoded = root / _encode_cwd(cwd)
        if encoded.is_dir():
            dirs = [encoded]
    out: list[dict] = []
    for cwd_dir in dirs:
        try:
            session_dirs = [p for p in cwd_dir.iterdir() if p.is_dir()]
        except OSError:
            continue
        decoded_cwd = urllib.parse.unquote(cwd_dir.name)
        for session_dir in session_dirs:
            jsonl = session_dir / "chat_history.jsonl"
            if not jsonl.is_file():
                continue
            try:
                mtime = int(jsonl.stat().st_mtime)
            except OSError:
                mtime = 0
            preview = ""
            title = ""
            summary = session_dir / "summary.json"
            try:
                info = json.loads(summary.read_text(encoding="utf-8"))
                if isinstance(info, dict):
                    title = str(info.get("generated_title")
                                or info.get("session_summary") or "")
            except (OSError, json.JSONDecodeError):
                pass
            try:
                for turn in parse_turns(jsonl):
                    if turn["role"] == "user" and turn["text"]:
                        preview = turn["text"][:240]
                        break
            except OSError:
                pass
            out.append({
                "id": session_dir.name, "mtime": mtime, "preview": preview,
                "title": title, "cwd": decoded_cwd,
            })
    out.sort(key=lambda item: item["mtime"], reverse=True)
    return out[:limit]
