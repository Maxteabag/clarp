"""Prompt-send use-case helpers.

The HTTP handler still owns response writing and backend callback wiring, but
routing a user prompt to the correct agent is domain logic. Keeping it here
makes it testable without a live handler instance.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import agents as agents_db
from .agent_store import load_agents
from .paths import RuntimePaths
from .protocol import TurnSource
from .routing import resolve_agent_by_spoken_name


@dataclass(frozen=True)
class SendTarget:
    session: str
    text: str
    routed_by_name: bool = False


def resolve_send_target(*, text: str, requested_session: str,
                        default_session: str, agents_path,
                        sticky_session: str = "") -> SendTarget:
    """Pick which agent a message goes to.

    If the message names an agent ("Antoni, …") it routes there and flags
    `routed_by_name` so the caller can make that agent the new sticky default.
    Otherwise it continues with `sticky_session` — the last-addressed agent —
    falling back to what the client asked for, then the global default. This is
    what lets a hands-free user keep talking to one agent without re-naming it
    every turn."""
    routed_session, stripped = resolve_agent_by_spoken_name(
        text, load_agents(agents_path)
    )
    if routed_session:
        session = routed_session
        text = stripped or text
        routed_by_name = True
    else:
        session = (sticky_session or requested_session
                   or default_session).strip() or default_session
        routed_by_name = False
    if not agents_db.get_by_session(session):
        session = default_session
        routed_by_name = False
    return SendTarget(session=session, text=text, routed_by_name=routed_by_name)


def source_marker_text(*, session: str, trace_id: str, now: float,
                       synthesize_audio: bool = True) -> str:
    return (
        f"{TurnSource.PWA_VOICE_MARKER} {session} {now:.3f} {trace_id} "
        f"{1 if synthesize_audio else 0}\n"
    )


def source_marker_path(home, session: str):
    return RuntimePaths.from_home(home).source_marker(session)
