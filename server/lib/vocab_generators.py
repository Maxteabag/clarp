"""Sources that populate transcription context packs.

Each generator returns a ranked, open-ended list of `Term`s. None of them
knows the budget - depth is decided later by `vocab_budget.compile_packs`
against the active model. That separation is what lets one profile yield 12
terms on `small.en` and 400 on a roomy provider without reconfiguration.

Generators must never raise into the transcription path: a broken glossary is
a worse outcome than a slightly worse prompt, so every entry point degrades to
an empty list and logs.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from .vocab_budget import Term

# Words we never want to spend budget biasing. Deliberately small: the goal is
# to catch the words that dominate ordinary speech, not to be a dictionary.
# Anything absent scores as "rare", which is the behaviour we want - a made-up
# product name should always look rare.
_COMMON = frozenset("""
a about after all also am an and any are as at back be because been before
being but by call can come could day did do does doing done down each even
every first for from get give go going good got had has have he her here him
his how i if in into is it its just know like little long look made make man
many may me more most much must my new no not now of on one only or other our
out over own people put said same say see she should so some take than that
the their them then there these they thing think this those time to too two up
us use very want was way we well went were what when where which while who
why will with work would year you your
""".split())

# Speech that is common in this product's domain but carries no biasing value.
_STOPWORDS = frozenset({
    "agent", "agents", "session", "server", "model", "voice", "audio",
    "okay", "yeah", "yes", "no", "hmm", "uh", "um", "gonna", "kinda",
})

# `_` is a word character here so snake_case identifiers survive tokenisation
# as one unit; splitting them first would hide them from the identifier test.
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9'\-_]*")
_IDENTIFIER = re.compile(r"\b(?:[A-Z][a-z0-9]+){2,}\b|\b[a-z]+(?:_[a-z0-9]+)+\b")


def estimate_rarity(text: str) -> float:
    """Score 0..1 for how much a term is worth biasing.

    Not a real frequency model - a heuristic that reliably separates proper
    nouns and identifiers from ordinary speech, which is the distinction the
    budget actually cares about.
    """
    value = str(text or "").strip()
    if not value:
        return 0.0
    words = _WORD.findall(value)
    if not words:
        return 0.5
    scores: list[float] = []
    for word in words:
        lowered = word.lower()
        if lowered in _COMMON or lowered in _STOPWORDS:
            scores.append(0.05)
        elif _IDENTIFIER.fullmatch(word):
            scores.append(0.98)          # CamelCase / snake_case: always rare
        elif any(ch.isdigit() for ch in word):
            scores.append(0.9)
        elif word[:1].isupper():
            scores.append(0.85)          # proper noun
        elif len(word) >= 12:
            # Weak evidence on its own: "conversation" is long and entirely
            # ordinary. Scored below the salience cut so length alone never
            # buys a place in the budget.
            scores.append(0.7)
        else:
            scores.append(0.55)
    # A multi-word phrase is as rare as its rarest part: "Knut Thomas" should
    # not be dragged down by an ordinary second word.
    return max(scores)


def _dedupe(terms: Iterable[Term]) -> list[Term]:
    seen: set[str] = set()
    result: list[Term] = []
    for term in terms:
        key = term.text.casefold()
        if key and key not in seen:
            seen.add(key)
            result.append(term)
    return result


def agents_pack(names: Iterable[str], *, pack: str = "agents") -> list[Term]:
    """Personas the user can address by name."""
    return _dedupe(
        Term(text=name.strip(), pack=pack, rarity=max(estimate_rarity(name), 0.9))
        for name in names
        if str(name or "").strip()
    )


def workspace_pack(
    *,
    identifiers: Iterable[str] = (),
    branches: Iterable[str] = (),
    commit_subjects: Iterable[str] = (),
    project_name: str = "",
    pack: str = "workspace",
) -> list[Term]:
    """Vocabulary derived from the repository an agent is working in.

    This is where a project's own name enters the prompt - the single most
    common transcription failure, because a coined name has no chance of being
    decoded correctly without biasing.
    """
    terms: list[Term] = []
    if project_name.strip():
        terms.append(Term(
            text=project_name.strip(), pack=pack,
            rarity=max(estimate_rarity(project_name), 0.95)))
    for value in identifiers:
        text = str(value or "").strip()
        if text:
            terms.append(Term(text=text, pack=pack, rarity=estimate_rarity(text)))
    for value in branches:
        text = _branch_phrase(value)
        if text:
            terms.append(Term(text=text, pack=pack, rarity=estimate_rarity(text)))
    for subject in commit_subjects:
        for token in _salient_tokens(str(subject or "")):
            terms.append(Term(text=token, pack=pack, rarity=estimate_rarity(token)))
    return _dedupe(terms)


def _branch_phrase(branch: str) -> str:
    """`worktree-751-shared-plain-library` -> `shared plain library`.

    Branch names carry the words a user actually says out loud; the numeric
    and prefix noise around them does not.
    """
    raw = str(branch or "").strip()
    if not raw:
        return ""
    parts = [p for p in re.split(r"[/_\-]+", raw) if p]
    words = [p for p in parts if not p.isdigit() and p.lower() != "worktree"]
    return " ".join(words[:6])


def _salient_tokens(text: str) -> list[str]:
    out: list[str] = []
    for word in _WORD.findall(text):
        if word.lower() in _COMMON or word.lower() in _STOPWORDS:
            continue
        if len(word) < 3:
            continue
        if estimate_rarity(word) >= 0.8:
            out.append(word)
    return out


def recent_speech_pack(
    transcripts: Iterable[str],
    *,
    pack: str = "recent-speech",
    limit: int = 24,
) -> list[Term]:
    """Rare tokens from the last few turns - NOT the turns themselves.

    Feeding whole turns would be self-defeating: a 300-word turn exceeds
    Whisper's entire 224-token budget on its own and would evict every other
    pack. Only the unusual words carry biasing value, and they cost ~15 tokens.

    Recency is graded across the supplied order (most recent first), so a name
    said moments ago outranks one from three turns back.
    """
    ordered = [str(t or "") for t in transcripts]
    terms: list[Term] = []
    total = max(len(ordered), 1)
    for index, text in enumerate(ordered):
        recency = 1.0 - (index / total)
        for token in _salient_tokens(text):
            terms.append(Term(
                text=token, pack=pack,
                rarity=estimate_rarity(token), recency=recency))
    # Keep the highest-recency instance of each token, then the strongest first.
    best: dict[str, Term] = {}
    for term in terms:
        key = term.text.casefold()
        if key not in best or term.recency > best[key].recency:
            best[key] = term
    ranked = sorted(best.values(), key=lambda t: (-t.recency, -t.rarity))
    return ranked[:limit]


def corrections_pack(
    corrections: Iterable[tuple[str, str]],
    *,
    pack: str = "corrections",
) -> list[Term]:
    """Terms the transcriber has demonstrably got wrong.

    Each entry is `(heard, intended)`. These are marked `confusable`, which
    lets them bypass the rarity floor: "Clark" looks like an ordinary name and
    would otherwise be dropped at a tight budget, yet it is exactly the term
    worth spending bytes on because we have observed the failure.
    """
    terms: list[Term] = []
    for heard, intended in corrections:
        text = str(intended or "").strip()
        if not text:
            continue
        terms.append(Term(
            text=text, pack=pack,
            rarity=max(estimate_rarity(text), 0.9),
            confusable=True,
            say_as=str(heard or "").strip() or None,
        ))
    return _dedupe(terms)
