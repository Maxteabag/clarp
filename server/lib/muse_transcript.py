"""Parse Muse Code session logs into the shared turn list.

A session is ``<home>/sessions/YYYY/MM/DD/<session-id>/session.jsonl``: one
envelope per line. The conversation is in the ``runtime.session`` rows whose
``payload.kind`` is ``run``; ``payload.event`` carries the step
(docs/architecture/muse-backend.md).
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import pathlib
import re
from typing import Any, Iterator

from .claude_transcript import summarise_tool, truncate
from .voice_preamble import strip_voice_preamble


def muse_home() -> pathlib.Path:
    override = os.environ.get("CLAUDE_PWA_MUSE_HOME")
    if override:
        return pathlib.Path(override)
    return pathlib.Path.home() / ".local" / "share" / "muse"


def sessions_root(home: pathlib.Path | None = None) -> pathlib.Path:
    return (home or muse_home()) / "sessions"


# Muse tool names → the Claude-shaped names the clients render as rich cards
# (claude_transcript.summarise_tool), with the argument names it expects.
_TOOL_NAMES = {
    "read_file": "Read",
    "write_file": "Write",
    "edit_file": "Edit",
    "apply_patch": "Edit",
    "shell": "Bash",
    "exec_command": "Bash",
    "grep": "Grep",
    "glob": "Glob",
    "list_dir": "Glob",
    "web_search": "WebSearch",
    "web_fetch": "WebFetch",
}
_ARG_NAMES = {
    "path": "file_path",
    "cmd": "command",
}
# A UUID-shaped session id; anything else never reaches the filesystem.
_SESSION_ID = re.compile(r"^[0-9a-fA-F-]{8,64}$")
_STOPPED = {"cancelled", "interrupted"}


def _iso(recorded_at: Any) -> str:
    """Muse stamps rows in microseconds since the epoch."""
    try:
        micros = int(recorded_at)
    except (TypeError, ValueError):
        return ""
    return _dt.datetime.fromtimestamp(micros / 1_000_000, tz=_dt.timezone.utc) \
        .isoformat().replace("+00:00", "Z")


def _rows(path) -> Iterator[dict]:
    try:
        lines = pathlib.Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            yield row


def _run_event(row: dict) -> dict | None:
    if row.get("payload_type") != "runtime.session":
        return None
    payload = row.get("payload")
    if not isinstance(payload, dict) or payload.get("kind") != "run":
        return None
    event = payload.get("event")
    return event if isinstance(event, dict) else None


def _arguments(call: dict) -> dict:
    raw = call.get("args")
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
    tool = summarise_tool(name, _arguments(call), str(call.get("call_id") or call.get("id") or ""))
    tool["status"] = "pending"
    return tool


def _apply_results(tools: list[dict], event: dict) -> None:
    by_id = {t.get("id"): t for t in tools}
    for result in event.get("results") or []:
        if not isinstance(result, dict):
            continue
        tool = by_id.get(str(result.get("tool_call_id") or ""))
        if tool is None:
            continue
        failed = bool(result.get("is_error") or result.get("error"))
        tool["status"] = "error" if failed else "ok"
        text = str(result.get("text") or "")
        if text.strip():
            tool["result"] = truncate(text, 300)


def _failure_text(terminal: str, reason: Any) -> str:
    detail = str(reason or "").strip()
    return f"Muse Code {terminal}: {detail}" if detail else f"Muse Code {terminal}"


def parse_turns(path) -> list[dict]:
    """Read a Muse ``session.jsonl`` into user/assistant turns.

    A run is ``started`` (the prompt), then per model call either
    ``assistant_tool_calls_committed`` + ``tool_result_batch_committed`` or
    ``assistant_message_committed`` (text), then ``terminal``. Tool-only
    steps attach to the run's next text answer; the run's last answer is the
    final answer and earlier ones are commentary. Encrypted reasoning is never
    shown. A run that ended in failure without an answer becomes one failed
    row; a stopped run leaves none.
    """
    turns: list[dict] = []
    pending: list[dict] = []
    answers: list[dict] = []
    for row in _rows(path):
        event = _run_event(row)
        if event is None:
            continue
        kind = event.get("kind")
        stamp = _iso(row.get("recorded_at"))
        if kind == "started":
            pending, answers = [], []
            text = strip_voice_preamble(str(event.get("prompt") or "")).strip()
            if text:
                turns.append({"role": "user", "text": text, "tools": [], "timestamp": stamp})
        elif kind == "assistant_tool_calls_committed":
            for call in event.get("tool_calls") or []:
                if isinstance(call, dict):
                    pending.append(_tool_from_call(call))
        elif kind == "tool_result_batch_committed":
            _apply_results(pending, event)
        elif kind == "assistant_message_committed":
            text = str(event.get("text") or "")
            if not text and not pending:
                continue
            for earlier in answers:
                earlier["kind"] = "commentary"
            turn = {"role": "assistant", "text": text, "tools": pending,
                    "timestamp": stamp, "kind": "final_answer"}
            pending = []
            turns.append(turn)
            answers.append(turn)
        elif kind == "terminal":
            terminal = str(event.get("terminal") or "")
            if pending:
                if answers:
                    answers[-1]["tools"].extend(pending)
                else:
                    turns.append({"role": "assistant", "text": "", "tools": pending,
                                  "timestamp": stamp})
                pending = []
            if terminal and terminal != "completed" and terminal not in _STOPPED and not answers:
                # No answer: a retry sends the same prompt again (message_writes).
                turns.append({"role": "assistant", "text": _failure_text(terminal, event.get("reason")),
                              "tools": [], "timestamp": stamp, "failed": True})
            answers = []
    if pending:
        turns.append({"role": "assistant", "text": "", "tools": pending,
                      "timestamp": turns[-1]["timestamp"] if turns else ""})
    return turns


def find_latest_jsonl(session_id: str, *, home: pathlib.Path | None = None):
    """``sessions/YYYY/MM/DD/<id>/session.jsonl``, newest day first."""
    if not session_id or not _SESSION_ID.match(session_id):
        return None
    root = sessions_root(home)
    if not root.is_dir():
        return None
    try:
        hits = sorted(root.glob(f"[0-9][0-9][0-9][0-9]/[0-9][0-9]/[0-9][0-9]/{session_id}/session.jsonl"),
                      reverse=True)
    except OSError:
        return None
    return next((p for p in hits if p.is_file()), None)


def _session_facts(path: pathlib.Path) -> tuple[str, str]:
    """(cwd, first prompt) of a session log."""
    cwd = ""
    preview = ""
    for row in _rows(path):
        kind = row.get("payload_type")
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        record = payload.get("record") if isinstance(payload.get("record"), dict) else {}
        if kind == "runtime.session.route_facts" and record.get("cwd"):
            cwd = str(record["cwd"])
        elif kind == "runtime.session.metadata" and record.get("workspace_root") and not cwd:
            cwd = str(record["workspace_root"])
        event = _run_event(row)
        if event and event.get("kind") == "started" and not preview:
            preview = strip_voice_preamble(str(event.get("prompt") or "")).strip()[:240]
        if cwd and preview:
            break
    return cwd, preview


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
    want = str(pathlib.Path(os.path.expanduser(cwd))) if cwd else ""
    try:
        logs = list(root.glob("[0-9][0-9][0-9][0-9]/[0-9][0-9]/[0-9][0-9]/*/session.jsonl"))
    except OSError:
        return []
    out: list[dict] = []
    for log in logs:
        session_cwd, preview = _session_facts(log)
        if not all_projects and want and session_cwd != want:
            continue
        try:
            mtime = int(log.stat().st_mtime)
        except OSError:
            mtime = 0
        out.append({"id": log.parent.name, "mtime": mtime, "preview": preview,
                    "title": "", "cwd": session_cwd})
    out.sort(key=lambda item: item["mtime"], reverse=True)
    return out[:limit]
