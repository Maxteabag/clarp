"""How a spoken turn chooses its agent.

The AI router used to be the only answer to an unaddressed utterance. That is
a large hammer for "she said one name", and an expensive one for "she said
nothing at all" — a model call, a second or more of latency, and a bill, to
answer a question a count can often settle.

Four modes, explicit:

    off          never delegate; the turn stays with the focused agent
    first_name   the earliest agent named wins
    most_names   whoever is named most wins
    ai           a spoken name still wins outright; the model decides the rest

Only `ai` ever reaches the model, and even then only when no name was heard.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import settings_store
from .routing import _clean_word, _word_tokens

MODE_KEY = "orchestrator.addressing_mode"

OFF = "off"
FIRST_NAME = "first_name"
MOST_NAMES = "most_names"
AI = "ai"

MODES = (OFF, FIRST_NAME, MOST_NAMES, AI)
DEFAULT_MODE = AI


@dataclass(frozen=True)
class Addressed:
    """Who the turn is for, and whether the model still needs asking."""
    session: str | None = None
    #: True only in `ai` mode when no name was spoken.
    needs_ai: bool = False
    #: Which rule decided, for the audit row and the settings screen.
    reason: str = ""


def mode() -> str:
    value = (settings_store.get_text(MODE_KEY, default=DEFAULT_MODE) or "").strip()
    return value if value in MODES else DEFAULT_MODE


def set_mode(value: str) -> str:
    chosen = (value or "").strip()
    if chosen not in MODES:
        raise ValueError(f"unknown addressing mode: {value}")
    settings_store.set_text(MODE_KEY, chosen)
    return chosen


def spoken_names(text: str, agents: dict) -> list[str]:
    """Sessions whose persona name appears in `text`, in the order spoken.

    Unlike the vocative matcher in `routing`, this looks at the whole
    utterance: "ask Rachel about Josh's branch" names two people, and which
    one wins is the mode's business rather than this function's.
    """
    if not text or not agents:
        return []
    by_word: dict[str, str] = {}
    for session, info in agents.items():
        name = _clean_word(str((info or {}).get("name") or "")).lower()
        if name:
            by_word.setdefault(name, session)
    return [
        by_word[token]
        for token, _start, _end in _word_tokens(text)
        if token in by_word
    ]


def resolve(text: str, agents: dict, *, mode: str,
            recent: list[str] | None = None) -> Addressed:
    """Decide who a turn is for under `mode`.

    `recent` is most-recently-spoken-to first. It only ever breaks a tie: a
    nudge toward the conversation already in progress when counting cannot
    separate two names, never a thumb on the scale against a clear winner.
    """
    chosen = mode if mode in MODES else DEFAULT_MODE
    order = spoken_names(text, agents)

    if chosen == OFF:
        return Addressed(reason="delegation off")

    if not order:
        if chosen == AI:
            return Addressed(needs_ai=True, reason="no name spoken")
        return Addressed(reason="no name spoken")

    if chosen in (FIRST_NAME, AI):
        return Addressed(session=order[0], reason="first name spoken")

    counts: dict[str, int] = {}
    for session in order:
        counts[session] = counts.get(session, 0) + 1
    best = max(counts.values())
    leaders = [s for s, n in counts.items() if n == best]
    if len(leaders) == 1:
        return Addressed(session=leaders[0], reason="most names spoken")

    for session in (recent or []):
        if session in leaders:
            return Addressed(
                session=session, reason="tie broken by recent conversation")
    # No recency to lean on: first spoken is the least surprising tiebreak.
    for session in order:
        if session in leaders:
            return Addressed(session=session, reason="tie broken by order")
    return Addressed(reason="undecided")
