"""Moved out of server/lib/dreaming.py by tools/refactor/split_apply.py (split 'dreaming', recipe tools/refactor/splits/s9-dreaming.toml).
dreaming.py imports these names back. Generated: change the recipe, not this file."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from . import backends
from . import settings_store


DREAMING_OK = "DREAMING_OK"
DREAM_HIDDEN_PREFIX = "[[CLARP_DREAMING_RUN"
DREAM_TARGET_HOUR = 3
DREAM_WINDOW_MINUTES = 60
DREAM_POLL_INTERVAL_SEC = 5 * 60
DREAM_ACK_MAX_CHARS = 300

DREAMS_PER_NIGHT_DEFAULT = 1
DREAMS_PER_NIGHT_MIN = 1
DREAMS_PER_NIGHT_MAX = 5
DREAM_DIRECTION_MIN = 2
DREAM_DIRECTION_MAX = 8
DREAM_TOKEN_BUDGET_MIN = 30_000
DREAM_TOKEN_BUDGET_MAX = 300_000

DREAM_MIN_DIRECTIONS = 3
DREAM_DIRECTION_COUNT = 3
DREAM_ITERATION_THREAD_COUNT = 1
DREAM_PLANNED_ROUNDS = (
    1 + DREAM_DIRECTION_COUNT + DREAM_ITERATION_THREAD_COUNT + 1 + 1
)
DREAM_TARGET_TOKEN_BUDGET = 70_000
DREAM_TARGET_MINUTES = 120
DREAM_SENT_RECOVERY_SEC = 90 * 60
# A run still active this long after it started is ended, so a round that
# keeps failing cannot hold the agent's nightly dream for ever (AUT-06).
DREAM_RUN_DEADLINE_SEC = 12 * 60 * 60
_DEADLINE_REASON = f"dream run passed its {DREAM_RUN_DEADLINE_SEC // 3600} h deadline"
DREAM_PRIOR_CONTEXT_MAX_CHARS = 7_000
DREAM_PRIOR_ROUND_EXCERPT_CHARS = 1_000

_STAGE_TARGET_TOKENS = {
    "roleplay_day": 8_000,
    "seed": 10_000,
    "fanout": 10_000,
    "iterate": 12_000,
    "completeness": 6_000,
    "synthesize": 8_000,
}

KEY_DREAMS_PER_NIGHT = "dreaming.dreams_per_night"
KEY_DIRECTION_COUNT = "dreaming.direction_count"
KEY_TOKEN_BUDGET = "dreaming.target_token_budget"
# Dreaming runs on its own provider. A user may want a cheap, chatty model for
# the live conversation and a slow expensive one for the night, or the reverse;
# inheriting the agent's backend made that impossible to express.
KEY_DREAM_BACKEND = "dreaming.backend"
KEY_DREAM_MODEL = "dreaming.model"
KEY_DREAM_EFFORT = "dreaming.effort"


@dataclass(frozen=True)
class DreamingSettings:
    dreams_per_night: int = DREAMS_PER_NIGHT_DEFAULT
    direction_count: int = DREAM_DIRECTION_COUNT
    target_token_budget: int = DREAM_TARGET_TOKEN_BUDGET
    # Empty means "inherit the agent's own backend/model/effort".
    backend: str = ""
    model: str = ""
    effort: str = ""

    @property
    def min_directions(self) -> int:
        return self.direction_count

    @property
    def planned_rounds(self) -> int:
        return 1 + self.direction_count + DREAM_ITERATION_THREAD_COUNT + 1 + 1

    @property
    def target_minutes(self) -> int:
        return DREAM_TARGET_MINUTES

    def stage_target_tokens(self) -> dict[str, int]:
        scale = self.target_token_budget / DREAM_TARGET_TOKEN_BUDGET
        return {
            stage: max(1_000, int(round(tokens * scale)))
            for stage, tokens in _STAGE_TARGET_TOKENS.items()
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "dreams_per_night": self.dreams_per_night,
            "direction_count": self.direction_count,
            "planned_directions": self.direction_count,
            "min_directions": self.min_directions,
            "planned_rounds": self.planned_rounds,
            "target_token_budget": self.target_token_budget,
            "target_tokens": self.target_token_budget,
            "target_minutes": self.target_minutes,
            "target_hour": DREAM_TARGET_HOUR,
            "window_minutes": DREAM_WINDOW_MINUTES,
            "backend": self.backend,
            "model": self.model,
            "effort": self.effort,
        }


def _clamp_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def get_settings() -> DreamingSettings:
    return DreamingSettings(
        dreams_per_night=settings_store.get_int(
            KEY_DREAMS_PER_NIGHT,
            default=DREAMS_PER_NIGHT_DEFAULT,
            minimum=DREAMS_PER_NIGHT_MIN,
            maximum=DREAMS_PER_NIGHT_MAX,
        ),
        direction_count=settings_store.get_int(
            KEY_DIRECTION_COUNT,
            default=DREAM_DIRECTION_COUNT,
            minimum=DREAM_DIRECTION_MIN,
            maximum=DREAM_DIRECTION_MAX,
        ),
        target_token_budget=settings_store.get_int(
            KEY_TOKEN_BUDGET,
            default=DREAM_TARGET_TOKEN_BUDGET,
            minimum=DREAM_TOKEN_BUDGET_MIN,
            maximum=DREAM_TOKEN_BUDGET_MAX,
        ),
        backend=settings_store.get_text(KEY_DREAM_BACKEND, default=""),
        model=settings_store.get_text(KEY_DREAM_MODEL, default=""),
        effort=settings_store.get_text(KEY_DREAM_EFFORT, default=""),
    )


def update_settings(data: dict[str, Any]) -> DreamingSettings:
    current = get_settings()
    if "dreams_per_night" in data:
        settings_store.set_int(
            KEY_DREAMS_PER_NIGHT,
            _clamp_int(
                data.get("dreams_per_night"),
                default=current.dreams_per_night,
                minimum=DREAMS_PER_NIGHT_MIN,
                maximum=DREAMS_PER_NIGHT_MAX,
            ),
        )
    if "direction_count" in data:
        settings_store.set_int(
            KEY_DIRECTION_COUNT,
            _clamp_int(
                data.get("direction_count"),
                default=current.direction_count,
                minimum=DREAM_DIRECTION_MIN,
                maximum=DREAM_DIRECTION_MAX,
            ),
        )
    if "target_token_budget" in data:
        settings_store.set_int(
            KEY_TOKEN_BUDGET,
            _clamp_int(
                data.get("target_token_budget"),
                default=current.target_token_budget,
                minimum=DREAM_TOKEN_BUDGET_MIN,
                maximum=DREAM_TOKEN_BUDGET_MAX,
            ),
        )
    if "backend" in data:
        raw = str(data.get("backend") or "").strip()
        # Empty clears the override; anything else must name a real adapter,
        # otherwise a typo would silently strand every dream on Claude.
        settings_store.set_text(
            KEY_DREAM_BACKEND, backends.normalize(raw) if raw else "")
    if "model" in data:
        settings_store.set_text(
            KEY_DREAM_MODEL, str(data.get("model") or "").strip()[:120])
    if "effort" in data:
        raw_effort = str(data.get("effort") or "").strip()
        target = str(data.get("backend") or get_settings().backend or "").strip()
        settings_store.set_text(
            KEY_DREAM_EFFORT,
            backends.clean_effort(backends.normalize(target), raw_effort)
            if raw_effort else "")
    return get_settings()


def dreaming_prompt_text(settings: DreamingSettings | None = None) -> str:
    settings = settings or get_settings()
    return (
        "Deep Dreaming contract: run a grounded overnight investigation, not a "
        "single prompt and not pure ideation by default. First check current "
        "code/state and anti-promote already-fixed ideas, then seed exactly "
        f"{settings.direction_count} candidate directions, fan out into "
        "separate hidden sub-turns per direction, iterate on the strongest "
        "thread, run a missed-angle completeness check, then produce one "
        "visible Dream Digest. Output altitude is a spectrum: idea-only, "
        "verified finding, disposable worktree, or PR proposal when confidence "
        "is high. Guardrails: isolated context only; no shared-tree edits, "
        "deploys, service restarts, auto-merges, destructive data changes, "
        "spending, or external messages. "
        f"If there is genuinely nothing useful, the seed round replies {DREAMING_OK} "
        "and the run is suppressed. "
        f"Budget target: {settings.planned_rounds} rounds, "
        f"{settings.target_token_budget} target tokens, "
        f"{settings.target_minutes} minutes."
    )


DREAMING_PROMPT = dreaming_prompt_text(DreamingSettings())


_TOKEN_EDGE_RE = re.compile(
    rf"(?:^{re.escape(DREAMING_OK)}\b.*$|^.*\b{re.escape(DREAMING_OK)}\W*$)",
    re.S,
)
_STAGE_OUTPUT_RE = re.compile(
    r"DREAM_STAGE_OUTPUT\s+run_id=(?P<run_id>dream_[A-Za-z0-9_]+)\s+"
    r"round_id=(?P<round_id>dround_[A-Za-z0-9_]+)\s+"
    r"stage=(?P<stage>[A-Z_]+)",
    re.I,
)
_DIGEST_DONE_RE = re.compile(
    r"\s*DREAM_DIGEST_DONE\s+run_id=(?P<run_id>dream_[A-Za-z0-9_]+)\s+"
    r"round_id=(?P<round_id>dround_[A-Za-z0-9_]+)\s*$",
    re.I,
)
# Markdown decoration a model puts in front of a line it considers a heading:
# blockquote, ATX heading, list bullet, then emphasis. Every one of these was
# rejected by the original pattern, so a perfectly-formed `### D1 [new]: ...`
# slate parsed as zero directions and the run branched on fallback prose.
_MD_PREFIX = r"[ \t]*(?:>[ \t]*)*(?:#{1,6}[ \t]*)?(?:[-*+][ \t]+)?[*_`~]{0,3}[ \t]*"

_SLATE_LINE_RE = re.compile(
    rf"^{_MD_PREFIX}(?:D|Direction)[ \t]*(?P<idx>\d+)"
    r"[ \t]*(?P<tag>\[[^\]]+\])?[*_`]{0,3}[ \t]*[:.)-][*_`~ \t]*"
    r"(?P<title>.+?)\s*$",
    re.I | re.M,
)
_FALLBACK_LINE_RE = re.compile(
    r"^[ \t]*(?:#{1,6}[ \t]+|[-*+][ \t]+|\d+[.)][ \t]+|\*\*)"
    r"(?P<title>.+?)\s*$",
    re.M,
)
_RUN_ID_RE = re.compile(r"run_id=(dream_[A-Za-z0-9_]+)")
_EVIDENCE_STATUS_RE = re.compile(
    rf"^{_MD_PREFIX}(?:Evidence status|Status)[*_`]{{0,2}}[ \t]*:[*_ \t]*"
    r"(confirmed|refuted|speculative)\b",
    re.I | re.M,
)
_ALTITUDE_RE = re.compile(
    rf"^{_MD_PREFIX}Altitude[*_`]{{0,2}}[ \t]*:[*_ \t]*"
    r"(idea|brainstorm|verified(?: finding)?|worktree|pr|pull request)\b",
    re.I | re.M,
)
_ARTIFACT_RE = re.compile(
    rf"^{_MD_PREFIX}(?:Artifact|Worktree|PR)[*_`]{{0,2}}[ \t]*:[*_ \t]*"
    r"(?P<value>.+?)\s*$",
    re.I | re.M,
)
_EVIDENCE_SUMMARY_RE = re.compile(
    rf"^{_MD_PREFIX}Evidence summary[*_`]{{0,2}}[ \t]*:[*_ \t]*"
    r"(?P<value>.+?)\s*$",
    re.I | re.M,
)
_GUARDRAIL_REFUSAL_RE = re.compile(
    rf"^{_MD_PREFIX}(?:Guardrail refused|Refused forbidden op)[*_`]{{0,2}}"
    r"[ \t]*:[*_ \t]*(?P<value>.+?)\s*$",
    re.I | re.M,
)
