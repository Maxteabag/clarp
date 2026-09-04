"""Where a night's ideas come from.

A dream used to have exactly one source of subject matter: a snapshot of the
agent's own recent chat, plus "generate three directions". That reliably
produced ideas about whatever the user happened to be doing at dinner time.

This module makes the source an explicit, recorded variable. A run picks a
seeding strategy and a context dose, both stored on `dream_runs`, so two
nights can be compared rather than merely read.

Nothing here decides quality. It only decides what the seed round is looking
at when it starts thinking.
"""
from __future__ import annotations

import json
import random
import re
import urllib.error
import urllib.request
from typing import Any

from .log import log, log_exception

# --- recipe axes -----------------------------------------------------------

CONTROL = "control"
LENSES = "lenses"
FOREIGN = "foreign"
ROLEPLAY = "roleplay"
STRATEGIES = (CONTROL, LENSES, FOREIGN, ROLEPLAY)

DOSE_NONE = "none"
DOSE_FRAGMENTS = "fragments"
DOSE_FULL = "full"
CONTEXT_DOSES = (DOSE_NONE, DOSE_FRAGMENTS, DOSE_FULL)

# The control arm keeps the full session snapshot, because that *is* the
# behaviour being used as the baseline. Everything else varies the dose so a
# strategy is never silently confounded with how much context it received.
_DOSE_WEIGHTS = {
    CONTROL: ((DOSE_FULL,), (1.0,)),
    LENSES: ((DOSE_FULL, DOSE_FRAGMENTS), (0.5, 0.5)),
    FOREIGN: ((DOSE_FRAGMENTS, DOSE_NONE), (0.7, 0.3)),
    ROLEPLAY: ((DOSE_NONE, DOSE_FRAGMENTS), (0.8, 0.2)),
}


def choose_strategy(rng: random.Random | None = None) -> str:
    return (rng or random).choice(list(STRATEGIES))


def choose_dose(strategy: str, rng: random.Random | None = None) -> str:
    options, weights = _DOSE_WEIGHTS.get(strategy, ((DOSE_FULL,), (1.0,)))
    return (rng or random).choices(list(options), weights=list(weights))[0]


# --- the static lens bank --------------------------------------------------

LENS_BANK = (
    "Considering every bug found in this project recently, is there one "
    "structural cause underneath them?",
    "What axiom of this system has never been examined? What breaks if it is "
    "wrong?",
    "Which two components are secretly coupled in a way nobody has named?",
    "What would you refactor if you knew nobody would object?",
    "What did we build that nobody uses? Why did that happen?",
    "Where is the code lying — where do the docs, names, or prompts describe "
    "something the implementation does not actually do?",
    "What is the most embarrassing thing a stranger would notice in their "
    "first ten minutes in this repository?",
    "Which recent decision would we reverse if we were being honest?",
    "What is this project's single largest source of accidental complexity, "
    "and what would removing it cost?",
    "What does this system make hard that it should make easy?",
    "If this project had to run on one tenth of the code, what survives?",
    "What failure is this system currently one unlucky event away from?",
)

# --- personas for the blind role-play arm ---------------------------------
# Weighted toward hands-busy, non-desk lives: this product is voice-first, so
# the interesting friction is least likely to come from someone at a keyboard.

ROLEPLAY_PERSONAS = (
    "a nurse working a twelve-hour hospital shift",
    "a field service technician repairing equipment at customer sites",
    "a long-haul truck driver on a multi-day route",
    "a parent running a household with two young children",
    "a software developer with severe RSI who cannot type for long",
    "a sysadmin who gets paged at 3am several nights a month",
    "a blind developer who works entirely through audio",
    "an independent consultant billing hours across five clients",
    "a warehouse worker who wears gloves all day",
    "a PhD researcher running experiments that span months",
    "a chef running a restaurant kitchen during service",
    "a midwife doing home visits across a rural county",
    "a commercial fisherman with intermittent connectivity",
    "a high school teacher managing 150 students",
)

