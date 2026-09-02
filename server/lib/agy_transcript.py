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


def truncate(s: Any, n: int = 600) -> str:
    if not isinstance(s, str):
        return ""
    return s if len(s) <= n else s[:n] + "…"


def _extract_user_text(content: Any) -> str:
    """Pull the real prompt out of agy's <USER_REQUEST> wrapper, then strip
    our voice preamble (so the history shows what the user actually said)."""
    if not isinstance(content, str):
        return ""
    m = _USER_REQUEST_RE.search(content)
    text = (m.group(1) if m else content).strip()
    try:
        from .codex_runner import strip_voice_preamble
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

    def _flush_tools_onto_assistant(ts: str) -> None:
        if not pending_tools:
            return
        if turns and turns[-1].get("role") == "assistant":
            turns[-1].setdefault("tools", []).extend(pending_tools)
        else:
            turns.append({"role": "assistant", "text": "", "tools": list(pending_tools),
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
            if content:
                turns.append({"role": "assistant", "text": content,
                              "tools": list(pending_tools), "timestamp": ts})
                pending_tools.clear()
        elif etype in _TOOL_TYPES or (
                etype not in ("CONVERSATION_HISTORY", "SYSTEM_MESSAGE",
                              "GENERIC", "ERROR_MESSAGE") and ev.get("content")
                and "Created At:" in str(ev.get("content"))):
            pending_tools.append(_tool_from(ev))
    _flush_tools_onto_assistant(str(rows[-1].get("created_at") or "") if rows else "")
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
