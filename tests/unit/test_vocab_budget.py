"""Budget-aware compilation of transcription context packs.

The behaviour that matters here is not "does it produce a string" but the
three scaling rules: depth, rarity floor, and emitted form. Each has a test
that would fail loudly if someone flattened packs back into fixed lists.
"""
from __future__ import annotations

import pytest

from lib.vocab_budget import (
    Budget,
    Form,
    Pack,
    Term,
    Unit,
    WHISPER_PROMPT_TOKENS,
    compile_packs,
    estimate_tokens,
    rarity_floor_for,
    select,
)


def whisper_budget(capacity: int = WHISPER_PROMPT_TOKENS) -> Budget:
    return Budget(
        provider="faster-whisper", model="small.en",
        unit=Unit.TOKENS, capacity=capacity, supports_prose=True,
    )


def terms(pack: str, *names: str, rarity: float = 0.9, recency: float = 0.0):
    return tuple(
        Term(text=n, pack=pack, rarity=rarity, recency=recency) for n in names
    )


# --- elasticity ------------------------------------------------------------

def test_same_pack_yields_more_on_a_bigger_budget():
    """One pack definition, deeper draw when the model allows. No variants."""
    pack = Pack(name="workspace", terms=terms(
        "workspace", *[f"Identifier{i}" for i in range(60)]))
    small = compile_packs([pack], whisper_budget(40))
    large = compile_packs([pack], Budget(
        provider="assemblyai", model="universal-3",
        unit=Unit.WORDS, capacity=1500, supports_prose=True))
    assert len(small.terms) < len(large.terms)
    assert len(large.terms) == 60  # roomy budget drains the pack


def test_nothing_fits_when_capacity_is_zero():
    pack = Pack(name="p", terms=terms("p", "Clarp"))
    result = compile_packs([pack], whisper_budget(0))
    assert result.terms == []
    assert result.payload == ""
    assert [why for _, why in result.dropped] == ["no budget"]


# --- floors ----------------------------------------------------------------

def test_floor_protects_a_small_pack_from_a_large_one():
    """People must survive even when Workspace out-ranks it everywhere."""
    people = Pack(name="people", priority=1.0, floor=2,
                  terms=terms("people", "Knut Thomas", "Oscar", rarity=0.7))
    workspace = Pack(name="workspace", priority=3.0, terms=terms(
        "workspace", *[f"Symbol{i}" for i in range(50)], rarity=0.95))
    result = compile_packs([people, workspace], whisper_budget(30))
    kept = {t.pack for t in result.terms}
    assert "people" in kept
    assert sum(1 for t in result.terms if t.pack == "people") >= 2


def test_floor_terms_bypass_the_rarity_gate():
    """A curated name ships even if it looks common."""
    people = Pack(name="people", floor=1,
                  terms=terms("people", "Bill", rarity=0.01))
    result = compile_packs([people], whisper_budget(60))
    assert [t.text for t in result.terms] == ["Bill"]


# --- rarity floor scales with budget --------------------------------------

def test_rarity_floor_is_stricter_when_the_budget_is_tight():
    assert rarity_floor_for(whisper_budget(60)) > rarity_floor_for(
        whisper_budget(223))
    assert rarity_floor_for(Budget("a", "b", Unit.WORDS, 1500)) == 0.0


def test_common_words_are_dropped_at_a_tight_budget():
    pack = Pack(name="mixed", terms=(
        Term("Clarp", "mixed", rarity=0.98),
        Term("meeting", "mixed", rarity=0.10),
    ))
    result = compile_packs([pack], whisper_budget(60))
    assert [t.text for t in result.terms] == ["Clarp"]
    assert ("below rarity floor", "meeting") == (
        result.dropped[0][1], result.dropped[0][0].text)


def test_confusable_terms_survive_the_rarity_gate():
    """Observed failures earn their bytes even when the word looks ordinary."""
    pack = Pack(name="corrections", terms=(
        Term("Clark", "corrections", rarity=0.05, confusable=True),
    ))
    result = compile_packs([pack], whisper_budget(60))
    assert [t.text for t in result.terms] == ["Clark"]


# --- ordering: Whisper weights the tail -----------------------------------

