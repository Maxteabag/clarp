"""Permission-intent classification — strict regex pass.

When a non-current agent has raised a herald ("Rachel here, ready for an
update"), we hold their real audio in a buffer until the user grants them
the floor. The user's next utterance is checked against this module to
decide whether to release that buffer.

This is intentionally STRICT — we'd rather miss a grant (and replay the
herald) than play a backlog the user didn't ask for. An LLM fallback can
catch ambiguous cases later; for now keyword regex covers the obvious
phrases.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


# Words that signal the user is opening a channel — "go ahead, talk".
AFFIRMATIVES = (
    r"yes", r"yeah", r"yep", r"yup", r"sure", r"ok", r"okay",
    r"go\s*ahead", r"go\s*on", r"go\s*for\s*it",
    r"shoot", r"tell\s*me", r"talk(?:\s*to\s*me)?",
    r"what(?:'s| is)\s*up", r"what(?:'s| is)\s*it",
    r"let(?:'s| us)\s*hear\s*(?:it|you)?",
    r"hit\s*me",
    r"i'?m\s*listening",
)
# Words that mean "shut up for now".
DECLINES = (
    r"not\s*now", r"later", r"in\s*a\s*(?:minute|sec(?:ond)?|moment|bit)",
    r"hold\s*on", r"wait", r"hush", r"shh+",
    r"be\s*quiet", r"quiet", r"stop\s*talking",
)


@dataclass
class IntentResult:
    grants: list[str] = field(default_factory=list)
    declines: list[str] = field(default_factory=list)

    @property
    def has_signal(self) -> bool:
        return bool(self.grants or self.declines)


def _normalize(text: str) -> str:
    """Lowercase, collapse whitespace, strip punctuation we don't need.

    Apostrophes stay so `what's` survives intact for the vocative pattern;
    other punctuation collapses to a space.
    """
    t = text.lower().strip()
    t = re.sub(r"[.,;:!?\"]+", " ", t)
    t = re.sub(r"\s+", " ", t)
    return t


def _name_pattern(name: str) -> str:
    """Match a name as a whole word, e.g. \\brachel\\b."""
    return rf"\b{re.escape(name.lower())}\b"


def classify_intent(text: str, candidates: list[str]) -> IntentResult:
    """Return per-candidate intent based on the transcribed utterance.

    `candidates` is the list of agent persona names (e.g. ["Rachel", "Bella"])
    whose heralds are currently pending. Names are matched case-insensitively
    as whole words.

    Grant signals (any of):
      * candidate name within 4 words of an affirmative phrase
      * the utterance starts with the candidate's name + interrogative
        ("rachel what's up", "rachel?")
      * "your turn" or imperative "tell me" near the name

    Decline signals: candidate name within 4 words of a decline phrase, OR a
    bare decline phrase when only one candidate is pending (covers "not now").

    Everything else: classified as a mention; no grant, no decline.
    """
    result = IntentResult()
    if not text or not candidates:
        return result

    norm = _normalize(text)
    affirm_re = re.compile(
        r"(?:^|\s)(?:" + "|".join(AFFIRMATIVES) + r")(?:\s|$)"
    )
    decline_re = re.compile(
        r"(?:^|\s)(?:" + "|".join(DECLINES) + r")(?:\s|$)"
    )

    bare_decline = bool(decline_re.search(norm))

    for cand in candidates:
        name_re = re.compile(_name_pattern(cand))
        name_match = name_re.search(norm)
        if not name_match:
            # No name → if only one candidate AND bare decline → decline it.
            if bare_decline and len(candidates) == 1:
                result.declines.append(cand)
            continue

        # Look at the window around the name (±3 words). Tighter than ±4 so
        # that "go ahead rachel but bella later" doesn't grant Bella from
        # "go ahead" being in her window.
        words = norm.split()
        try:
            idx = next(i for i, w in enumerate(words) if w == cand.lower())
        except StopIteration:
            continue
        window = " ".join(words[max(0, idx - 3): idx + 4])

        if affirm_re.search(window):
            result.grants.append(cand)
            continue
        if decline_re.search(window):
            result.declines.append(cand)
            continue

        # Vocative starts: "rachel what's up", "rachel go", "rachel?"
        rest = " ".join(words[idx + 1: idx + 5])
        if re.match(
            r"(?:what(?:'s| is)?\s*(?:up|it|going\s*on)|go|talk|shoot|tell\s*me)",
            rest,
        ):
            result.grants.append(cand)
            continue
        # Trailing question mark right after the name → grant
        # ("rachel?" = "what is it, rachel?")
        original = text.strip()
        m = re.search(rf"{_name_pattern(cand)}\s*\?", original.lower())
        if m:
            result.grants.append(cand)
            continue

    return result
