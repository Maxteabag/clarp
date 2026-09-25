"""Read a session's recorded model without substituting today's global default."""
from __future__ import annotations

import json
import os
import sqlite3
import time
from functools import lru_cache
from pathlib import Path
import tomllib


def _codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def codex_transcript_path(session: str) -> Path | None:
    """Codex's rollout file: the thread index first, else the newest match."""
    try:
        with sqlite3.connect(f"file:{_codex_home() / 'state_5.sqlite'}?mode=ro", uri=True, timeout=0.1) as db:
            row = db.execute("SELECT rollout_path FROM threads WHERE id = ?", (session,)).fetchone()
        if row and row[0] and Path(row[0]).is_file():
            return Path(row[0])
    except sqlite3.Error:
        pass
    from .codex_transcript import find_latest_jsonl
    return find_latest_jsonl(session)


def codex_indexed_model(session: str) -> str:
    return _indexed_model(str(_codex_home()), session, int(time.monotonic() // 5))


def codex_cli_default_model() -> str:
    data = tomllib.loads((_codex_home() / "config.toml").read_text())
    profile = data.get("profiles", {}).get(data.get("profile", ""), {})
    return str(profile.get("model") or data.get("model") or "")


def claude_cli_default_model() -> str:
    home = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    data = json.loads((home / "settings.json").read_text())
    return str(os.environ.get("ANTHROPIC_MODEL") or data.get("model") or "")


@lru_cache(maxsize=512)
def _transcript(backend: str, session: str, epoch: int):
    del epoch
    from .backends import adapter_for
    locate = adapter_for(backend).recorded_model_source.transcript
    return locate(session) if locate is not None else None


@lru_cache(maxsize=512)
def _read_model(path: str, stamp: int, size: int) -> str:
    del stamp
    with open(path, "rb") as stream:
        stream.seek(max(0, size - 1024 * 1024))
        lines = stream.read().splitlines()
    for line in reversed(lines):
        try:
            event = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            continue
        if not isinstance(event, dict):
            continue
        payload = event.get("payload") or {}
        message = event.get("message") or {}
        if not isinstance(payload, dict) or not isinstance(message, dict):
            continue
        model = (payload.get("model") if event.get("type") == "turn_context"
                 else message.get("model") if event.get("type") == "assistant" else "")
        if isinstance(model, str) and model and model != "<synthetic>":
            return model
    return ""


@lru_cache(maxsize=512)
def _indexed_model(home: str, session: str, epoch: int) -> str:
    del epoch
    try:
        with sqlite3.connect(f"file:{Path(home) / 'state_5.sqlite'}?mode=ro", uri=True, timeout=0.1) as db:
            row = db.execute("SELECT model FROM threads WHERE id = ?", (session,)).fetchone()
        return str(row[0] or "") if row else ""
    except sqlite3.Error:
        return ""


def recorded_model(backend: str, session: str) -> str:
    if not session:
        return ""
    from .backends import adapter_for
    indexed_model = adapter_for(backend).recorded_model_source.indexed_model
    if indexed_model is not None:
        indexed = indexed_model(session)
        if indexed:
            return indexed
    try:
        path = _transcript(backend, session, int(time.monotonic() // 60))
        if path is None:
            return ""
        stat = path.stat()
        return _read_model(str(path), stat.st_mtime_ns, stat.st_size)
    except (OSError, ValueError):
        return ""


def launch_default(backend: str, cfg) -> str:
    from .backends import adapter_for, default_model_effort
    model = default_model_effort(backend, cfg)[0]
    if model:
        return model
    cli_default = adapter_for(backend).recorded_model_source.cli_default_model
    if cli_default is None:
        return ""
    try:
        return cli_default()
    except (OSError, ValueError):
        pass
    return ""


def agent_model(agent: dict, session: str = "") -> str:
    return str(agent.get("model") or recorded_model(str(agent.get("backend") or "claude"), session))
