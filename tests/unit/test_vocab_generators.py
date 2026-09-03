"""Pack generators.

The behaviour worth protecting is what each generator *refuses* to emit:
ordinary speech, whole turns, and numeric branch noise all waste a budget
that is only 224 tokens wide on the default model.
"""
from __future__ import annotations

from lib.vocab_budget import Budget, Pack, Unit, compile_packs
from lib.vocab_generators import (
    agents_pack,
    corrections_pack,
    estimate_rarity,
    recent_speech_pack,
    workspace_pack,
)


# --- rarity heuristic ------------------------------------------------------

def test_ordinary_words_score_low_and_coined_names_score_high():
    assert estimate_rarity("the") < 0.2
    assert estimate_rarity("about") < 0.2
    assert estimate_rarity("Clarp") > 0.8
    assert estimate_rarity("EcitServicePortal") > 0.9


def test_a_phrase_is_as_rare_as_its_rarest_word():
    """'Knut Thomas' must not be dragged down by an ordinary second word."""
    assert estimate_rarity("Knut Thomas") > 0.8


def test_identifiers_are_always_treated_as_rare():
    assert estimate_rarity("TelephonyRangeCalculator") > 0.9
    assert estimate_rarity("notification_projection") > 0.9


# --- agents ----------------------------------------------------------------

def test_agent_names_are_high_rarity_and_deduplicated():
    terms = agents_pack(["Rachel", "Arnold", "rachel", "  ", "OPUS"])
    assert [t.text for t in terms] == ["Rachel", "Arnold", "OPUS"]
    assert all(t.rarity >= 0.9 for t in terms)


# --- workspace -------------------------------------------------------------

def test_project_name_is_included_and_ranked_top():
    terms = workspace_pack(project_name="Clarp", identifiers=["Widget"])
    assert terms[0].text == "Clarp"
    assert terms[0].rarity >= 0.95


def test_branch_names_become_spoken_phrases_without_numeric_noise():
    terms = workspace_pack(branches=["worktree-751-shared-plain-library"])
    assert [t.text for t in terms] == ["shared plain library"]


def test_commit_subjects_contribute_only_salient_tokens():
    terms = workspace_pack(
        commit_subjects=["fix the bug in TelephonyRangeCalculator for all users"])
    texts = [t.text for t in terms]
    assert "TelephonyRangeCalculator" in texts
    assert "the" not in texts and "for" not in texts and "all" not in texts


# --- recent speech ---------------------------------------------------------

def test_recent_speech_extracts_tokens_not_whole_turns():
    """A 300-word turn must not be pasted into a 224-token budget."""
    turn = " ".join(["this is just ordinary conversation about the thing"] * 40)
    turn += " Clarp"
    terms = recent_speech_pack([turn])
    assert [t.text for t in terms] == ["Clarp"]


def test_more_recent_turns_outrank_older_ones():
    terms = recent_speech_pack(["Cartesia", "Deepgram"])  # most recent first
    by_text = {t.text: t for t in terms}
    assert by_text["Cartesia"].recency > by_text["Deepgram"].recency


def test_recent_speech_is_capped():
    words = " ".join(f"Rareword{i}" for i in range(100))
    assert len(recent_speech_pack([words], limit=10)) == 10


def test_recent_speech_survives_the_tight_budget_that_a_full_turn_would_blow():
    """The regression this generator exists to prevent."""
    turn = ("So I was saying that we should look at Clarp and the "
            "EcitServicePortal work " + "and talk about it some more " * 30)
    pack = Pack(name="recent-speech", terms=tuple(recent_speech_pack([turn])))
    budget = Budget("faster-whisper", "small.en", Unit.TOKENS, 223)
    result = compile_packs([pack], budget)
    assert result.used <= 223
    assert "Clarp" in result.payload


# --- corrections -----------------------------------------------------------

def test_corrections_are_confusable_so_they_bypass_the_rarity_floor():
    terms = corrections_pack([("Flarp", "Clarp"), ("Clark", "Clarp")])
    assert all(t.confusable for t in terms)
    assert [t.text for t in terms] == ["Clarp"]  # deduplicated by intent


def test_a_correction_records_what_was_misheard():
    terms = corrections_pack([("Flarp", "Clarp")])
    assert terms[0].say_as == "Flarp"


def test_correction_for_an_ordinary_looking_word_still_ships_when_tight():
    pack = Pack(name="corrections", terms=tuple(
        corrections_pack([("Marcus", "Mark")])))
    result = compile_packs(
        [pack], Budget("faster-whisper", "small.en", Unit.TOKENS, 60))
    assert "Mark" in result.payload


# --- generators never break transcription ---------------------------------

def test_generators_tolerate_empty_and_blank_input():
    assert agents_pack([]) == []
    assert workspace_pack() == []
    assert recent_speech_pack([""]) == []
    assert corrections_pack([("", "")]) == []
