"""Spoken tutorial lines for the Flic controller (`GET /controller-narration`).

The iOS tutorial mode says what a gesture would do instead of doing it. The
text is the client's; the Host owns the voice, the synthesis and the cache so
one line is paid for once per Host, not once per phone.
"""
from __future__ import annotations

import hashlib
import re

MAX_TEXT_CHARS = 240
NARRATION_PERSONA = "Rachel"

_WHITESPACE = re.compile(r"\s+")


def normalize_text(raw: str) -> str:
    """Collapse whitespace, drop control characters, cap the length.

    Returns "" for anything that is not a short spoken line.
    """
    text = "".join(ch for ch in str(raw or "") if ch.isprintable())
    text = _WHITESPACE.sub(" ", text).strip()
    if not text or len(text) > MAX_TEXT_CHARS:
        return ""
    return text


def voice_id(cfg) -> str:
    """The narration voice: the persona's configured Cartesia voice."""
    return str(cfg.cartesia_voice_for(NARRATION_PERSONA) or "")


def cache_key(*, text: str, voice: str, model: str) -> str:
    """Every input that can change the bytes is part of the key."""
    return hashlib.sha256(
        "\0".join(("v1", voice, model, text)).encode()).hexdigest()
