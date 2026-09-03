"""Compile an agent's profile into a provider-shaped biasing payload.

The entry point the transcription path calls. Resolves which packs apply,
fills dynamic packs from generators, compiles against the active model's
budget, and records the run so any transcript can be traced back to the exact
prompt that produced it.

Failure policy: transcription must never fail because biasing failed. Every
public function degrades to an empty payload and logs.
"""

from __future__ import annotations

from dataclasses import dataclass

from .log import log_exception
from .vocab_budget import (
    Budget,
    CompileResult,
    Pack,
    Unit,
    WHISPER_PROMPT_TOKENS,
    compile_packs,
)
from .vocab_generators import (
    agents_pack,
    corrections_pack,
    recent_speech_pack,
    workspace_pack,
)

# What each provider+model will accept. Capacities are the documented limits;
# where a provider recommends less than it permits we take the recommendation,
# because over-stuffing keyterms measurably dilutes them.
_BUDGETS: dict[str, Budget] = {
    "faster-whisper": Budget(
        provider="faster-whisper", model="*", unit=Unit.TOKENS,
        capacity=WHISPER_PROMPT_TOKENS, supports_prose=True),
    "whisper.cpp": Budget(
        provider="whisper.cpp", model="*", unit=Unit.TOKENS,
        capacity=WHISPER_PROMPT_TOKENS, supports_prose=True),
    "deepgram": Budget(
        provider="deepgram", model="nova-3", unit=Unit.TERMS, capacity=50),
    "cartesia": Budget(
        provider="cartesia", model="ink-2", unit=Unit.TERMS, capacity=100),
    "elevenlabs": Budget(
        provider="elevenlabs", model="scribe-v2-realtime", unit=Unit.TERMS,
        capacity=50, max_term_chars=20),
    "assemblyai": Budget(
        provider="assemblyai", model="universal-3", unit=Unit.WORDS,
        capacity=1500, supports_prose=True),
}


def budget_for(provider: str, model: str = "") -> Budget:
    """The active budget, or a conservative Whisper-shaped default.

    An unknown provider gets the tightest sensible budget rather than an
    unbounded one: sending more than a provider accepts is silently truncated
    at the far end, which is the failure mode this whole module exists to
    avoid.
    """
    base = _BUDGETS.get(provider.strip().lower())
    if base is None:
        base = _BUDGETS["faster-whisper"]
    return Budget(
        provider=base.provider, model=(model or base.model),
        unit=base.unit, capacity=base.capacity,
        max_term_chars=base.max_term_chars,
        supports_prose=base.supports_prose)


def describe_budgets() -> list[dict]:
    """Provider capacities for the settings UI.

    The app shows these so a user can see why a profile yields more on one
    model than another, rather than guessing.
    """
    return [
        {"provider": b.provider, "model": b.model, "unit": b.unit,
         "capacity": b.capacity, "max_term_chars": b.max_term_chars,
         "supports_prose": b.supports_prose}
        for b in _BUDGETS.values()
    ]


@dataclass
class Sources:
    """Live inputs for the dynamic packs. All optional; all degrade to empty."""

    agent_names: tuple[str, ...] = ()
    project_name: str = ""
    identifiers: tuple[str, ...] = ()
    branches: tuple[str, ...] = ()
    commit_subjects: tuple[str, ...] = ()
    recent_transcripts: tuple[str, ...] = ()
    corrections: tuple[tuple[str, str], ...] = ()


# Dynamic packs are weighted above static ones: something said moments ago, or
# a term we have observed being misheard, beats a curated glossary entry.
_DYNAMIC_PRIORITY = {
    "corrections": 3.0,
    "recent-speech": 2.5,
    "agents": 2.0,
    "workspace": 1.2,
}

# Floors keep small critical packs alive at tight budgets. Workspace has none:
# it is large and its terms compete on merit.
_DYNAMIC_FLOOR = {
    "corrections": 2,
    "agents": 2,
}


def dynamic_packs(sources: Sources) -> list[Pack]:
    """Build the generated packs for this turn."""
    built: list[Pack] = []

    def add(name: str, terms: list) -> None:
        if terms:
            built.append(Pack(
                name=name, terms=tuple(terms),
                priority=_DYNAMIC_PRIORITY.get(name, 1.0),
                floor=_DYNAMIC_FLOOR.get(name, 0)))

    try:
        add("corrections", corrections_pack(sources.corrections))
        add("recent-speech", recent_speech_pack(sources.recent_transcripts))
        add("agents", agents_pack(sources.agent_names))
        add("workspace", workspace_pack(
            identifiers=sources.identifiers,
            branches=sources.branches,
            commit_subjects=sources.commit_subjects,
            project_name=sources.project_name))
    except Exception as e:  # noqa: BLE001 - biasing must never break STT
        log_exception("vocabDynamicPackFail", e)
    return built


def compile_for(
    *,
    provider: str,
    model: str = "",
    sources: Sources | None = None,
    static_packs: list[Pack] | None = None,
) -> CompileResult:
    """Compile the payload for one transcription request."""
    budget = budget_for(provider, model)
    try:
        packs = list(static_packs or []) + dynamic_packs(sources or Sources())
        return compile_packs(packs, budget)
    except Exception as e:  # noqa: BLE001 - never fail a turn over biasing
        log_exception("vocabCompileFail", e)
        return CompileResult(
            payload="", capacity=budget.capacity, unit=budget.unit)


def compile_and_record(
    *,
    provider: str,
    model: str = "",
    sources: Sources | None = None,
    static_packs: list[Pack] | None = None,
    agent_id: str = "",
    session: str = "",
    trace_id: str = "",
    profile_id: str | None = None,
) -> CompileResult:
    """Compile and persist the audit row. Returns the result either way."""
    result = compile_for(provider=provider, model=model, sources=sources,
                         static_packs=static_packs)
    try:
        from . import vocab_store
        run_id = vocab_store.record_run(
            result, provider=provider, model=model, agent_id=agent_id,
            session=session, trace_id=trace_id, profile_id=profile_id)
        result.run_id = int(run_id or 0)
    except Exception as e:  # noqa: BLE001 - an audit row is not worth a turn
        log_exception("vocabRunRecordFail", e)
    return result
