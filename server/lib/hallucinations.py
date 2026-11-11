"""Whisper hallucination filter — pure functions, no I/O.

Bugs this module pins (see TESTS.md):
- B4: Whisper invents "Thank you." on silent input.
- B5: Multi-sentence YouTube outros ("Thank you very much. I hope you
  enjoyed it.") should be dropped wholesale.
"""

from __future__ import annotations

import re


# Phrases Whisper invents on silent or noisy clips. Matched per-sentence so
# multi-sentence outros are caught.
HALLUCINATION_PHRASES: frozenset[str] = frozenset({
    # Generic gratitude
    "thank you", "thanks", "thanks for watching",
    "thank you for watching", "thank you very much",
    "thank you so much", "thanks so much", "thanks a lot",
    # YouTube outros
    "i hope you enjoyed it", "i hope you enjoyed this video",
    "i hope you enjoyed", "hope you enjoyed it",
    "i'll see you in the next one", "see you next time",
    "see you in the next video", "see you",
    # Time-of-day farewells
    "good night", "have a good night", "goodnight",
    "good morning", "good evening",
    # Goodbyes
    "bye", "bye bye", "goodbye",
    # Stutter fragments
    "you", ".", "...", "..",
    # YouTube CTAs
    "subscribe", "like and subscribe", "please subscribe",
    "don't forget to subscribe", "like comment and subscribe",
    # Filler / single-word noise
    "mm-hmm", "uh-huh", "uhm", "um", "hmm", "hm", "mmm",
    "okay", "ok", "yeah",
    # Music / SFX captions
    "music", "[music]", "(music)", "[applause]", "(applause)",
    "♪", "♪♪", "♪ ♪",
    # Misc IVR-flavored noise
    "please", "please transfer",
    # Conversational filler
    "you know what i mean", "you know",
    # Single articles
    "and", "the", "a",
})


# Subtitle-trained Whisper models emit an open-ended vocabulary of stage
# directions for non-speech audio. Match the caption shape only when one
# correctly paired bracketed span is the entire sentence; parenthetical words
# inside real speech must remain untouched.
_CAPTION_RE = re.compile(r"(?:\([^()\[\]\r\n]*\)|\[[^()\[\]\r\n]*\])")


def _norm(s: str) -> str:
    return s.lower().strip().strip(" .,!?—–-").strip()


def _is_caption(sentence: str) -> bool:
    return _CAPTION_RE.fullmatch(sentence.strip()) is not None


def is_pure_hallucination(text: str) -> bool:
    """Return True iff every sentence in `text` is a known hallucination."""
    full = _norm(text)
    if not full:
        return True
    if full in HALLUCINATION_PHRASES or _is_caption(full):
        return True
    sentences = [
        p for p in text.replace("!", ".").replace("?", ".").split(".") if p.strip()
    ]
    normed = [_norm(s) for s in sentences if _norm(s)]
    return bool(normed) and all(
        s in HALLUCINATION_PHRASES or _is_caption(s) for s in normed
    )
