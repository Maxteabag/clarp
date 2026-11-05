"""Module-level helpers moved out of server.py by tools/refactor/s3_apply.py.
server.py imports them back. Generated: change tools/refactor/s3_moves.toml, not this file."""

from __future__ import annotations

import mimetypes
import os
import pathlib
from urllib.parse import unquote, urlparse
from lib import db
from lib.paths import RuntimePaths


def _runtime_paths() -> RuntimePaths:
    return RuntimePaths.from_home(pathlib.Path.home())


def _truthy_header(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _raw_query_value(path: str, key: str) -> str:
    """Decode one query value without form-style `+` to space rewriting.

    Filesystem paths legitimately contain plus signs. Native URLQueryItem may
    leave them literal, whereas parse_qs assumes HTML form encoding.
    """
    for field in urlparse(path).query.split("&"):
        raw_key, separator, raw_value = field.partition("=")
        if separator and unquote(raw_key) == key:
            return unquote(raw_value)
    return ""


# /clips/ack statuses that are moments on the voice timeline.
_PLAY_EVENTS = {"play-start": "play_start", "play-ok": "play_end",
                "play-fail": "play_fail"}


def _clip_session_and_trace(clip_id: int | None) -> tuple[str | None, str | None]:
    if clip_id is None:
        return None, None
    try:
        row = db.conn().execute(
            """SELECT a.session AS session, c.trace_id AS trace_id
                 FROM clips c LEFT JOIN agents a ON a.agent_id = c.agent_id
                WHERE c.clip_id = ?""", (clip_id,)).fetchone()
    except Exception:  # noqa: BLE001
        return None, None
    if row is None:
        return None, None
    return (row["session"] or None), (row["trace_id"] or None)


def _safe_upload_name(raw: str, content_type: str = "") -> str:
    """Reduce a client-supplied filename to a safe basename, preserving the
    extension so the agent's Read tool recognises images. Blocks path
    traversal (no separators / leading dots survive); falls back to a
    type-derived name when nothing usable is sent."""
    base = os.path.basename((raw or "").replace("\\", "/")).strip()
    cleaned = "".join(c for c in base if c.isalnum() or c in "._- ()")
    cleaned = cleaned.strip().replace(" ", "_").lstrip(".")
    primary_type = content_type.split(";", 1)[0].strip()
    if not cleaned:
        ext = mimetypes.guess_extension(primary_type) or ".bin"
        cleaned = "upload" + ext
    elif "." not in cleaned:
        cleaned += mimetypes.guess_extension(primary_type) or ""
    return cleaned[:128]
