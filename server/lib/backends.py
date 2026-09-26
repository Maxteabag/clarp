"""AI-CLI backend facade.

Each coding CLI is a ``Backend`` strategy object in ``lib.backend``
(``docs/architecture/backend-strategy.md``); ``by_id()`` / ``for_agent()``
hand them out. This module stays the import path callers use: the id
constants, ``normalize()`` (the one alias normaliser) and, while the
migration ran, the registry rows the strategies delegated
to for their catalogue metadata, presentation and capability flags.
Clients do not hardcode provider ids — they render
``/agent-model-options``, including the label, brand colours, symbol and
``supports_*`` flags carried here, so a new CLI looks intentional in the
apps without an app release.
"""
from __future__ import annotations

import importlib
import pathlib
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from .protocol import AgentBackend

CLAUDE = AgentBackend.CLAUDE
CODEX = AgentBackend.CODEX
AGY = AgentBackend.AGY
GROK = AgentBackend.GROK
OPENCODE = AgentBackend.OPENCODE
DEEPSEEK = AgentBackend.DEEPSEEK
DEFAULT = CLAUDE
_RUNTIME_CLIENT: Any | None = None

# One runtime ``status`` RPC per short window, shared by every caller in this
# process.  A snapshot asks active_handles() once per agent, so a 120-agent
# fleet opened 120 runtime connections per poll from dozens of HTTP threads
# and overran the runtime socket's listen backlog (2026-09-12).  The window is
# short enough that the spawn/finish race it adds is no wider than the one a
# point-in-time RPC already had, and dispatch invalidates it explicitly.
RUNTIME_STATUS_TTL = 0.25
_STATUS_LOCK = threading.Lock()
# (client the result came from, monotonic time, result, error)
_STATUS_CACHE: tuple[Any | None, float, dict | None, BaseException | None] = (
    None, 0.0, None, None)
_clock = time.monotonic


def configure_runtime_client(client: Any | None) -> None:
    """Route process ownership calls to the external runtime when configured."""
    global _RUNTIME_CLIENT
    _RUNTIME_CLIENT = client
    invalidate_runtime_status()


def invalidate_runtime_status() -> None:
    """Forget the shared status window, e.g. right after this process dispatched."""
    global _STATUS_CACHE
    with _STATUS_LOCK:
        _STATUS_CACHE = (None, 0.0, None, None)


def runtime_status(*, max_age: float | None = None) -> dict:
    """The runtime's ``status`` result, at most ``max_age`` seconds old.

    Concurrent callers share one in-flight RPC.  A failure is remembered for
    the same window and logged once, so a runtime outage costs one connection
    and one journal line per window instead of one per agent.
    """
    global _STATUS_CACHE
    if _RUNTIME_CLIENT is None:
        raise RuntimeError("no external runtime client configured")
    ttl = RUNTIME_STATUS_TTL if max_age is None else max_age
    with _STATUS_LOCK:
        client, fetched_at, result, error = _STATUS_CACHE
        if client is not _RUNTIME_CLIENT or _clock() - fetched_at >= ttl:
            client = _RUNTIME_CLIENT
            try:
                result, error = client.status(), None
            except Exception as exc:  # noqa: BLE001 - remembered for the window
                result, error = None, exc
                from .log import log_exception
                log_exception("runtimeStatusUnavailable", exc, detail="shared-status")
            _STATUS_CACHE = (client, _clock(), result, error)
        if error is not None:
            raise error
        return result if isinstance(result, dict) else {}


@dataclass(frozen=True)
class _RemoteHandle:
    trace_id: str

    def is_alive(self) -> bool:
        return True


@dataclass(frozen=True)
class BackendCapabilities:
    supports_fork: bool
    supports_transcript_streaming: bool
    required_binary: str


from .backend.base import (  # noqa: E402
    BackendBrand, DEFAULT_BRAND, DEFAULT_SYMBOL, LOGIN_KINDS, EFFORT_UIS, EFFORT_SCOPES)


def _mod(name: str):
    return importlib.import_module(f"lib.{name}")


def adapters() -> tuple["Backend", ...]:
    """Every backend in catalogue order (the name predates the classes)."""
    return all_backends()


