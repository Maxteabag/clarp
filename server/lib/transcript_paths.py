"""Where a backend's transcript lookup may look: one session id, inside one store (R70).

A native session id names one file or directory in a tool's own store. Every
lookup takes an id only when it is a single path component by the rule below,
and a file only when, with symlinks resolved, it lies inside the store's root.
Anything else is "no transcript": one log line naming the backend, never the id
or a path, and never an exception.
"""
from __future__ import annotations

import pathlib
import re

from .log import log

SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def session_id_ok(session_id: object) -> bool:
    """Whether a native session id is one path component by the shared rule."""
    return isinstance(session_id, str) and SESSION_ID.fullmatch(session_id) is not None


def refused(backend: str) -> None:
    log("transcriptSessionIdRefused", f"backend={backend}")


def checked_id(backend: str, session_id: str) -> str | None:
    """The id when it passes the rule; otherwise None, logged once."""
    if session_id_ok(session_id):
        return session_id
    refused(backend)
    return None


def inside(backend: str, root: pathlib.Path, path: pathlib.Path | None) -> pathlib.Path | None:
    """``path`` when, with symlinks resolved, it lies inside ``root``; otherwise None, logged once."""
    if path is None:
        return None
    try:
        if path.resolve().is_relative_to(pathlib.Path(root).resolve()):
            return path
    except (OSError, RuntimeError, ValueError):
        pass
    refused(backend)
    return None
