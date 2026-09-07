"""Parse a Claude Code project JSONL transcript into a turn list for the UI.

Pure module — no HTTP, no I/O dependencies beyond reading the JSONL file at
the path you hand it. The HTTP handler does the path resolution (it knows
which app session maps to which Claude session-id); we just take a file path
and return a list of {role, text, tools, timestamp}.
"""
from __future__ import annotations

import json
import pathlib
import re
from functools import lru_cache
from typing import Any

from .activity import summarize_tool_activity
from .log import log_exception


# Harness-injected user-role envelopes. claude-code records these as `user`
# turns, but the user never typed them — background-task notifications, system
# reminders, and slash-command echoes. Without filtering, the PWA renders them
# as if the user said "<task-notification> …". Matched at the start of a turn's
# (stripped) text.
_INJECTED_USER_NOISE_RE = re.compile(
    r"^<(?:task-notification|system-reminder|local-command-stdout|"
    r"command-name|command-message|command-args|clarp-account-recovery)\b",
    re.IGNORECASE,
)


def truncate(s: Any, n: int = 600) -> str:
    if not isinstance(s, str):
        return ""
    return s if len(s) <= n else s[:n] + "…"


def _as_int(v: Any) -> int | None:
    """Coerce a tool-input value to a plain int, or None. The model can emit a
    bogus value for a numeric field (e.g. Read offset="[160, 175]"); the client
    decodes these as Int?, so a non-int would fail the WHOLE transcript decode
    and blank the conversation. Be defensive: only pass through a real int."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str):
        s = v.strip()
        try:
            return int(s)
        except ValueError:
            return None
    return None


def summarise_tool(name: str, inp: dict | None, tool_id: str = "") -> dict:
    """Reduce a tool_use's input to the small set of fields the UI shows."""
    inp = inp or {}
    readable = summarize_tool_activity(name, inp)
    out: dict[str, Any] = {
        "name": name,
        "summary": readable["summary"],
        "action": readable["action"],
        "file_path": readable["file_path"],
        "status": "recorded",
    }
    if tool_id:
        out["id"] = tool_id
    if name == "Edit":
        out.update({
            "file_path": inp.get("file_path", ""),
            "old": truncate(inp.get("old_string", "")),
            "new": truncate(inp.get("new_string", "")),
            "replace_all": bool(inp.get("replace_all", False)),
        })
    elif name == "MultiEdit":
        edits = inp.get("edits") or []
        out.update({
            "file_path": inp.get("file_path", ""),
            "edits": [
                {
                    "old": truncate(e.get("old_string", ""), 200),
                    "new": truncate(e.get("new_string", ""), 200),
                } for e in edits[:10]
            ],
            "edit_count": len(edits),
        })
    elif name == "Write":
        out.update({
            "file_path": inp.get("file_path", ""),
            "content": truncate(inp.get("content", "")),
        })
    elif name == "Read":
        out.update({
            "file_path": inp.get("file_path", ""),
            "offset": _as_int(inp.get("offset")),
            "limit": _as_int(inp.get("limit")),
        })
    elif name == "Bash":
        out.update({
            "command": truncate(inp.get("command", ""), 400),
            "description": inp.get("description", ""),
        })
    elif name in ("Glob", "Grep"):
        out.update({k: inp.get(k) for k in ("pattern", "path", "glob", "output_mode")})
    elif name == "TodoWrite":
        todos = inp.get("todos") or []
        out["todos"] = [
            {"content": t.get("content", ""), "status": t.get("status", "")}
            for t in todos
        ]
    else:
        # Generic fallback: include short scalar inputs only.
        out["input"] = {
            k: truncate(str(v), 200)
            for k, v in inp.items()
            if not isinstance(v, (list, dict))
        }
    return out


def _tool_result_text(block: dict) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return truncate(content, 300)
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
            elif isinstance(item, str):
                parts.append(item)
        return truncate("\n".join(p for p in parts if p), 300)
    return ""


def _apply_tool_result(turns: list[dict], block: dict) -> None:
    tool_use_id = block.get("tool_use_id") or block.get("toolUseId") or ""
    is_error = bool(block.get("is_error") or block.get("isError"))
    for turn in reversed(turns):
        for tool in reversed(turn.get("tools") or []):
            if tool_use_id and tool.get("id") != tool_use_id:
                continue
            if tool.get("status") in ("ok", "error"):
                continue
            tool["status"] = "error" if is_error else "ok"
            result = _tool_result_text(block)
            if result:
                tool["result"] = result
            return


