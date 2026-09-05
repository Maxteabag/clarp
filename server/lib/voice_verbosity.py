"""How much an agent narrates aloud while it is still working.

The default spoken contract is deliberately quiet: one acknowledgment, silence
while working, one final summary. That is right when you are driving and wrong
when you want to follow along, so each agent carries its own level.

Levels are discrete rather than a continuous 0..1 because the setting resolves
to prompt text, and prose does not interpolate. The client renders them as a
low-to-high slider; only the endpoints and the steps between them are real.

Level 0 reproduces the previous behaviour exactly, so existing agents are
unchanged until someone moves the slider.
"""
from __future__ import annotations

QUIET = 0
MILESTONES = 1
STEPS = 2
RUNNING = 3

MIN_LEVEL = QUIET
MAX_LEVEL = RUNNING
DEFAULT_LEVEL = QUIET

LABELS: dict[int, str] = {
    QUIET: "Quiet",
    MILESTONES: "Milestones",
    STEPS: "Steps",
    RUNNING: "Running commentary",
}

# Appended to the spoken-turn instruction block. Level 0 contributes nothing;
# the base instruction already says to stay silent, and repeating it only
# dilutes it.
_CLAUSES: dict[int, str] = {
    QUIET: "",
    MILESTONES: (
        "Narration level: milestones. Besides the opening acknowledgment and "
        "the final summary, speak one short <speak> line when you move between "
        "major phases of the work — not for individual tool calls."
    ),
    STEPS: (
        "Narration level: steps. Speak one short <speak> line before each "
        "significant action, saying what you are about to do and why. Keep "
        "each to a sentence; skip trivial or repeated steps."
    ),
    RUNNING: (
        "Narration level: running commentary. Talk through the work as you go, "
        "the way you would to someone sitting beside you — what you are "
        "looking at, what you found, what it means, what you will try next. "
        "Stay in short <speak> lines so each is spoken while you continue."
    ),
}


def clamp(value: object) -> int:
    """Coerce anything a client sends into a level this server understands."""
    try:
        level = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_LEVEL
    return max(MIN_LEVEL, min(MAX_LEVEL, level))


def narration_clause(level: object) -> str:
    """The instruction fragment for `level`, or "" when the agent stays quiet."""
    return _CLAUSES[clamp(level)]


def options() -> list[dict[str, object]]:
    """Level catalogue for the settings UI, so labels live on the server."""
    return [
        {"level": level, "label": LABELS[level]}
        for level in range(MIN_LEVEL, MAX_LEVEL + 1)
    ]