# --- offline cross-domain pool --------------------------------------------
# The foreign arm prefers a live random article, but a dream must not fail
# because the network did. These are deliberately unrelated to software.

_OFFLINE_FOREIGN = (
    "Termite mound ventilation: passive airflow regulated by structure alone",
    "The Antikythera mechanism: analogue computation with fixed gearing",
    "Sourdough starter maintenance: a living culture kept by daily feeding",
    "Lighthouse keeping: solitary operation with a legally mandated log",
    "Coral reef bleaching: gradual collapse from a slow parameter drift",
    "Japanese kintsugi: repair that deliberately advertises the break",
    "Air traffic control handoff protocol between adjacent sectors",
    "Beekeeping: reading a hive's health without opening it",
    "The Dewey Decimal system: hierarchical classification under growth",
    "Ship ballast management: stability by deliberately carrying dead weight",
    "Cave diving line protocol: never losing your route out",
    "Traditional Polynesian wayfinding without instruments",
    "Cathedral construction spanning multiple human lifetimes",
    "Blood typing and the logistics of a transfusion supply chain",
    "Avalanche forecasting from layered snowpack history",
    "The Voyager probes' golden record: designing for unknown readers",
    "Mycorrhizal networks: trees trading nutrients through fungi",
    "Semaphore telegraph lines before electrical signalling",
)

_CONSTRAINTS = (
    "What if this system had no screen at all?",
    "What if the user only ever had one physical button?",
    "What if every operation had to survive a 30-second network outage?",
    "What if you could only ship one feature next month?",
    "What if the user were a stranger with no context and no patience?",
    "What if this had to work for someone who cannot read?",
    "What if latency were ten times worse?",
    "What if the whole thing had to be explainable in one sentence?",
)

_STOPWORDS = frozenset("""
a an and are as at be been but by can could did do does for from had has have
he her him his how i if in into is it its just like me my no not of on or our
out over she should so some than that the their them then there these they
this to too up was we were what when where which who why will with would you
your it's don't i'm we're that's here now got get make made really thing
things want need know think see look going go one two three okay yeah
""".split())


def random_foreign_material(rng: random.Random | None = None) -> str:
    """One piece of genuinely unrelated material to collide with the project."""
    rng = rng or random
    article = _random_wikipedia_summary()
    if not article:
        article = rng.choice(list(_OFFLINE_FOREIGN))
        log("dreamForeignOffline", "using offline pool; wikipedia unavailable")
    constraint = rng.choice(list(_CONSTRAINTS))
    return f"UNRELATED SUBJECT: {article}\n\nARBITRARY CONSTRAINT: {constraint}"


def _random_wikipedia_summary(timeout: float = 6.0) -> str:
    url = "https://en.wikipedia.org/api/rest_v1/page/random/summary"
    request = urllib.request.Request(
        url, headers={"User-Agent": "clarp-dreaming/1.0 (self-hosted)"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as e:
        log_exception("dreamForeignFetchFail", e)
        return ""
    title = str(payload.get("title") or "").strip()
    extract = " ".join(str(payload.get("extract") or "").split())
    if not title:
        return ""
    return f"{title} — {extract[:900]}" if extract else title


def session_keywords(snapshot: str, limit: int = 12,
                     rng: random.Random | None = None) -> list[str]:
    """Distinctive terms from the session, for collision rather than context.

    Deliberately crude. The point is a handful of words that smell like this
    project, not an accurate summary — an accurate summary would just
    reproduce the anchoring the foreign arm exists to escape.
    """
    words = re.findall(r"[A-Za-z][A-Za-z0-9_\-]{3,}", snapshot or "")
    counts: dict[str, int] = {}
    for word in words:
        lowered = word.lower()
        if lowered in _STOPWORDS or len(lowered) < 4:
            continue
        counts[lowered] = counts.get(lowered, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    pool = [word for word, _ in ranked[:60]]
    if len(pool) <= limit:
        return pool
    return (rng or random).sample(pool, limit)
