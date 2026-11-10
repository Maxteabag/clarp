"""The backend singletons and the only place their ids are listed.

``by_id`` normalises the way ``lib.backends.normalize`` does (aliases,
case, unknown -> Claude), so the two never disagree: there is exactly one
normaliser and it lives in the inert catalogue leaf.
"""
from __future__ import annotations

import threading

from ..backend_catalogue import AgentBackend, normalize, is_valid, ids
from .agy import AgyBackend
from .base import Backend
from .claude import ClaudeBackend
from .codex import CodexBackend
from .deepseek import DeepSeekBackend
from .grok import GrokBackend
from .muse import MuseBackend
from .opencode import OpenCodeBackend

_CLASSES: dict[str, type[Backend]] = {
    AgentBackend.CLAUDE: ClaudeBackend,
    AgentBackend.CODEX: CodexBackend,
    AgentBackend.AGY: AgyBackend,
    AgentBackend.GROK: GrokBackend,
    AgentBackend.OPENCODE: OpenCodeBackend,
    AgentBackend.DEEPSEEK: DeepSeekBackend,
    AgentBackend.MUSE: MuseBackend,
}
_LOCK = threading.Lock()
_INSTANCES: dict[str, Backend] | None = None


def _instances() -> dict[str, Backend]:
    """Build the singletons on first use."""
    global _INSTANCES
    if _INSTANCES is None:
        with _LOCK:
            if _INSTANCES is None:
                _INSTANCES = {bid: cls() for bid, cls in _CLASSES.items()}
    return _INSTANCES


def by_id(backend_id: str | None) -> Backend:
    """The backend that runs ``backend_id``, normalised like ``normalize``."""
    return _instances()[normalize(backend_id)]


def all() -> tuple[Backend, ...]:  # noqa: A001 - the contract names it all()
    """Every backend, in registry (catalogue) order."""
    return tuple(_instances().values())


def get(backend_id: str | None) -> Backend | None:
    """Known strategy without execution's fallback for unknown ids."""
    return by_id(backend_id) if is_valid(backend_id) else None


def catalogue_fields(backend_id: str | None) -> dict:
    """Join declared values with current strategy-derived capabilities."""
    row = get(backend_id)
    if row is None:
        neutral = type("UnregisteredBackend", (Backend,), {
            "id": str(backend_id or ""), "label": str(backend_id or ""),
            "required_binary": ""})()
        return neutral.catalogue_fields(len(ids()))
    return row.catalogue_fields(ids().index(normalize(backend_id)))
