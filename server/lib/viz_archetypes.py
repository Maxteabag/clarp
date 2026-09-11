"""How a thing on the fleet map behaves, independent of what it is.

A GitHub Action is not novel logic -- it is a *process*: it starts, runs for
some time, and ends well or badly. Almost everything an agent does falls into
one of a handful of such patterns. Naming them buys two things:

* the renderer needs five behaviours, not one per tool;
* an authoring model can classify something it has never seen by *picking a
  pattern and supplying parameters*, instead of emitting free-form drawing
  code that runs in the render loop.

New patterns are added here deliberately, so the library converges: over time
the system needs less generation, not more.
"""
from __future__ import annotations

from typing import Any

# --- the library ----------------------------------------------------------
PULSE = "pulse"              # a point event: flash and gone
PLACE = "place"              # a durable location: persists, gains weight
PROCESS = "process"          # has duration and an outcome
CHANNEL = "channel"          # a link between two actors
ACCUMULATOR = "accumulator"  # grows as it is added to

ARCHETYPES = (PULSE, PLACE, PROCESS, CHANNEL, ACCUMULATOR)

# Every verb the rule table can emit maps to exactly one archetype. A verb
# absent here renders as PULSE, which is why an unclassified tool is still
# visible rather than silently dropped.
VERB_ARCHETYPE: dict[str, str] = {
    "push": PULSE, "spawn": PULSE, "media": PULSE,
    "read": PULSE, "search": PULSE, "query": PULSE, "util": PULSE,
    "write": ACCUMULATOR, "vcs": ACCUMULATOR,
    "build": PROCESS, "test": PROCESS, "github": PROCESS,
    "review": PROCESS, "execute": PROCESS, "ops": PROCESS,
    "message": CHANNEL,
    "network": PULSE, "cloud": PROCESS, "remote": PROCESS,
    "clarp": PULSE, "skill": PULSE, "plan": PULSE,
}

# Presentation parameters per archetype. The renderer reads these rather than
# hard-coding behaviour, so a generated archetype assignment changes the
# drawing without shipping new client code.
SPEC: dict[str, dict[str, Any]] = {
    PULSE:       {"travel": 0.035, "decay": 0.94, "persist": False,
                  "weight": 1.0, "trail": 0.0},
    PLACE:       {"travel": 0.030, "decay": 0.97, "persist": True,
                  "weight": 1.6, "trail": 0.0},
    PROCESS:     {"travel": 0.018, "decay": 0.985, "persist": True,
                  "weight": 1.3, "trail": 0.35},
    CHANNEL:     {"travel": 0.045, "decay": 0.90, "persist": False,
                  "weight": 1.0, "trail": 0.6},
    ACCUMULATOR: {"travel": 0.030, "decay": 0.95, "persist": True,
                  "weight": 1.2, "trail": 0.0},
}


def archetype_for(verb: str) -> str:
    """Which pattern a verb draws as. Unknown verbs stay visible as pulses."""
    return VERB_ARCHETYPE.get(verb, PULSE)


def specs() -> dict[str, dict[str, Any]]:
    """Presentation table, sent to the client so behaviour is data not code."""
    return {name: dict(SPEC[name]) for name in ARCHETYPES}


def validate_assignment(verb: str, archetype: str) -> tuple[bool, str]:
    """Gate for an authored (verb -> archetype) proposal.

    Compatibility gate for callers using the built-in library. Autonomous
    tier-two designs use viz_library.validate_design instead, which permits
    new semantic archetypes with bounded specs and sandboxed drawing logic.
    """
    if not verb or not verb.replace("_", "").isalnum():
        return False, f"verb {verb!r} is not a plain identifier"
    if archetype not in ARCHETYPES:
        return False, (f"archetype {archetype!r} is not in the library "
                       f"({', '.join(ARCHETYPES)})")
    return True, ""
