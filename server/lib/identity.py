"""Typed agent and turn identity.

An agent is an ``AgentRef``; a turn is a ``TurnRef``. Strings arrive at the
boundary (a query string, a JSON body, a hook payload, a database row) and
are parsed into these once. Translating between ``session``, ``agent_id``,
``backend_session_id`` and ``trace_id`` happens here and nowhere else: every
other module asks this one instead of pairing ``get_by_session`` with
``get_by_agent_id`` on its own.

The agents table stays owned by ``agents.py``; this module only calls its
public getters.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from . import agents as _agents
from . import trace as _trace

# ``agents._new_agent_id`` mints ``secrets.token_hex(8)``.
_AGENT_ID_RE = re.compile(r"^[0-9a-f]{16}$")


@dataclass(frozen=True, slots=True)
class AgentRef:
    """One live agent: the durable id and the human-facing session name."""
    agent_id: str
    session: str

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "AgentRef":
        return cls(agent_id=str(row["agent_id"]), session=str(row["session"]))


@dataclass(frozen=True, slots=True)
class TurnRef:
    """One turn of one agent.

    ``turn_id`` and ``runtime_id`` are None until the corresponding rows
    exist; ``backend_session_id`` is '' for a backend that has not bound its
    conversation id yet.
    """
    agent: AgentRef
    trace_id: str
    turn_id: int | None
    runtime_id: int | None
    backend_session_id: str

    @property
    def agent_id(self) -> str:
        return self.agent.agent_id

    @property
    def session(self) -> str:
        return self.agent.session


def _clean(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def looks_like_agent_id(value: object) -> bool:
    return isinstance(value, str) and bool(_AGENT_ID_RE.match(value.strip()))


def lookup(value: object) -> dict[str, Any] | None:
    """The live agent row behind any identity shape, or None.

    Accepts an ``AgentRef``, an agent row (any mapping carrying ``agent_id``),
    an ``agent_id`` string or a ``session`` name. A row is trusted as-is when
    it already carries both ids; otherwise it is refreshed through its
    ``agent_id``. A string that is shaped like an agent id is tried as one
    first, then as a session name; any other string is tried as a session
    name first. Empty, None and unknown values resolve to None.
    """
    if value is None:
        return None
    if isinstance(value, AgentRef):
        return _agents.get_by_agent_id(value.agent_id)
    if isinstance(value, dict):
        agent_id = _clean(value.get("agent_id"))
        if not agent_id:
            return None
        if _clean(value.get("session")):
            return value
        return _agents.get_by_agent_id(agent_id)
    key = _clean(value)
    if not key:
        return None
    if looks_like_agent_id(key):
        return _agents.get_by_agent_id(key) or _agents.get_by_session(key)
    return _agents.get_by_session(key) or _agents.get_by_agent_id(key)


def resolve(value: object) -> AgentRef | None:
    """``AgentRef`` for any identity shape, or None when no live agent matches.

    An ``AgentRef`` passes through untouched; a row carrying both ids is
    trusted without a database read.
    """
    if isinstance(value, AgentRef):
        return value
    row = lookup(value)
    if not row:
        return None
    try:
        return AgentRef.from_row(row)
    except (KeyError, TypeError):
        return None


def resolve_backend_session(backend_session_id: object) -> AgentRef | None:
    """The agent whose live runtime is bound to this backend conversation id."""
    key = _clean(backend_session_id)
    row = _agents.get_by_backend_session(key) if key else None
    return AgentRef.from_row(row) if row else None


def resolve_hook(*, backend_session_id: object,
                 session: object) -> AgentRef | None:
    """The agent that fired a hook: backend session first, session name second."""
    row = _agents.resolve_for_hook(
        backend_session_id=_clean(backend_session_id) or None,
        session=_clean(session) or None)
    return AgentRef.from_row(row) if row else None


def backend_session(agent: AgentRef | dict[str, Any] | str) -> str:
    """The most recent live runtime's backend conversation id, or ''."""
    ref = resolve(agent)
    return _agents.live_backend_session(ref.agent_id) if ref else ""


def turn_ref(agent: AgentRef | dict[str, Any] | str, trace_id: object,
             *, turn_id: int | None = None) -> TurnRef | None:
    """Bind a trace id to an agent's current runtime.

    Returns None for an unknown agent or an unparseable trace id.
    """
    ref = resolve(agent)
    parsed = _trace.parse_trace_id(trace_id)
    if ref is None or parsed is None:
        return None
    return TurnRef(
        agent=ref,
        trace_id=parsed,
        turn_id=turn_id,
        runtime_id=_agents.current_runtime_id(ref.agent_id),
        backend_session_id=_agents.live_backend_session(ref.agent_id),
    )
