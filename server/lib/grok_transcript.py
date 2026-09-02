"""Parse Grok Build session transcripts into the shared turn list."""
from __future__ import annotations

import json
import os
import pathlib
import urllib.parse
from typing import Any


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


def parse_turns(path) -> list[dict]:
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
                turns[-1]["tools"] = pending_tools
                pending_tools = []
            text = _text_of(row.get("content"))
            if text:
                turns.append({"role": "user", "text": text, "tools": [],
                              "timestamp": str(row.get("ts") or "")})
            continue
        if kind == "assistant":
            text = _text_of(row.get("content"))
            tools = list(pending_tools)
            pending_tools = []
            for call in row.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                tools.append({
                    "name": str(call.get("name") or "tool"),
                    "status": "ok",
                    "input": call.get("arguments") or {},
                })
            turns.append({"role": "assistant", "text": text, "tools": tools,
                          "timestamp": str(row.get("ts") or "")})
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
        turns[-1]["tools"] = pending_tools
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
