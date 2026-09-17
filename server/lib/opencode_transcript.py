"""Parse OpenCode session history into the shared turn list.

OpenCode keeps a conversation in two tables. ``message.data`` is an envelope
(role, model, time, error); what was actually said lives in ``part`` rows keyed
by ``message_id``, one per text block, tool call, reasoning block or step
marker. A parser that reads only ``message`` finds no text at all."""
from __future__ import annotations

import json
import os
import pathlib
import sqlite3
from datetime import UTC, datetime
from typing import Any

from .codex_runner import strip_voice_preamble
from .log import log_exception
from .transcript_log import summarise_tool

# OpenCode's lowercase tool names and camelCase arguments, mapped to the shared
# vocabulary the chat already knows how to render as file, edit and shell cells.
_TOOL_NAMES = {
    "bash": "Bash", "read": "Read", "edit": "Edit", "write": "Write",
    "glob": "Glob", "grep": "Grep", "webfetch": "WebFetch",
    "websearch": "WebSearch", "todowrite": "TodoWrite", "skill": "Skill",
}
_ARG_NAMES = {"filePath": "file_path", "oldString": "old_string",
              "newString": "new_string", "replaceAll": "replace_all"}


def opencode_home() -> pathlib.Path:
    override = os.environ.get("CLAUDE_PWA_OPENCODE_HOME")
    if override:
        return pathlib.Path(override)
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return pathlib.Path(xdg) / "opencode"
    return pathlib.Path.home() / ".local" / "share" / "opencode"


def db_path(home: pathlib.Path | None = None) -> pathlib.Path:
    return (home or opencode_home()) / "opencode.db"


