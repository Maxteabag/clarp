"""Compile pipeline: profile + generators -> provider payload + audit row.

The contract that matters most is the failure policy. Biasing is an
optimisation; transcription is the product. Every path here must degrade to
an empty payload rather than raise into a user's turn.
"""
from __future__ import annotations

import sqlite3

import pytest

from lib import vocab_compile
from lib.vocab_budget import Pack, Term, Unit
from lib.vocab_compile import Sources, budget_for, compile_for, describe_budgets


def sources(**kw) -> Sources:
    return Sources(**kw)


# --- budgets ---------------------------------------------------------------

def test_each_known_provider_has_its_documented_budget():
    assert budget_for("faster-whisper").capacity == 223
    assert budget_for("faster-whisper").unit == Unit.TOKENS
    assert budget_for("elevenlabs").max_term_chars == 49
    assert budget_for("elevenlabs").capacity == 100
    # Two Cartesia models, two capabilities: Ink-2 biases over the socket,
    # Ink-Whisper's batch endpoint takes nothing.
    assert budget_for("cartesia", "ink-2").capacity == 100
    assert budget_for("cartesia", "ink-whisper").capacity == 0
    assert budget_for("assemblyai").unit == Unit.WORDS


def test_unknown_provider_falls_back_to_the_tightest_sensible_budget():
    """Guessing high would be silently truncated at the provider's end."""
    unknown = budget_for("some-new-vendor")
    assert unknown.unit == Unit.TOKENS
    assert unknown.capacity == 223


def test_budget_keeps_the_caller_supplied_model_name():
    assert budget_for("faster-whisper", "large-v3-turbo").model == "large-v3-turbo"


def test_describe_budgets_exposes_every_provider_for_the_ui():
    providers = {b["provider"] for b in describe_budgets()}
    assert {"faster-whisper", "deepgram", "cartesia",
            "elevenlabs", "assemblyai"} <= providers


# --- dynamic packs ---------------------------------------------------------

def test_project_name_reaches_the_payload():
    result = compile_for(
        provider="faster-whisper",
        sources=sources(project_name="Clarp", agent_names=["Rachel"]))
    assert "Clarp" in result.payload


def test_corrections_outrank_a_curated_glossary():
    """An observed failure beats a term someone typed once."""
    static = [Pack(name="glossary", terms=(
        Term("Kubernetes", "glossary", rarity=0.9),))]
    result = compile_for(
        provider="faster-whisper", static_packs=static,
        sources=sources(corrections=(("Flarp", "Clarp"),)))
    # Highest-ranked term is emitted last for token budgets.
    assert result.payload.rstrip(".").endswith("Clarp")


def test_recent_speech_beats_workspace_identifiers():
    result = compile_for(
        provider="faster-whisper",
        sources=sources(
            recent_transcripts=("we were discussing Cartesia",),
            identifiers=tuple(f"Symbol{i}" for i in range(40))))
    assert result.payload.rstrip(".").endswith("Cartesia")


def test_agents_survive_a_tight_budget_via_their_floor():
    result = compile_for(
        provider="elevenlabs",
        sources=sources(
            agent_names=["Rachel", "Arnold"],
            identifiers=tuple(f"Symbol{i}" for i in range(200))))
    assert "Rachel" in result.payload


def test_term_budget_provider_gets_a_comma_list_not_prose():
    result = compile_for(
        provider="deepgram", sources=sources(project_name="Clarp"))
    assert result.unit == Unit.TERMS
    assert "Terms that may come up" not in result.payload


def test_roomy_provider_gets_prose():
    result = compile_for(
        provider="assemblyai",
        sources=sources(project_name="Clarp", agent_names=["Rachel"]))
    assert result.payload.startswith("Terms that may come up:")


def test_no_sources_yields_an_empty_payload_not_an_error():
    assert compile_for(provider="faster-whisper").payload == ""


# --- failure policy --------------------------------------------------------

def test_a_broken_generator_never_breaks_transcription(monkeypatch):
    def boom(*_a, **_kw):
        raise RuntimeError("generator exploded")

    monkeypatch.setattr(vocab_compile, "agents_pack", boom)
    result = compile_for(
        provider="faster-whisper", sources=sources(agent_names=["Rachel"]))
    assert result.payload == ""          # degraded, did not raise
    assert result.capacity == 223


def test_a_failed_audit_write_never_breaks_transcription(monkeypatch):
    import lib.vocab_store as store

    def boom(*_a, **_kw):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, "record_run", boom)
    result = vocab_compile.compile_and_record(
        provider="faster-whisper", sources=sources(project_name="Clarp"))
    assert "Clarp" in result.payload     # the turn still gets its prompt


def test_compiled_payload_never_exceeds_the_provider_budget():
    result = compile_for(
        provider="elevenlabs",
        sources=sources(identifiers=tuple(f"Identifier{i}" for i in range(500))))
    assert result.used <= result.capacity
    assert all(len(t.text) <= 20 for t in result.terms)
