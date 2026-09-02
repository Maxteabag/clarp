"""B12, B13: spoken-name routing must work anywhere in the first three words,
and survive Whisper mishearing the name."""

from __future__ import annotations

import pytest

from lib.routing import (
    resolve_agent_by_spoken_name,
    word_similarity,
)


AGENTS = {
    "claude": {"name": "Mike",   "voice_id": "v-mike"},
    "arnold": {"name": "Arnold", "voice_id": "v-arnold"},
    "rachel": {"name": "Rachel", "voice_id": "v-rachel"},
    "domi":   {"name": "Domi",   "voice_id": "v-domi"},
    "bella":  {"name": "Bella",  "voice_id": "v-bella"},
}


@pytest.mark.parametrize("text,expected_session,forwarded", [
    ("Arnold, do X", "arnold", "do X"),
    ("also Arnold, do X", "arnold", "do X"),
    ("hey Bella fix the bug", "bella", "fix the bug"),
    ("Rachel, write me an essay", "rachel", "write me an essay"),
    ("Hi Rachel, what's up?", "rachel", "what's up?"),
    ("Hey Rachel can you check the logs", "rachel", "can you check the logs"),
    ("Yo, hey Mike how are things", "claude", "how are things"),
    # Name later than position 3 does NOT route.
    ("I will tell Rachel later about the bug", None, "I will tell Rachel later about the bug"),
    ("I was talking about Bella", None, "I was talking about Bella"),
    ("Yeah, go ahead and fix that, Bella.", None, "Yeah, go ahead and fix that, Bella."),
    ("just a normal sentence", None, "just a normal sentence"),
])
def test_exact_name_routing(text, expected_session, forwarded):
    """B12: route only prefix-addressed names and strip the address."""
    sid, returned = resolve_agent_by_spoken_name(text, AGENTS)
    assert sid == expected_session
    assert returned == forwarded


@pytest.mark.parametrize("misheard,expected_session", [
    # B13: Whisper-style clipped variants of female names.
    ("Bell, write me a poem",  "bella"),
    ("Bel, check the diff",    "bella"),
    ("Dom, look at this file", "domi"),
    ("Rach, what time is it?", "rachel"),
])
def test_fuzzy_routing_for_misheard_names(misheard, expected_session):
    sid, returned = resolve_agent_by_spoken_name(misheard, AGENTS)
    assert sid == expected_session
    assert returned


@pytest.mark.parametrize("text", [
    "Tell me how the weather is",
    "I just want a coffee",
    "what's the time",
])
def test_fuzzy_routing_does_not_misfire_on_ordinary_speech(text):
    sid, _ = resolve_agent_by_spoken_name(text, AGENTS)
    assert sid is None


def test_word_similarity_prefix_score_meets_threshold():
    # B13 specifically — the fuzzy threshold is 0.78. Ensure Bell→Bella clears.
    assert word_similarity("Bell", "Bella") >= 0.78
    # Whole different word stays below.
    assert word_similarity("Coffee", "Bella") < 0.5