def ids() -> tuple[str, ...]:
    return tuple(b.id for b in all_backends())


def catalogue_fields(backend: str | None) -> dict[str, Any]:
    """Presentation + capability flags for one ``/agent-model-options`` row.

    An unregistered id gets the neutral defaults so a catalogue row is
    always complete; ``sort_index`` follows registry order.
    """
    row = get(backend)
    if row is None:
        neutral = type("UnregisteredBackend", (Backend,), {
            "id": str(backend or ""), "label": str(backend or ""),
            "required_binary": ""})()
        return neutral.catalogue_fields(len(all_backends()))
    return row.catalogue_fields(
        all_backends().index(row), supports_compact=supports_compact(row.id))


def supports_compact(backend: str) -> bool:
    """Whether the backend has a compaction strategy the Host can drive."""
    try:
        by_id(backend).compaction("")
    except Unsupported:
        return False
    return True


def routing_adapters() -> tuple["Backend", ...]:
    """Backends that can answer one isolated orchestrator request."""
    return tuple(b for b in all_backends() if b.supports_routing)


def auth_adapters() -> tuple["Backend", ...]:
    """Backends whose CLI has a sign-in the Host can drive."""
    return tuple(b for b in all_backends() if b.supports_auth)


def get(backend: str | None):
    """The backend object for a known id or alias, else None."""
    return by_id(backend) if is_valid(backend) else None


def adapter_for(backend: str | None):
    """Alias of ``by_id``: the backend object, normalised like ``normalize``."""
    return by_id(backend)


def for_provider(provider: str) -> str:
    """The backend id that executes a routing provider.

    A registered backend id passes through; an API-key provider maps to the
    CLI whose adapter fronts it (``openai`` runs through Codex).
    """
    for b in all_backends():
        if provider in b.api_providers:
            return b.id
    return provider


def valid_efforts(backend: str) -> tuple[str, ...]:
    return by_id(backend).efforts


def clean_effort(backend: str, effort: str | None) -> str:
    return by_id(backend).clean_effort(effort)


def is_valid_model(backend: str, model: str | None) -> bool:
    return by_id(backend).is_valid_model(model)


def normalize(backend: str | None) -> str:
    """Coerce an arbitrary string to a registered backend, else Claude.

    Unknown / empty values fall back to Claude so a malformed agent row can
    never strand a user with a backend that has no runner. Registered ids
    (including ones a client has never seen) pass through.
    """
    b = (backend or "").strip().lower()
    b = _ALIASES.get(b, b)
    return b if b in _BY_ID else DEFAULT


def is_valid(backend: str | None) -> bool:
    b = (backend or "").strip().lower()
    return _ALIASES.get(b, b) in _BY_ID


def label(backend: str | None) -> str:
    return by_id(backend).label


def capabilities(backend: str | None) -> BackendCapabilities:
    runner = by_id(backend)
    return BackendCapabilities(
        supports_fork=runner.supports_fork,
        supports_transcript_streaming=runner.supports_transcript_streaming,
        required_binary=runner.executable(),
    )


def spawn_turn(backend: str, **kwargs: Any):
    return by_id(backend).spawn_turn(**kwargs)


def interrupt(backend: str, agent_id: str) -> int:
    if _RUNTIME_CLIENT is not None:
        return int(_RUNTIME_CLIENT.interrupt(normalize(backend), agent_id))
    from .turn_model_fallback import REGISTRY
    return by_id(backend).interrupt(agent_id) + REGISTRY.interrupt(agent_id, event="fallbackInterruptFail")


def interrupt_any(agent_id: str) -> int:
    if _RUNTIME_CLIENT is not None:
        return int(_RUNTIME_CLIENT.interrupt_any(agent_id))
    from .turn_model_fallback import REGISTRY
    total = REGISTRY.interrupt(agent_id, event="fallbackInterruptFail")
    seen: set[str] = set()
    for b in all_backends():
        if b.runner and b.runner not in seen:
            seen.add(b.runner)
            total += int(b.interrupt(agent_id) or 0)
        for name in b.extra_interrupt_modules:
            if name not in seen:
                seen.add(name)
                total += int(_mod(name).interrupt(agent_id) or 0)
    return total


