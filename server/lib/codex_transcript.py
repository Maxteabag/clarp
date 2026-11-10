"""Parse a Codex `exec` rollout JSONL into transcript turns.

Codex persists each session to
~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl. Each line is an
envelope `{timestamp, type, payload}`. We map it onto the exact shape
lib/claude_transcript.parse_turns produces for Claude, while also emitting
native Codex display cells for compact terminal-style rendering:

    {"role": "assistant", "text": str, "tools": [...],
     "display_cells": [...], "timestamp": str}

Text comes from `event_msg/user_message` + `event_msg/agent_message`
(the conversation as the user/agent see it); tools come from
`response_item/function_call` + `custom_tool_call` with their `*_output`
counterparts matched back by call_id.
"""
from __future__ import annotations

import functools
import json
import os
import pathlib
import re
import shlex
import sqlite3
from collections import Counter
from typing import Any

from .activity import summarize_tool_activity
from . import native_rotation_context
from .log import log_exception
from .text_util import truncate
from .voice_markup import strip_hidden_blocks
from .codex_tool_cells import (  # noqa: F401 - compatibility exports
    TOOL_OUTPUT_LINE_LIMIT,
    TOOL_OUTPUT_CHAR_LIMIT,
    CELL_LINE_CHAR_HARD_LIMIT,
    CELL_LINE_COUNT_HARD_LIMIT,
    CELL_BYTES_HARD_LIMIT,
    _exec_command_str,
    _safe_id,
    _display_line,
    _display_cell,
    _shell_words,
    _split,
    _SPLIT_CACHE_MAX_CHARS,
    _cached_split,
    _QUOTED_WRAPPER,
    _QUOTE_ESCAPE,
    _quoted_wrapper_words,
    _SHELLS,
    _EXPLORERS,
    _PLAIN_FIRST_WORD,
    _first_word_rules_out,
    _strip_shell_wrapper,
    _looks_like_path,
    _classify_exploration,
    _preview_output,
    _truncate_output_line,
    _cap_cell,
    _clean_tool_output,
    _tool_output_exit_code,
    _command_cell,
    _patch_cell,
    _patch_file_paths,
    _patch_edits,
    _generic_tool_cell,
    _mcp_cell,
    _web_search_cell,
    _json_obj,
    _token,
    _get_any,
    _status_text,
    _status_cell_state,
    _agent_label,
    _agent_state_lines,
    _subagent_tool_cell,
    _subagent_output_cell,
    _collab_details,
    _collab_tool_cell,
    _collab_protocol_cell,
    _replace_or_append_cell,
    _codex_tool,
    _attach_output,
    _NESTED_TOOL_RE,
    _JSON_CMD_RE,
    _legacy_tool_category,
    _nested_exec_cell,
    _legacy_function_cell,
)


def _codex_home() -> pathlib.Path:
    return pathlib.Path(os.environ.get("CODEX_HOME",
                        str(pathlib.Path.home() / ".codex")))



def _clean_user_text(msg: Any) -> str:
    """Codex echoes the user prompt prefixed with a '› ' marker and can
    double it ('› i'm planning to I am planning to…'); strip the marker."""
    if not isinstance(msg, str):
        return ""
    return msg.lstrip("›").strip()


# The same short exploration commands (rg, sed, cat) come back on every
# reparse of a growing transcript; long scripts are left out to bound memory.
# `shlex.join(["/usr/bin/bash", "-lc", script])`, the shape of nearly every
# Codex command: single-quoted text has no escapes, and quote() writes each
# embedded ' as '"'"', so the script decodes without a character-wise lexer.
# A leading word with no quotes, escapes or exotic whitespace is the first
# word shlex.split would return, and the first word of its str.split fallback.
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
    # Codex wrote "# AGENTS.md instructions for <cwd>" until it moved the
    # path out of the heading; newer rollouts start with the bare heading.
    return text.startswith((
        "# AGENTS.md instructions for ",
        "# AGENTS.md instructions\n",
        "<environment_context>",
        "<recommended_plugins>",
    ))


def _append_visible_message(turns: list[dict], *, role: str, text: str,
                            timestamp: str, kind: Any = None) -> None:
    """Append one visible chat message, deduplicating dual event formats."""
    if not text or role not in {"user", "assistant"}:
        return
    if turns and turns[-1].get("role") == role and (
        turns[-1].get("text") == text
        # One copy (agent_message) can omit a hidden metadata block that the
        # other (response_item) keeps; compared raw they imported as two
        # identical bubbles. Keep the fuller copy so the block is not lost.
        or strip_hidden_blocks(turns[-1].get("text") or "").strip()
        == strip_hidden_blocks(text).strip()
    ):
        if len(text) > len(turns[-1].get("text") or ""):
            turns[-1]["text"] = text
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
    native_thread_id = ""
    metadata_conflict = False
    rotation_prefix = True
    context_prefix: list[dict] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError as e:
                metadata_conflict = True
                rotation_prefix = False
                log_exception("codexTranscriptLineSkip", e, detail=str(path))
                continue
            if not isinstance(ev, dict):
                metadata_conflict = True
                rotation_prefix = False
                continue
            ts = str(ev.get("timestamp") or "")
            payload = ev.get("payload")
            if not isinstance(payload, dict):
                payload = {}
            inner = payload.get("type") or ev.get("type") or ""
            if ev.get("type") == "session_meta":
                supplied = payload.get("id")
                if not isinstance(supplied, str) or not supplied:
                    metadata_conflict = True
                elif native_thread_id and native_thread_id != supplied:
                    metadata_conflict = True
                else:
                    native_thread_id = supplied
            if inner in ("task_started", "turn_started"):
                rotation_prefix = False
            item = payload.get("item") or ev.get("item")
            candidate = item if isinstance(item, dict) else payload
            if (rotation_prefix and native_thread_id and not metadata_conflict and
                    native_rotation_context.is_context_item(candidate, native_thread_id)):
                if not context_prefix:
                    # Delay the drop until EOF: a later conflicting header or
                    # corrupt record cannot retroactively certify this prefix.
                    _append_visible_message(
                        context_prefix, role="user",
                        text=native_rotation_context.item_text(candidate), timestamp=ts)
                    continue
                metadata_conflict = True  # ambiguous repeated seed, not a new authority
            if rotation_prefix:
                bootstrap = (inner == "message" and
                             (payload.get("role") in ("system", "developer") or
                              (payload.get("role") == "user" and
                               _is_injected_user_context(_response_message_text(payload)))))
                metadata = (ev.get("type") in ("session_meta", "turn_context") or
                            inner in ("thread_settings_applied", "token_count"))
                if not bootstrap and not metadata:
                    # Also close on real input/output with a missing start
                    # event, or on an unknown pre-seed record. The native seed
                    # producer owns a fresh thread before any real turn.
                    rotation_prefix = False

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
                from .voice_preamble import strip_voice_preamble
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
    if metadata_conflict and context_prefix:
        # The one withheld bootstrap user row predates all visible turns.
        # Preserve the ordinary dual-format dedup rule at the splice point.
        if turns and turns[0].get("role") == "user":
            prefix_text = context_prefix[-1]["text"]
            first_text = turns[0].get("text") or ""
            if (prefix_text == first_text or
                    strip_hidden_blocks(prefix_text).strip() == strip_hidden_blocks(first_text).strip()):
                first = turns.pop(0)
                _append_visible_message(context_prefix, role="user", text=first_text,
                                        timestamp=first.get("timestamp") or "", kind=first.get("kind"))
        turns = context_prefix + turns
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
                where = "WHERE (has_user_event = 1 OR COALESCE(first_user_message, '') != '')"
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
