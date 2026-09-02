"""B4, B5, B7: Whisper hallucination filter and TTS sanitiser."""

from __future__ import annotations

import pytest

from lib.hallucinations import (
    HALLUCINATION_PHRASES,
    is_pure_hallucination,
    sanitize_for_tts,
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


# ----- TTS sanitiser (B7) -----


def test_angle_bracket_fragments_are_stripped():
    """B7: '<space>' fragments cause ElevenLabs to silently stop."""
    out = sanitize_for_tts("show only <space> / <?> in the bottom bar")
    assert "<" not in out and ">" not in out
    assert "show only" in out
    assert "in the bottom bar" in out


def test_inline_code_content_is_kept():
    """Backticks go, content stays — usernames and short paths need to be spoken."""
    out = sanitize_for_tts("Logged in as `octocat`, switch with `gh auth switch`")
    assert "octocat" in out
    assert "gh auth switch" in out
    assert "`" not in out


def test_code_block_is_replaced_with_omitted_marker():
    out = sanitize_for_tts("here is code\n```\nprint('hi')\n```\nend")
    assert "code block omitted" in out
    assert "print('hi')" not in out


def test_long_text_truncates_at_word_boundary():
    """B18: characters above the cap get trimmed at a space, not mid-word."""
    long = ("hello world " * 1000).strip()
    out = sanitize_for_tts(long, max_chars=120)
    assert out.endswith("…")
    assert len(out) <= 130
    assert "helloworld" not in out  # word boundary intact


def test_urls_are_removed():
    out = sanitize_for_tts("see https://example.com/page for details")
    assert "https://" not in out
    assert "details" in out
