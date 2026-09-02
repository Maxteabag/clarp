"""Whisper hallucination filter — pure functions, no I/O.

Bugs this module pins (see TESTS.md):
- B4: Whisper invents "Thank you." on silent input.
- B5: Multi-sentence YouTube outros ("Thank you very much. I hope you
  enjoyed it.") should be dropped wholesale.
- B7: Angle-bracket fragments like "<space>" cause ElevenLabs to silently
  stop — strip them before TTS.
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
    "mm-hmm", "uh-huh", "uhm", "um",
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


def _norm(s: str) -> str:
    return s.lower().strip().strip(" .,!?—–-").strip()


def is_pure_hallucination(text: str) -> bool:
    """Return True iff every sentence in `text` is a known hallucination."""
    full = _norm(text)
    if not full:
        return True
    if full in HALLUCINATION_PHRASES:
        return True
    sentences = [
        p for p in text.replace("!", ".").replace("?", ".").split(".") if p.strip()
    ]
    normed = [_norm(s) for s in sentences if _norm(s)]
    return bool(normed) and all(s in HALLUCINATION_PHRASES for s in normed)


# Sanitisation chain applied to text before sending it to ElevenLabs.
# Each substitution is small and deliberate; keep them as a list so tests can
# verify the chain order if needed.
def sanitize_for_tts(text: str, max_chars: int = 8000) -> str:
    if not text:
        return ""
    t = text
    # Triple-backtick code blocks would just be noise read aloud.
    t = re.sub(r"```.*?```", " code block omitted ", t, flags=re.DOTALL)
    # Keep inline-code content (the backticks are noise).
    t = re.sub(r"`([^`]+)`", r"\1", t)
    # Markdown links → just the link text.
    t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t)
    # URLs.
    t = re.sub(r"https?://\S+", "", t)
    # Strip XML/SSML-looking tag fragments — ElevenLabs silently stops at
    # things like '<space>', '<?>' etc. because they look like markup.
    t = re.sub(r"<[^>]*>", "", t)
    # Markdown emphasis / list markers.
    t = re.sub(r"[*_#>]+", "", t)
    # Collapse whitespace.
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) > max_chars:
        t = t[:max_chars].rsplit(" ", 1)[0] + "…"
    return t
