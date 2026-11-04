"""Module-level helpers moved out of server.py by tools/refactor/s3_apply.py.
server.py imports them back. Generated: change tools/refactor/s3_moves.toml, not this file."""

from __future__ import annotations

import mimetypes
import os
import pathlib
from lib.paths import RuntimePaths


def _runtime_paths() -> RuntimePaths:
    return RuntimePaths.from_home(pathlib.Path.home())


def _truthy_header(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


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
