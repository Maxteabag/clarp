"""The Host's tool-explanation setting (docs/live-items.md §6).

``enabled`` (default on, as before) decides whether the explainer runs at
all; ``detail_level`` (0-4, default 2 Balanced) is the audience the Host uses
for explanations it attaches to live tool items. Devices keep their own level
for ``POST /tool-explanations``; with the setting off that endpoint answers
every item ``disabled`` without asking the explainer.
"""
from __future__ import annotations

from typing import Any

from . import settings_store

_ENABLED = "tool_explanations.enabled"
_LEVEL = "tool_explanations.detail_level"
DEFAULT_LEVEL = 2


def get() -> dict[str, Any]:
    stored = settings_store.get_many([_ENABLED, _LEVEL])
    enabled = stored.get(_ENABLED, "true").strip().lower() in {"1", "true", "yes", "on"}
    try:
        level = min(4, max(0, int(stored.get(_LEVEL, DEFAULT_LEVEL))))
    except ValueError:
        level = DEFAULT_LEVEL
    return {"enabled": enabled, "detail_level": level}


def enabled() -> bool:
    return get()["enabled"]


def update(changes: dict[str, Any]) -> dict[str, Any]:
    """Apply a partial update; ValueError names the first bad field."""
    if not isinstance(changes, dict):
        raise ValueError("object required")
    if "enabled" in changes and not isinstance(changes["enabled"], bool):
        raise ValueError("enabled must be true or false")
    level = changes.get("detail_level")
    if "detail_level" in changes and (type(level) is not int or level not in range(5)):
        raise ValueError("detail_level must be an integer from 0 to 4")
    if "enabled" in changes:
        settings_store.set_bool(_ENABLED, changes["enabled"])
    if "detail_level" in changes:
        settings_store.set_int(_LEVEL, level)
    return get()
