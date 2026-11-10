"""Which STT engine runs a clip outside a call (S7 step 6): a pure decision.

transcription_pipeline gathers the facts (the phone's X-Transcription-Model,
the focused agent's engines, the cloud engine chosen in settings, the
long-form model for this clip's length) and does the I/O and logging; this
module only decides. Calls plan their engines in call_transcription.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EnginePlan:
    model: str                       # "" = the server's default engine
    agent_engine: str | None         # the focused agent's own first engine
    agent_fallbacks: tuple[str, ...]  # tried in order after it fails
    escalated_from: str | None       # the model long-form escalation replaced, or None


def wants_cloud_engine(*, requested_model: str, agent_engines: list[str], in_call: bool) -> bool:
    """Whether the cloud engine chosen in settings could stand in for the
    server default (so whether it is worth reading)."""
    agent_engine = agent_engines[0] if agent_engines else None
    return not in_call and not (agent_engine or requested_model)


def plan_engines(*, requested_model: str, agent_engines: list[str], cloud_engine: str,
                 long_model: str, in_call: bool) -> EnginePlan:
    """An agent's own engine is more specific than the phone's device-wide
    model choice, so it wins over the header; a cloud engine chosen in settings
    stands in for the server default (an explicit header still wins); a long
    recording escalates to its stronger model, overriding a pinned header too,
    but never an agent's engine (chosen for its language) or a call's plan."""
    agent_engine = agent_engines[0] if agent_engines else None
    model = agent_engine or requested_model
    if not model and not in_call and cloud_engine:
        model = cloud_engine
    escalated_from = None
    if long_model and long_model != model and not agent_engine and not in_call:
        escalated_from = model
        model = long_model
    return EnginePlan(model, agent_engine, tuple(agent_engines[1:]), escalated_from)
