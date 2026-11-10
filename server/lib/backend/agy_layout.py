from __future__ import annotations

import os
import pathlib


def _agy_home(brain_root: pathlib.Path | None = None) -> pathlib.Path:
    if brain_root is not None:
        return brain_root
    base = os.environ.get("CLAUDE_PWA_AGY_HOME") or str(
        pathlib.Path.home() / ".gemini" / "antigravity-cli")
    return pathlib.Path(base) / "brain"
