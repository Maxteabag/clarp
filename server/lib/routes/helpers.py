"""Module-level helpers moved out of server.py by tools/refactor/s3_apply.py.
server.py imports them back. Generated: change tools/refactor/s3_moves.toml, not this file."""

from __future__ import annotations

import pathlib
from lib.paths import RuntimePaths


def _runtime_paths() -> RuntimePaths:
    return RuntimePaths.from_home(pathlib.Path.home())


def _truthy_header(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}