def _text_of(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(filter(None, (_text_of(item) for item in value)))
    if isinstance(value, dict):
        for key in ("text", "content", "delta"):
            if isinstance(value.get(key), str):
                return value[key]
        if "content" in value:
            return _text_of(value["content"])
    return ""


def _role_of(data: dict) -> str:
    role = str(data.get("role") or data.get("type") or "").lower()
    if role in {"user", "human"}:
        return "user"
    if role in {"assistant", "model", "ai"}:
        return "assistant"
    return ""


def _iso(created: Any) -> str:
    """OpenCode stores epoch milliseconds. Conversation rows are merged and
    ordered by comparing timestamps as strings against ISO values from other
    sources, so a bare integer would sort before every one of them."""
    try:
        value = float(created)
    except (TypeError, ValueError):
        return str(created or "")
    if value > 10_000_000_000:
        value /= 1000
    try:
        stamp = datetime.fromtimestamp(value, UTC)
    except (OverflowError, OSError, ValueError):
        return str(created)
    return stamp.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _tool_of(part: dict) -> dict:
    state = part.get("state") if isinstance(part.get("state"), dict) else {}
    raw = part.get("input") or state.get("input") or {}
    args = ({_ARG_NAMES.get(str(k), str(k)): v for k, v in raw.items()}
            if isinstance(raw, dict) else {})
    raw_name = str(part.get("name") or part.get("tool") or "tool")
    tool = summarise_tool(_TOOL_NAMES.get(raw_name, raw_name), args,
                          str(part.get("callID") or part.get("id") or ""))
    tool["status"] = "error" if state.get("status") == "error" else "ok"
    tool["input"] = args
    return tool


def _error_text(data: dict) -> str:
    error = data.get("error")
    if not isinstance(error, dict):
        return ""
    detail = error.get("data") if isinstance(error.get("data"), dict) else {}
    message = str(detail.get("message") or error.get("message") or "").strip()
    name = str(error.get("name") or "error")
    return f"OpenCode {name}: {message}" if message else f"OpenCode {name}"


def _turns_from_messages(
    rows: list[tuple[str, str]],
    parts: dict[str, list[dict]] | None = None,
    ids: list[str] | None = None,
) -> list[dict]:
    turns: list[dict] = []
    pending: list[dict] = []
    for index, (created, raw) in enumerate(rows):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        role = _role_of(data)
        if not role:
            continue
        inline = data.get("content") or data.get("body")
        text = _text_of(inline) if inline else ""
        stored = (parts or {}).get(ids[index], []) if ids else []
        texts: list[str] = []
        tools: list[dict] = []
        for part in list(data.get("parts") or []) + stored:
            if not isinstance(part, dict):
                continue
            kind = str(part.get("type") or "")
            if kind in {"tool", "tool_use", "toolcall"}:
                tools.append(_tool_of(part))
            elif kind in {"", "text"} and not part.get("synthetic") \
                    and not part.get("ignored"):
                # Reasoning and step markers are not part of the reply.
                texts.append(_text_of(part))
        if not text:
            text = "\n\n".join(filter(None, texts))
        if role == "user":
            text = strip_voice_preamble(text)
        elif not text and not tools:
            # A turn that died (suspended account, provider outage) has an
            # error and no parts; without this the chat shows an unanswered
            # prompt and nothing about why.
            text = _error_text(data)
        if not text and not tools:
            continue
        if role == "assistant" and not text:
            # OpenCode writes one message per step, so a single reply is a run
            # of tool-only messages ending in the one that carries the text.
            # Hold the tools and show them with that answer, as one turn.
            pending.extend(tools)
            continue
        if role == "user" and pending and turns and turns[-1]["role"] == "assistant":
            turns[-1]["tools"].extend(pending)
            pending = []
        if role == "assistant":
            tools, pending = pending + tools, []
        turns.append({
            "role": role, "text": text, "tools": tools,
            "timestamp": _iso(created),
        })
    if pending:
        # The reply never reached its text (interrupted, or still running).
        if turns and turns[-1]["role"] == "assistant":
            turns[-1]["tools"].extend(pending)
        else:
            turns.append({"role": "assistant", "text": "", "tools": pending,
                          "timestamp": turns[-1]["timestamp"] if turns else ""})
    return turns


def parse_turns(path) -> list[dict]:
    """``path`` is either a jsonl file or ``opencode.db#session_id``."""
    raw = str(path)
    if "#" in raw and not pathlib.Path(raw).is_file():
        db, _, session_id = raw.partition("#")
        return _parse_db_session(pathlib.Path(db), session_id)
    path = pathlib.Path(path)
    if path.suffix == ".db":
        return []
    turns: list[dict] = []
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
        role = _role_of(row)
        if not role:
            continue
        turns.append({
            "role": role,
            "text": _text_of(row.get("content") or row.get("part") or row),
            "tools": [],
            "timestamp": str(row.get("time") or row.get("timestamp") or ""),
        })
    return turns


def _parse_db_session(db: pathlib.Path, session_id: str) -> list[dict]:
    if not db.is_file() or not session_id:
        return []
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error as e:
        log_exception("opencodeTranscriptOpenFail", e, detail=str(db))
        return []
    try:
        rows = con.execute(
            "SELECT id, time_created, data FROM message "
            "WHERE session_id = ? ORDER BY time_created, id",
            (session_id,),
        ).fetchall()
        parts = _parts_by_message(con, session_id)
    except sqlite3.Error as e:
        log_exception("opencodeTranscriptReadFail", e, detail=session_id)
        return []
    finally:
        con.close()
    return _turns_from_messages(
        [(str(created), data) for _id, created, data in rows],
        parts, [str(message_id) for message_id, _created, _data in rows])


def _parts_by_message(con: sqlite3.Connection, session_id: str) -> dict[str, list[dict]]:
    try:
        rows = con.execute(
            "SELECT message_id, data FROM part WHERE session_id = ? "
            "ORDER BY message_id, id",
            (session_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        # Databases written before OpenCode split parts out have no such table
        # and carry the text inside message.data instead.
        return {}
    grouped: dict[str, list[dict]] = {}
    for message_id, raw in rows:
        try:
            part = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(part, dict):
            grouped.setdefault(str(message_id), []).append(part)
    return grouped


def _first_user_text(con: sqlite3.Connection, session_id: str) -> str:
    """Preview for the session picker without parsing the whole conversation;
    a listing covers up to a hundred sessions, some with thousands of parts."""
    try:
        rows = con.execute(
            "SELECT p.data FROM part p JOIN message m ON m.id = p.message_id "
            "WHERE p.session_id = ? AND json_extract(m.data, '$.role') = 'user' "
            "AND json_extract(p.data, '$.type') = 'text' "
            "ORDER BY m.time_created, m.id, p.id LIMIT 5",
            (session_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        return ""
    for (raw,) in rows:
        try:
            part = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(part, dict) and not part.get("synthetic") and not part.get("ignored"):
            text = strip_voice_preamble(_text_of(part)).strip()
            if text:
                return text
    return ""


def find_latest_jsonl(session_id: str, *, home: pathlib.Path | None = None):
    if not session_id:
        return None
    db = db_path(home)
    if not db.is_file():
        return None
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            row = con.execute(
                "SELECT id FROM session WHERE id = ?", (session_id,)
            ).fetchone()
        finally:
            con.close()
    except sqlite3.Error as e:
        log_exception("opencodeSessionLookupFail", e, detail=session_id)
        return None
    if not row:
        return None
    return pathlib.Path(f"{db}#{session_id}")


def list_sessions(
    cwd: str,
    limit: int = 20,
    *,
    all_projects: bool = False,
    home: pathlib.Path | None = None,
) -> list[dict]:
    db = db_path(home)
    if not db.is_file():
        return []
    want = str(pathlib.Path(os.path.expanduser(cwd))) if cwd else ""
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error as e:
        log_exception("opencodeSessionListOpenFail", e, detail=str(db))
        return []
    try:
        sql = ("SELECT id, directory, title, time_updated FROM session "
               "WHERE time_archived IS NULL ")
        params: list[Any] = []
        if want and not all_projects:
            sql += "AND directory = ? "
            params.append(want)
        sql += "ORDER BY time_updated DESC LIMIT ?"
        params.append(limit)
        rows = con.execute(sql, params).fetchall()
        previews = {str(row[0]): _first_user_text(con, str(row[0])) for row in rows}
    except sqlite3.Error as e:
        log_exception("opencodeSessionListFail", e, detail=str(db))
        return []
    finally:
        con.close()
    out = []
    for session_id, directory, title, updated in rows:
        preview = previews.get(str(session_id), "")
        if not preview:
            # Older databases keep the text inside message.data.
            for turn in _parse_db_session(db, str(session_id)):
                if turn["role"] == "user" and turn["text"]:
                    preview = turn["text"]
                    break
        preview = preview[:240]
        mtime = int(updated or 0)
        if mtime > 10_000_000_000:
            mtime //= 1000
        out.append({
            "id": str(session_id), "mtime": mtime, "preview": preview,
            "title": str(title or ""), "cwd": str(directory or ""),
        })
    return out
