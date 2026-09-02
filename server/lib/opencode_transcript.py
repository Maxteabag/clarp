"""Parse OpenCode session history into the shared turn list."""
from __future__ import annotations

import json
import os
import pathlib
import sqlite3
from typing import Any


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


def _turns_from_messages(rows: list[tuple[str, str]]) -> list[dict]:
    turns: list[dict] = []
    for created, raw in rows:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        role = _role_of(data)
        if not role:
            continue
        text = _text_of(data.get("content") or data.get("body") or data)
        tools: list[dict] = []
        for part in data.get("parts") or []:
            if not isinstance(part, dict):
                continue
            kind = str(part.get("type") or part.get("tool") or "")
            if kind in {"tool", "tool_use", "toolcall"}:
                tools.append({
                    "name": str(part.get("name") or part.get("tool") or "tool"),
                    "status": "ok",
                    "input": part.get("input") or part.get("state") or {},
                })
            elif not text:
                text = _text_of(part)
        if not text and not tools:
            continue
        turns.append({
            "role": role, "text": text, "tools": tools,
            "timestamp": str(created),
        })
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
    except sqlite3.Error:
        return []
    try:
        rows = con.execute(
            "SELECT time_created, data FROM message "
            "WHERE session_id = ? ORDER BY time_created, id",
            (session_id,),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()
    return _turns_from_messages([(str(a), b) for a, b in rows])


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
    except sqlite3.Error:
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
    except sqlite3.Error:
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
    except sqlite3.Error:
        return []
    finally:
        con.close()
    out = []
    for session_id, directory, title, updated in rows:
        preview = ""
        turns = _parse_db_session(db, str(session_id))
        for turn in turns:
            if turn["role"] == "user" and turn["text"]:
                preview = turn["text"][:240]
                break
        mtime = int(updated or 0)
        if mtime > 10_000_000_000:
            mtime //= 1000
        out.append({
            "id": str(session_id), "mtime": mtime, "preview": preview,
            "title": str(title or ""), "cwd": str(directory or ""),
        })
    return out