def parse_turns(path: pathlib.Path) -> list[dict]:
    """Read a JSONL transcript and return user/assistant turns.

    Raises OSError if the file can't be read — caller decides what to return
    to the client.
    """
    turns: list[dict] = []
    # claude-code auto-injects "Continue from where you left off." as a
    # user-role record (isMeta=true) whenever a session is resumed while
    # the previous turn was interrupted — and emits a short assistant
    # reply ("No response requested.") right after. Both clutter the
    # history pane. Track the meta uuids so we can also drop the
    # assistant reply whose parentUuid points at one.
    meta_uuids: set[str] = set()
    with path.open(encoding="utf-8") as f:
        for line in f:
            try:
                d = json.loads(line)
            except json.JSONDecodeError as e:
                log_exception("transcriptJsonLineSkip", e, detail=str(path))
                continue
            t = d.get("type")
            if t not in ("user", "assistant"):
                continue
            if d.get("isMeta") is True:
                u = d.get("uuid")
                if u: meta_uuids.add(u)
                continue
            if t == "assistant" and d.get("parentUuid") in meta_uuids:
                continue
            # claude-code writes a synthetic assistant record to the
            # transcript when its HTTP fetch to api.anthropic.com is
            # interrupted mid-stream — e.g. SIGTERM from our
            # preempt-kill, or systemd restarting the service. The
            # record carries an "API Error: The socket connection was
            # closed unexpectedly" text and is flagged with
            # isApiErrorMessage=true (and model="<synthetic>"). It's
            # not something Rachel/the agent actually said — drop it
            # so the history pane stays clean across restarts.
            if t == "assistant" and (
                d.get("isApiErrorMessage") is True
                or (d.get("message") or {}).get("model") == "<synthetic>"
            ):
                continue
            msg = d.get("message") or {}
            content = msg.get("content", "")
            text_parts: list[str] = []
            tools: list[dict] = []
            if isinstance(content, str):
                text_parts.append(content)
            elif isinstance(content, list):
                for c in content:
                    ctype = c.get("type") if isinstance(c, dict) else None
                    if ctype == "text":
                        text_parts.append(c.get("text", ""))
                    elif ctype == "tool_use":
                        tools.append(summarise_tool(
                            c.get("name", "tool"),
                            c.get("input"),
                            c.get("id", ""),
                        ))
                    elif ctype == "tool_result":
                        _apply_tool_result(turns, c)
            text = "\n".join(p for p in text_parts if p).strip()
            if not text and not tools:
                continue
            # Harness-injected user envelopes (task notifications, system
            # reminders, slash-command echoes) aren't anything the user said —
            # drop them so they don't render as a user message in the PWA.
            if t == "user" and not tools and _INJECTED_USER_NOISE_RE.match(text):
                continue
            turns.append({
                "role": t,
                "text": text,
                "tools": tools,
                "timestamp": d.get("timestamp", ""),
            })
    return turns


def context_tokens_from_jsonl(path: pathlib.Path, tail_bytes: int = 1_000_000) -> int | None:
    """Context-window occupancy after the latest turn — i.e. how full the
    conversation is right now.

    The right number is the LAST assistant message's own usage
    (input_tokens + cache_read + cache_creation), which is the size of the
    context the model carried on its final step. We deliberately do NOT use the
    `result` event's aggregated usage: that sums every tool-step's cache read,
    so a long turn inflates to millions of tokens and tells you nothing about
    window occupancy. After a /compact this naturally drops to the small
    post-summary size.

    Reads only the tail of the file (transcripts reach tens of MB), so it's
    cheap to call on every snapshot. Returns None if no usage is found.
    """
    try:
        stat = path.stat()
    except OSError:
        return None
    return _context_tokens_cached(str(path.resolve()), stat.st_dev, stat.st_ino,
                                  stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns,
                                  tail_bytes)


@lru_cache(maxsize=256)
def _context_tokens_cached(path_string: str, device: int, inode: int,
                           size: int, mtime_ns: int, ctime_ns: int,
                           tail_bytes: int) -> int | None:
    # File identity protects replacement/truncation; timestamps protect edits
    # of the same length. Only a small derived integer is retained per version.
    path = pathlib.Path(path_string)
    try:
        with path.open("rb") as f:
            if size > tail_bytes:
                f.seek(size - tail_bytes)
                f.readline()  # discard the partial first line
            raw = f.read()
    except OSError:
        return None
    latest: int | None = None
    for line in raw.splitlines():
        if b'"usage"' not in line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("type") != "assistant":
            continue
        usage = (d.get("message") or {}).get("usage") or {}
        if not usage:
            continue
        latest = int(
            (usage.get("input_tokens") or 0)
            + (usage.get("cache_read_input_tokens") or 0)
            + (usage.get("cache_creation_input_tokens") or 0)
        )
    return latest


def find_latest_jsonl(
    backend_session_id: str,
    projects_root: pathlib.Path | None = None,
) -> pathlib.Path | None:
    """Find the JSONL for a specific bound claude session.

    Look up by exact session UUID under ~/.claude/projects/*/<id>.jsonl.
    bind_backend_session is called on the first turn (pre-mint in /send, or the
    UserPromptSubmit hook) so by the time a client
    asks for history the runtime row should already have a UUID.

    If backend_session_id is empty, return None. Empty UUID → empty
    history pane, which is the honest answer for "this agent hasn't
    talked yet".
    """
    projects_root = projects_root or (pathlib.Path.home() / ".claude" / "projects")
    if not backend_session_id:
        return None
    for jsonl in projects_root.glob(f"*/{backend_session_id}.jsonl"):
        return jsonl
    return None
