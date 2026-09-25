"""The backend singletons and the only place their ids are listed.

``by_id`` normalises the way ``lib.backends.normalize`` does (aliases,
case, unknown -> Claude), so the two never disagree: there is exactly one
normaliser and it lives in the facade.
"""
from __future__ import annotations

import threading

from ..protocol import AgentBackend
from .agy import AgyBackend
from .base import Backend
from .claude import ClaudeBackend
from .codex import CodexBackend
from .deepseek import DeepSeekBackend
from .grok import GrokBackend
from .opencode import OpenCodeBackend

_CLASSES: dict[str, type[Backend]] = {
    AgentBackend.CLAUDE: ClaudeBackend,
    AgentBackend.CODEX: CodexBackend,
    AgentBackend.AGY: AgyBackend,
    AgentBackend.GROK: GrokBackend,
    AgentBackend.OPENCODE: OpenCodeBackend,
    AgentBackend.DEEPSEEK: DeepSeekBackend,
}
_LOCK = threading.Lock()
_INSTANCES: dict[str, Backend] | None = None


def _instances() -> dict[str, Backend]:
    """Build the singletons on first use, after ``lib.backends`` has its
    adapter rows (the two modules import each other)."""
    global _INSTANCES
    if _INSTANCES is None:
        with _LOCK:
            if _INSTANCES is None:
                from .. import backends as facade
                _INSTANCES = {bid: cls(facade._BY_ID[bid])
                              for bid, cls in _CLASSES.items()}
    return _INSTANCES


def by_id(backend_id: str | None) -> Backend:
    """The backend that runs ``backend_id``, normalised like ``normalize``."""
    from .. import backends as facade
    return _instances()[facade.normalize(backend_id)]


def for_agent(agent_row: dict | None) -> Backend:
    """The backend of one agent row (``backend`` column, normalised)."""
    return by_id((agent_row or {}).get("backend"))


def all() -> tuple[Backend, ...]:  # noqa: A001 - the contract names it all()
    """Every backend, in registry (catalogue) order."""
    return tuple(_instances().values())
