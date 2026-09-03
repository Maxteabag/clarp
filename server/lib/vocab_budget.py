"""Budget-aware compilation of transcription context packs.

A *term* is one biasing entry. A *pack* is a ranked, open-ended source of
terms. A *profile* is an ordered set of packs assigned to an agent or team.
Packs are deliberately **elastic**: they do not carry a fixed length, they
carry a ranking, and the compiler draws as deep as the active model allows.
The same profile therefore yields more on ``large-v3-turbo`` than on
``small.en`` with no reconfiguration.

Three things scale with the budget, not just how many terms fit:

* **Depth** - how far down each pack's ranking we draw.
* **Rarity floor** - a tight budget can only afford genuinely rare terms
  (proper nouns, project names); a roomy one can afford ordinary jargon.
* **Emitted form** - a small budget emits bare terms, a large one emits
  prose, because Whisper responds to style priming and some providers accept
  natural-language prompting outright.

Pure logic: no I/O, no database, no provider calls. Everything here is
deterministic so it can be unit tested without audio.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field, replace

# Whisper reserves half of its 448-token window for previous text, leaving 224
# for `initial_prompt`. Overflow is silent - only the FINAL 224 tokens survive -
# so the compiler must fit deliberately and report what it dropped.
WHISPER_PROMPT_TOKENS = 223


class Unit:
    """How a provider meters its biasing budget."""

    TOKENS = "tokens"      # Whisper: one prompt string
    TERMS = "terms"        # Deepgram / ElevenLabs: a list of keyterms
    WORDS = "words"        # AssemblyAI: natural-language prompting


class Form:
    """Shape of the emitted payload."""

    TERMS = "terms"        # bare list: "Clarp, Knut Thomas"
    PROSE = "prose"        # styled sentence, better for priming


@dataclass(frozen=True)
class Budget:
    """What one provider+model will actually accept.

    ``capacity`` is expressed in ``unit``. ``max_term_chars`` truncates
    individual entries for providers that cap them (ElevenLabs realtime caps
    at 20 characters, which silently mangles longer names otherwise).
    """

    provider: str
    model: str
    unit: str
    capacity: int
    max_term_chars: int | None = None
    supports_prose: bool = False

    @property
    def is_tight(self) -> bool:
        """True when the budget cannot hold a general glossary.

        Drives the rarity floor and the emitted form. The threshold is
        deliberately generous: anything under ~300 tokens/terms cannot hold
        an organisation's vocabulary, so only rare terms earn their place.
        """
        return self.capacity < 300


@dataclass(frozen=True)
class Term:
    """One biasing entry.

    ``rarity`` is 0..1, higher meaning rarer in general English - a proper
    noun scores near 1.0, a common word near 0. ``recency`` is 0..1, higher
    meaning said more recently. ``confusable`` marks a term the transcriber
    has demonstrably got wrong before, which makes it worth its bytes even
    when it is not especially rare.
    """

    text: str
    pack: str
    rarity: float = 0.5
    recency: float = 0.0
    confusable: bool = False
    say_as: str | None = None

    def __post_init__(self) -> None:
        if not str(self.text).strip():
            raise ValueError("term text must not be empty")


@dataclass(frozen=True)
class Pack:
    """A ranked, open-ended source of terms.

    ``floor`` guarantees a minimum number of terms even when the pack ranks
    poorly overall. Without it a large pack (Workspace, hundreds of
    identifiers) starves a small critical one (People) at tight budgets -
    and getting a colleague's name wrong matters more than missing a class
    name.
    """

    name: str
    terms: tuple[Term, ...] = ()
    priority: float = 1.0
    floor: int = 0
    enabled: bool = True

    def ranked(self) -> list[Term]:
        return sorted(self.terms, key=lambda t: -score(t, self))


@dataclass
class CompileResult:
    """Everything the compiler decided, for the payload *and* the audit row."""

    payload: str
    terms: list[Term] = field(default_factory=list)
    dropped: list[tuple[Term, str]] = field(default_factory=list)
    used: int = 0
    capacity: int = 0
    unit: str = Unit.TOKENS
    form: str = Form.TERMS
    rarity_floor: float = 0.0

    @property
    def headroom(self) -> int:
        return max(0, self.capacity - self.used)

    def audit(self) -> dict:
        """Flat, storable record of one compile - the transparency contract."""
        return {
            "payload": self.payload,
            "form": self.form,
            "unit": self.unit,
            "used": self.used,
            "capacity": self.capacity,
            "headroom": self.headroom,
            "rarity_floor": round(self.rarity_floor, 3),
            "included": [
                {"text": t.text, "pack": t.pack, "rarity": round(t.rarity, 3)}
                for t in self.terms
            ],
            "dropped": [
                {"text": t.text, "pack": t.pack, "reason": why}
                for t, why in self.dropped
            ],
        }


def score(term: Term, pack: Pack) -> float:
    """Rank one term. Higher wins a place in the budget.

    Recency dominates because a name said thirty seconds ago is far more
    likely to recur than one from a static glossary; rarity matters because
    biasing a common word wastes budget and can skew decoding; confusability
    is a direct signal from observed failures.
    """
    return (
        term.recency * 2.0
        + term.rarity * 1.5
        + (0.75 if term.confusable else 0.0)
    ) * max(pack.priority, 0.0)


def rarity_floor_for(budget: Budget) -> float:
    """Minimum rarity a term needs to be worth its bytes.

    Scales with the budget: ruthless when tight, generous when roomy. This is
    what stops a 224-token Whisper prompt filling with ordinary words.
    """
    if budget.capacity <= 0:
        return 1.0
    if budget.capacity < 120:
        return 0.65
    if budget.capacity < 300:
        return 0.45
    if budget.capacity < 800:
        return 0.25
    return 0.0


def estimate_tokens(text: str) -> int:
    """Conservative token estimate; real Whisper tokenisation may differ.

    Mirrors ``vocab.estimated_prompt_tokens`` so the two agree - a UI meter
    that disagrees with what actually shipped is worse than no meter.
    """
    if not text:
        return 0
    lexical = len(re.findall(r"[\w]+|[^\w\s]", text, flags=re.UNICODE))
    return max(lexical, math.ceil(len(text.encode("utf-8")) / 3))


def _cost(term: Term, unit: str) -> int:
    if unit == Unit.TOKENS:
        return estimate_tokens(term.text) + 1  # +1 for the separator
    if unit == Unit.WORDS:
        return len(term.text.split())
    return 1


def _fit_term(term: Term, budget: Budget) -> Term:
    """Apply per-term character caps rather than letting a provider truncate."""
    if budget.max_term_chars and len(term.text) > budget.max_term_chars:
        return replace(term, text=term.text[: budget.max_term_chars].rstrip())
    return term


def select(packs: list[Pack], budget: Budget) -> CompileResult:
    """Choose which terms fit, honouring pack floors then global rank."""
    floor_value = rarity_floor_for(budget)
    result = CompileResult(
        payload="", capacity=max(0, budget.capacity),
        unit=budget.unit, rarity_floor=floor_value,
    )
    active = [p for p in packs if p.enabled and p.terms]
    if not active or budget.capacity <= 0:
        for pack in packs:
            for term in pack.terms:
                result.dropped.append((term, "no budget"))
        return result

    chosen: list[Term] = []
    seen: set[str] = set()
    used = 0

    def admit(term: Term, *, ignore_floor: bool = False) -> bool:
        nonlocal used
        key = term.text.casefold()
        if key in seen:
            result.dropped.append((term, "duplicate"))
            return False
        if not ignore_floor and term.rarity < floor_value and not term.confusable:
            result.dropped.append((term, "below rarity floor"))
            return False
        fitted = _fit_term(term, budget)
        cost = _cost(fitted, budget.unit)
        if used + cost > budget.capacity:
            result.dropped.append((term, "over budget"))
            return False
        seen.add(key)
        chosen.append(fitted)
        used += cost
        return True

    # Floors first: a guaranteed slice for small critical packs. Floor terms
    # bypass the rarity gate - if you curated a colleague's name, it ships.
    for pack in sorted(active, key=lambda p: -p.priority):
        for term in pack.ranked()[: max(0, pack.floor)]:
            admit(term, ignore_floor=True)

    # Then the remainder competes globally on rank.
    rest = [
        (score(t, p), t)
        for p in active
        for t in p.terms
        if t.text.casefold() not in seen
    ]
    for _, term in sorted(rest, key=lambda pair: -pair[0]):
        admit(term)

    result.terms = chosen
    result.used = used
    return result


def render(result: CompileResult, budget: Budget) -> CompileResult:
    """Serialise the selected terms into the provider's payload.

    Whisper weights the END of a prompt most heavily, so the highest-ranked
    terms are emitted last. This is the opposite of what reads naturally and
    is easy to regress, hence the explicit reversal here.
    """
    terms = result.terms
    if not terms:
        result.payload = ""
        return result

    if budget.unit == Unit.TERMS:
        result.form = Form.TERMS
        result.payload = ", ".join(t.text for t in terms)
        return result

    ordered = list(reversed(terms))  # best-last: attention favours the tail
    if budget.supports_prose and not budget.is_tight:
        result.form = Form.PROSE
        body = ", ".join(t.text for t in ordered)
        result.payload = f"Terms that may come up: {body}."
    else:
        result.form = Form.TERMS
        result.payload = ", ".join(t.text for t in ordered) + "."
    return result


def compile_packs(packs: list[Pack], budget: Budget) -> CompileResult:
    """Select and serialise in one step. The entry point callers should use."""
    return render(select(packs, budget), budget)
