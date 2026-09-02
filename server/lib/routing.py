"""Voice-routing logic — pick which agent a spoken message is for.

All pure functions, no I/O. Easy to unit-test.

Bugs this module pins (see TESTS.md):
- B12: agent name within the first few addressed words wins, not just position 0.
- B13: Whisper transcribes "Bella" as "Bell"; fuzzy match recovers it.
"""

from __future__ import annotations

import difflib
import re

LEADING_ADDRESS_FILLERS = frozenset({
    "also",
    "and",
    "hey",
    "hi",
    "ok",
    "okay",
    "please",
    "so",
    "um",
    "uh",
    "yo",
    "yeah",
    "yep",
})
MAX_ADDRESS_TOKENS = 3


def word_similarity(a: str, b: str) -> float:
    """Cheap similarity between two short words (0..1).

    Combines a prefix match and a character-overlap score so Whisper mistakes
    like "Bell" map to "Bella" but unrelated words don't.
    """
    a = (a or "").lower()
    b = (b or "").lower()
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a.startswith(b) or b.startswith(a):
        # Strong prefix score — "Bell"/"Bella" should land high.
        return 0.85 + 0.1 * min(len(a), len(b)) / max(len(a), len(b))
    return difflib.SequenceMatcher(None, a, b).ratio()


def _clean_word(word: str) -> str:
    return re.sub(r"[^A-Za-z]", "", word or "")


def _word_tokens(text: str) -> list[tuple[str, int, int]]:
    tokens: list[tuple[str, int, int]] = []
    for m in re.finditer(r"[A-Za-z][A-Za-z0-9_-]*", text or ""):
        clean = _clean_word(m.group(0)).lower()
        if clean:
            tokens.append((clean, m.start(), m.end()))
    return tokens


def _strip_address_prefix(text: str, end: int) -> str:
    """Drop leading filler/name material through `end` and tidy punctuation."""
    rest = (text or "")[end:]
    return re.sub(r"^[\s,;:!?.-]+", "", rest).strip()


def resolve_agent_by_spoken_name(
    text: str, agents: dict
) -> tuple[str | None, str]:
    """Look for a vocative occurrence of any agent's persona name within the
    first three words of `text`.

    When a prefix address is found, the returned text strips the leading
    filler/name material so the agent gets the actual request.

    Args:
        text: the transcribed user message.
        agents: { session_id: { "name": <persona>, ... } }.

    Returns:
        (session_id, text) when an agent is found.
        (None,       text) when nothing matches.
    """
    if not text:
        return None, text
    tokens = _word_tokens(text)
    if not tokens:
        return None, text

    # Candidates sorted longest-first so "Bella" beats "Bel" when both exist.
    candidates = [
        ((info or {}).get("name", "").strip(), session_id)
        for session_id, info in agents.items()
    ]
    candidates = [(p, sid) for p, sid in candidates if p]
    candidates.sort(key=lambda x: -len(x[0]))

    first = 0
    while first < len(tokens) and tokens[first][0] in LEADING_ADDRESS_FILLERS:
        first += 1
    address_tokens = tokens[first:first + MAX_ADDRESS_TOKENS]
    if not address_tokens:
        return None, text

    # 1) Exact known-agent token in the first addressed words.
    for token, _start, end in address_tokens:
        for persona, session_id in candidates:
            if token == _clean_word(persona).lower():
                return session_id, _strip_address_prefix(text, end)

    # 2) Fuzzy fallback against the first addressed word only, with a high bar
    # so we don't accidentally route ordinary sentences.
    first_word, _start, end = address_tokens[0]
    if len(first_word) < 3:
        return None, text

    best_score = 0.0
    best_match: tuple[str, str] | None = None
    for persona, session_id in candidates:
        score = word_similarity(first_word, persona)
        if score > best_score:
            best_score = score
            best_match = (persona, session_id)
    if best_match and best_score >= 0.78:
        return best_match[1], _strip_address_prefix(text, end)

    return None, text
