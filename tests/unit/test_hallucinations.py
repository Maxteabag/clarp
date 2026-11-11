"""B4, B5: Whisper hallucination filter."""

from __future__ import annotations

import pytest

from lib.hallucinations import (
    HALLUCINATION_PHRASES,
    is_pure_hallucination,
)


@pytest.mark.parametrize("phrase", [
    "Thank you.",
    "Thanks for watching!",
    "Bye bye.",
    "♪♪",
    "[music]",
    "Please subscribe",
    "okay",
    "...",
])
def test_known_hallucinations_are_filtered(phrase):
    """B4: single-phrase hallucinations on silent audio."""
    assert is_pure_hallucination(phrase), f"expected filter to catch: {phrase!r}"


def test_youtube_outro_multi_sentence_is_filtered():
    """B5: 'Thank you very much. I hope you enjoyed it. Good night.'"""
    text = "Thank you very much. I hope you enjoyed it. Good night."
    assert is_pure_hallucination(text)


@pytest.mark.parametrize("real_message", [
    "Thank you, that worked perfectly",          # contains 'thank you' but is real
    "Could you check the logs please",
    "Rachel, write me an essay about coffee",
])
def test_real_messages_pass_through(real_message):
    """Negative: filter must not eat legitimate messages."""
    assert not is_pure_hallucination(real_message)


def test_every_seed_phrase_is_caught_by_itself():
    """Property: every entry in the seed set is recognised as a hallucination
    when fed verbatim (i.e. the filter's own data is self-consistent)."""
    for phrase in HALLUCINATION_PHRASES:
        assert is_pure_hallucination(phrase), phrase


@pytest.mark.parametrize("caption", [
    "(indistinct)",
    "(air whooshing)",
    "(lips smacking)",
    "(speaking in foreign language)",
    "[door closes]",
])
def test_whole_stage_direction_captions_are_filtered(caption):
    assert is_pure_hallucination(caption)


@pytest.mark.parametrize("filler", ["Hmm.", "Hm", "MMM!"])
def test_bare_humming_fillers_are_filtered(filler):
    assert is_pure_hallucination(filler)


@pytest.mark.parametrize("real_message", [
    "The quick brown fox (mostly) jumps.",
    "Check the (new) portal please.",
    "Use [draft] as the label.",
    "This has a mismatched (caption] marker.",
])
def test_caption_shape_does_not_remove_real_messages(real_message):
    assert not is_pure_hallucination(real_message)


def test_caption_and_known_hallucination_sentences_are_filtered_together():
    assert is_pure_hallucination("(air whooshing). Thank you.")
