"""Canonical narration level values and coercion."""
from __future__ import annotations

QUIET = 0
MILESTONES = 1
STEPS = 2
RUNNING = 3

MIN_LEVEL = QUIET
MAX_LEVEL = RUNNING
DEFAULT_LEVEL = QUIET


def clamp(value: object) -> int:
    """Coerce anything a client sends into a level this server understands."""
    try:
        level = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_LEVEL
    return max(MIN_LEVEL, min(MAX_LEVEL, level))