def test_highest_ranked_term_is_emitted_last_for_token_budgets():
    pack = Pack(name="p", terms=(
        Term("LowValue", "p", rarity=0.5, recency=0.0),
        Term("Clarp", "p", rarity=0.99, recency=1.0),
    ))
    result = compile_packs([pack], whisper_budget())
    assert result.payload.rstrip(".").endswith("Clarp")


def test_term_list_providers_keep_rank_order():
    """Keyterm APIs are order-insensitive; best-first is the clearer contract."""
    budget = Budget("deepgram", "nova-3", Unit.TERMS, capacity=50)
    pack = Pack(name="p", terms=(
        Term("LowValue", "p", rarity=0.5),
        Term("Clarp", "p", rarity=0.99, recency=1.0),
    ))
    result = compile_packs([pack], budget)
    assert result.payload.split(", ")[0] == "Clarp"
    assert result.form == Form.TERMS


# --- form scales with budget ----------------------------------------------

def test_tight_budget_emits_bare_terms_and_roomy_emits_prose():
    pack = Pack(name="p", terms=terms("p", "Clarp", "EcitServicePortal"))
    tight = compile_packs([pack], whisper_budget(100))
    roomy = compile_packs([pack], Budget(
        provider="assemblyai", model="universal-3",
        unit=Unit.WORDS, capacity=1500, supports_prose=True))
    assert tight.form == Form.TERMS
    assert roomy.form == Form.PROSE
    assert roomy.payload.startswith("Terms that may come up:")


# --- provider caps ---------------------------------------------------------

def test_per_term_character_cap_is_applied_before_sending():
    """ElevenLabs realtime caps terms at 20 chars; truncate deliberately."""
    budget = Budget("elevenlabs", "scribe-v2", Unit.TERMS,
                    capacity=50, max_term_chars=20)
    pack = Pack(name="p", terms=terms("p", "EcitServicePortalCalculationEngine"))
    result = compile_packs([pack], budget)
    assert len(result.terms[0].text) == 20


def test_word_unit_costs_multi_word_phrases_correctly():
    budget = Budget("assemblyai", "universal-3", Unit.WORDS, capacity=3)
    pack = Pack(name="p", terms=(
        Term("Knut Thomas Berg", "p", rarity=0.99, recency=1.0),
        Term("Oscar", "p", rarity=0.95),
    ))
    result = compile_packs([pack], budget)
    assert [t.text for t in result.terms] == ["Knut Thomas Berg"]
    assert ("Oscar", "over budget") == (
        result.dropped[0][0].text, result.dropped[0][1])


# --- audit / transparency --------------------------------------------------

def test_audit_reports_inclusions_exclusions_and_headroom():
    pack = Pack(name="p", terms=(
        Term("Clarp", "p", rarity=0.99),
        Term("the", "p", rarity=0.01),
    ))
    audit = compile_packs([pack], whisper_budget(60)).audit()
    assert [t["text"] for t in audit["included"]] == ["Clarp"]
    assert audit["dropped"][0] == {
        "text": "the", "pack": "p", "reason": "below rarity floor"}
    assert audit["capacity"] == 60
    assert audit["headroom"] == audit["capacity"] - audit["used"]


def test_never_exceeds_capacity():
    pack = Pack(name="p", terms=terms(
        "p", *[f"Term{i}" for i in range(500)]))
    for capacity in (1, 7, 40, 223):
        result = compile_packs([pack], whisper_budget(capacity))
        assert result.used <= capacity
        assert estimate_tokens(result.payload) <= capacity + 2


def test_duplicates_across_packs_are_recorded_once():
    a = Pack(name="a", terms=terms("a", "Clarp"))
    b = Pack(name="b", terms=terms("b", "clarp"))
    result = compile_packs([a, b], whisper_budget())
    assert len(result.terms) == 1
    assert [why for _, why in result.dropped] == ["duplicate"]


def test_disabled_packs_contribute_nothing():
    pack = Pack(name="off", enabled=False, terms=terms("off", "Clarp"))
    assert compile_packs([pack], whisper_budget()).terms == []


def test_empty_term_text_is_rejected_at_construction():
    with pytest.raises(ValueError):
        Term(text="  ", pack="p")
