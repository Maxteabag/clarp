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
        # The id the live stream gives the same step, so the saved row replaces it.
        "id": f"step-{ev.get('step_index')}",
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
# steps. Call i of step N is answered by step N+1+i (a GENERIC; in 1.1 files a
# typed tool step), which is also the step the live stream names `step-<N+1+i>`.
_RESULT_HEADER_RE = re.compile(r"\A(?:(?:Created|Completed) At:[^\n]*\n)*\n?")
_EXIT_RE = re.compile(r"exited with code (-?\d+)")
_TASK_RE = re.compile(r"tasks/(task-\d+)\.log")


def _plain(value: Any) -> Any:
    """agy writes some string arguments JSON-encoded ('"/home/..."')."""
    if isinstance(value, str) and len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _call_tool(call: dict, result_step: int) -> dict:
    # The live path's own mapping and redaction, so a saved call shows (and
    # hides) exactly what its live item did.
    from .backend.agy import _canonical_tool_input, _canonical_tool_name
    from .claude_transcript import summarise_tool
    raw = str(call.get("name") or "tool")
    args_in = call.get("args") if isinstance(call.get("args"), dict) else {}
    args = {k: _plain(v) for k, v in args_in.items()}
    name = _canonical_tool_name(raw)
    if name == raw:
        name = raw.replace("_", " ").title()
    safe = _canonical_tool_input(name, args)
    tool = summarise_tool(name, safe, f"step-{result_step}")
    own = args.get("toolSummary")
    if isinstance(own, str) and own.strip():
        tool["summary"] = truncate(own.strip(), 200)   # agy's own words for the call
    tool["status"] = "running"
    tool["input"] = safe
    tool["_raw"] = raw
    return tool


def _settle(tool: dict, content: str, status: str = "") -> None:
    body = _RESULT_HEADER_RE.sub("", content or "", count=1)
    code = _EXIT_RE.search(body[:300])
    tool["exit_code"] = int(code.group(1)) if code else None
    if "Output:\n" in body:
        body = body.split("Output:\n", 1)[1]
    tool["result"] = truncate(body.strip(), 300)
    tool["_output"] = body
    if status.upper() == "RUNNING":
        tool["status"] = "running"
    else:
        tool["status"] = "error" if tool["exit_code"] not in (None, 0) else "ok"


_TASK_END_RE = re.compile(r"\b(finished|was canceled) with result:", re.I)


def _settle_background(content: str, background: dict[str, dict]) -> None:
    """agy reports a background task's end in a later SYSTEM_MESSAGE: it
    finished, was canceled, or every task stopped when agy restarted."""
    if "background tasks have been stopped" in content:
        for tool in background.values():
            tool.update(status="error", result="Stopped when Antigravity restarted")
        background.clear()
        return
    end = _TASK_END_RE.search(content)
    if not end:
        return
    for task_id, tool in list(background.items()):
        if not re.search(rf"/{task_id}\b", content):
            continue
        body = content[end.end():]
        body = re.split(r"\nLog: |</SYSTEM_MESSAGE>", body, maxsplit=1)[0]
        if end.group(1).lower() == "finished":
            _settle(tool, body)
        else:
            tool.update(status="error", result=truncate(body.strip() or "Canceled", 300))
        background.pop(task_id)


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


def _thinking_cell(text: Any, step: Any) -> dict | None:
    """The model's own thinking for a step, as the reasoning cell Codex shows."""
    from .codex_transcript import _display_cell, _display_line
    from .voice_markup import clean_for_display
    if not isinstance(text, str) or not text.strip():
        return None
    paragraphs = [p.strip() for p in clean_for_display(text).split("\n\n") if p.strip()]
    lines = [_display_line(truncate(p, 600), kind="status") for p in paragraphs[:8]]
    if not lines:
        return None
    return _display_cell(kind="reasoning", title="Reasoned", status="ok",
                         cell_id=f"agy-think-{step}", lines=lines)


def _finish_tools(turns: list[dict]) -> None:
    """Display cells for each assistant turn, in the order the steps produced
    them (thinking, then that step's calls), then drop the scratch keys."""
    from .opencode_transcript import _coalesce_exploration
    for turn in turns:
        order = turn.pop("_order", None)
        if order:
            cells = [item if kind == "cell" else _cell(item) for kind, item in order]
            turn["display_cells"] = _coalesce_exploration(
                list(turn.get("display_cells", [])) + cells)
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
    pending_order: list[tuple[str, dict]] = []   # thinking cells and calls, in step order
    by_index = {r.get("step_index"): r for r in rows if isinstance(r.get("step_index"), int)}
    background: dict[str, dict] = {}   # background task id -> its call, until it finishes
    last_user = max((i for i, r in by_index.items() if r.get("type") == "USER_INPUT"), default=-1)

    def _flush_tools_onto_assistant(ts: str) -> None:
        if not pending_tools and not pending_order:
            return
        if turns and turns[-1].get("role") == "assistant":
            turns[-1].setdefault("tools", []).extend(pending_tools)
            turns[-1].setdefault("_order", []).extend(pending_order)
        else:
            # Older step-typed tools always made their own turn. agy 1.2+ calls
            # never do: the old parser emitted no turn for them, and a new one
            # would shift the assistant ordinals stored for earlier turns.
            older = [t for t in pending_tools if "_raw" not in t]
            if older:
                turns.append({"role": "assistant", "text": "", "tools": older,
                              "timestamp": ts})
        pending_tools.clear()
        pending_order.clear()

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
            calls = []
            step = ev.get("step_index", 0) if isinstance(ev.get("step_index"), int) else 0
            for i, call in enumerate(ev.get("tool_calls") or []):
                answer = by_index.get(step + 1 + i) or {}
                # 1.1 recorded each call as its own typed step (handled below),
                # with or without args: never count such a call twice.
                if not isinstance(call, dict) or not isinstance(call.get("args"), dict) \
                        or str(answer.get("type") or "") in _TOOL_TYPES:
                    continue
                tool = _call_tool(call, step + 1 + i)
                if answer.get("type") == "GENERIC":
                    _settle(tool, str(answer.get("content") or ""), str(answer.get("status") or ""))
                    if tool["status"] == "running":   # handed to a background task
                        task = _TASK_RE.search(tool["_output"] or "")
                        if task:
                            background[task.group(1)] = tool
                elif step + 1 + i < last_user:
                    # The conversation moved on without an answer (a cancelled turn).
                    tool.update(status="error", result="No result was recorded")
                calls.append(tool)
            thinking = _thinking_cell(ev.get("thinking"), ev.get("step_index", 0))
            if thinking:
                pending_order.append(("cell", thinking))
            pending_order.extend(("tool", call) for call in calls)
            # One assistant turn per step with text, exactly as before, so the
            # stored authority ordinals of earlier turns keep pointing right;
            # thinking and calls from text-less steps ride on the next text step.
            pending_tools.extend(calls)
            if content:
                turns.append({"role": "assistant", "text": content,
                              "tools": list(pending_tools), "timestamp": ts,
                              "_order": list(pending_order)})
                pending_tools.clear()
                pending_order.clear()
        elif etype == "SYSTEM_MESSAGE" and background:
            _settle_background(str(ev.get("content") or ""), background)
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