def active_handles(backend: str, agent_id: str) -> list:
    if _RUNTIME_CLIENT is not None:
        try:
            status = runtime_status()
        except Exception:  # noqa: BLE001 - already logged once per window
            # During the runtime's short idle rollover, persisted busy state is
            # safer than claiming the process vanished and double-spawning.
            from . import agents as agents_db
            return ([_RemoteHandle("runtime-status-unknown")]
                    if agents_db.is_busy(agent_id) else [])
        active = status.get("active") or {}
        if agent_id in active:
            return [_RemoteHandle(str(active[agent_id]))]
        if agent_id in set(status.get("spawning") or ()):
            return [_RemoteHandle("spawning")]
        if agent_id in set(status.get("terminals") or ()):
            return [_RemoteHandle("terminal")]
        return []
    from .turn_model_fallback import REGISTRY
    return by_id(backend).active_handles(agent_id) + REGISTRY.active_handles(agent_id)


class GoalUnsupported(RuntimeError):
    """This backend has no goal control Clarp can drive yet."""


def goal(backend: str, agent_id: str, action: str, *, objective: str = "",
         stream=None) -> dict | None:
    """Start, pause, resume, clear or read the agent's goal.

    Returns the goal as ``agent_goals.public`` shapes it, or None when there is
    none. A backend without a goal protocol raises ``Unsupported``, surfaced
    here as GoalUnsupported so the caller can say so instead of pretending.
    """
    if _RUNTIME_CLIENT is not None:
        return _RUNTIME_CLIENT.goal(agent_id, action, objective=objective)
    try:
        return by_id(backend).goal(agent_id, action, objective=objective, stream=stream)
    except Unsupported:
        raise GoalUnsupported(
            f"{label(backend)} has no goal control Clarp can drive yet.") from None


def steer_turn(backend: str, agent_id: str, text: str, *,
               client_msg_id: str = "", synthesize_audio: bool = False) -> bool:
    if _RUNTIME_CLIENT is not None:
        return bool(_RUNTIME_CLIENT.steer(
            normalize(backend), agent_id, text,
            client_msg_id=client_msg_id,
            synthesize_audio=synthesize_audio,
        ))
    try:
        return by_id(backend).steer(
            agent_id, text, client_msg_id=client_msg_id,
            synthesize_audio=synthesize_audio)
    except Unsupported:
        return False


def find_session_jsonl(backend: str, session_id: str):
    return by_id(backend).find_transcript(session_id)


def parse_turns(backend: str, path) -> list[dict]:
    return by_id(backend).parse_transcript(path)


def list_sessions(backend: str, cwd: str, *, limit: int = 20,
                  all_projects: bool = False) -> list[dict]:
    return by_id(backend).list_sessions(cwd, limit=limit, all_projects=all_projects)


def default_model_effort(backend: str, cfg) -> tuple[str, str]:
    return by_id(backend).default_model_effort(cfg)


ResultCb = Callable[[dict], None]

# The strategy objects. Imported last: the registry builds its singletons
# from the adapter rows above on first use, so neither import order
# (facade first or package first) sees a half-initialised module.
from .backend.base import Backend, CompactionStrategy, Unsupported  # noqa: E402
from .backend.registry import by_id, for_agent  # noqa: E402
from .backend.registry import all as all_backends  # noqa: E402

# Lookup tables derived from the classes. Built here, after the package
# import, because the class modules may import this facade lazily.
_BY_ID: dict[str, Backend] = {b.id: b for b in all_backends()}
_ALIASES: dict[str, str] = {alias: b.id for b in all_backends() for alias in b.aliases}
VALID: set[str] = set(_BY_ID)
LABELS: dict[str, str] = {b.id: b.label for b in all_backends()}
EFFORTS: dict[str, tuple[str, ...]] = {b.id: b.efforts for b in all_backends()}
CAPABILITIES: dict[str, BackendCapabilities] = {
    b.id: BackendCapabilities(
        supports_fork=b.supports_fork,
        supports_transcript_streaming=b.supports_transcript_streaming,
        required_binary=b.required_binary,
    )
    for b in all_backends()
}
