"""Moved out of server/lib/dreaming.py by tools/refactor/split_apply.py (split 'dreaming', recipe tools/refactor/splits/s9-dreaming.toml).
dreaming.py imports these names back. Generated: change the recipe, not this file."""

from __future__ import annotations

from typing import Any
from . import db
from .dreaming_settings import DREAM_DIRECTION_COUNT, DREAM_DIRECTION_MAX, DREAM_DIRECTION_MIN, DREAM_TARGET_TOKEN_BUDGET, DREAM_TOKEN_BUDGET_MAX, DREAM_TOKEN_BUDGET_MIN, DreamingSettings, _clamp_int, get_settings


def get_run(run_id: str) -> dict[str, Any] | None:
    row = db.conn().execute(
        "SELECT * FROM dream_runs WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    return dict(row) if row else None


def _settings_for_run(run_id: str) -> DreamingSettings:
    run = get_run(run_id) or {}
    return DreamingSettings(
        dreams_per_night=get_settings().dreams_per_night,
        direction_count=_clamp_int(
            run.get("planned_directions"),
            default=DREAM_DIRECTION_COUNT,
            minimum=DREAM_DIRECTION_MIN,
            maximum=DREAM_DIRECTION_MAX,
        ),
        target_token_budget=_clamp_int(
            run.get("target_tokens"),
            default=DREAM_TARGET_TOKEN_BUDGET,
            minimum=DREAM_TOKEN_BUDGET_MIN,
            maximum=DREAM_TOKEN_BUDGET_MAX,
        ),
    )
