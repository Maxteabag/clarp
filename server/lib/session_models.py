"""Read a session's recorded model without substituting today's global default.

The backend objects own the per-CLI readers (``recorded_model``,
``model_transcript``, ``cli_default_model``); this module keeps the shared,
cached parsing helpers they call and the facade the host reads through.
"""
from __future__ import annotations

import json
import sqlite3
import time
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=8192)
def _transcript(backend: str, session: str, epoch: int):
    """The transcript the backend reads the model from, cached a minute."""
    del epoch
    from .backends import by_id
    return by_id(backend).model_transcript(session)


@lru_cache(maxsize=8192)
def _read_model(path: str, stamp: int, size: int) -> str:
    del stamp
    with open(path, "rb") as stream:
        floor = max(0, size - 1024 * 1024)
        window = 64 * 1024
        while True:
            start = max(floor, size - window)
            stream.seek(start)
            # Keep read-to-EOF behavior if the file grew after the caller's
            # stat. Only the starting offset is bounded by that size.
            lines = stream.read().splitlines()
            if start > floor and lines:
                # A trial window can begin inside a record. At the final
                # legacy floor, retain its original cropped-line semantics.
                lines = lines[1:]
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
            if start == floor:
                return ""
            window = min(window * 4, 1024 * 1024)


def indexed_model(home, session: str) -> str:
    """The model a CLI's own sqlite thread index records for ``session``,
    answered from a five-second cache."""
    return _indexed_model(str(home), session, int(time.monotonic() // 5))


@lru_cache(maxsize=8192)
def _indexed_model(home: str, session: str, epoch: int) -> str:
    del epoch
    try:
        with sqlite3.connect(f"file:{Path(home) / 'state_5.sqlite'}?mode=ro", uri=True, timeout=0.1) as db:
            row = db.execute("SELECT model FROM threads WHERE id = ?", (session,)).fetchone()
        return str(row[0] or "") if row else ""
    except sqlite3.Error:
        return ""


def transcript_model(backend: str, session: str) -> str:
    """The model the newest turn of the session's transcript names, or ""."""
    try:
        path = _transcript(backend, session, int(time.monotonic() // 60))
        if path is None:
            return ""
        stat = path.stat()
        return _read_model(str(path), stat.st_mtime_ns, stat.st_size)
    except (OSError, ValueError):
        return ""


def recorded_model(backend: str, session: str) -> str:
    if not session:
        return ""
    from .backends import by_id
    return by_id(backend).recorded_model(session)


def launch_default(backend: str, cfg) -> str:
    from .backends import by_id
    runner = by_id(backend)
    model = runner.default_model_effort(cfg)[0]
    if model:
        return model
    try:
        return runner.cli_default_model()
    except (OSError, ValueError):
        pass
    return ""


def agent_model(agent: dict, session: str = "") -> str:
    return str(agent.get("model") or recorded_model(str(agent.get("backend") or "claude"), session))
