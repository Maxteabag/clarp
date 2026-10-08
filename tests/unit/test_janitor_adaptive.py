"""Adaptive Janitor intervals (janitor_adaptive): idle runs double the wait from
the configured base up to the cap, a run that worked returns to the base, a
failed run leaves it alone, and a run that never reported counts as worked."""
from __future__ import annotations

import json

import pytest

from lib import janitor_adaptive as adaptive


def chain(activities, *, base=900, cap=None):
    state, intervals = {}, []
    for activity in activities:
        intervals.append(adaptive.advance(state, activity=activity, base_seconds=base, max_seconds=cap))
    return state, intervals


def test_idle_runs_double_from_fifteen_minutes_to_the_one_day_cap():
    state, intervals = chain(["idle"] * 9)
    assert [i // 60 for i in intervals] == [30, 60, 120, 240, 480, 960, 1440, 1440, 1440]
    assert state["idle_streak"] == 9 and state["last_activity"] == "idle"


def test_work_resets_to_the_base_and_failure_keeps_the_interval():
    state, intervals = chain(["idle", "idle", "failed", "worked", "idle"])
    assert intervals == [1800, 3600, 3600, 900, 1800]
    assert state["idle_streak"] == 1


def test_a_per_janitor_cap_and_a_fixed_interval():
    assert chain(["idle"] * 4, cap=7200)[1] == [1800, 3600, 7200, 7200]
    # 0 (the quota watches' default) or a cap below the base keeps it fixed.
    assert chain(["idle"] * 3, cap=0)[1] == [900, 900, 900]
    assert chain(["idle"] * 3, cap=600)[1] == [900, 900, 900]


def test_reconfiguring_the_base_starts_again_from_it():
    state, _ = chain(["idle"] * 3)
    assert adaptive.advance(state, activity="idle", base_seconds=300, max_seconds=None) == 600
    assert state["idle_streak"] == 1


def test_the_state_survives_a_restart_as_json():
    state, _ = chain(["idle"] * 3)
    restored = json.loads(json.dumps(state))
    assert adaptive.current(restored, base_seconds=900, max_seconds=None) == 7200
    assert adaptive.advance(restored, activity="idle", base_seconds=900, max_seconds=None) == 14400


@pytest.mark.parametrize("run, expected", [
    ({"status": "completed", "activity": "idle", "activity_summary": "Nothing to do"}, ("idle", "Nothing to do", True)),
    ({"status": "completed", "activity": "", "activity_summary": ""}, ("worked", "", False)),
    ({"status": "completed"}, ("worked", "", False)),
    ({"status": "failed", "outcome": "error", "activity": "idle", "error": "boom"}, ("failed", "", True)),
    ({"status": "failed", "outcome": "error", "error": "Runtime failed"}, ("failed", "Runtime failed", False)),
])
def test_a_finished_runs_outcome(run, expected):
    activity, summary, reported = adaptive.outcome(run)
    assert (activity, reported) == (expected[0], expected[2])
    assert summary == expected[1] or expected[0] == "failed"


def test_an_unknown_activity_is_refused():
    with pytest.raises(ValueError):
        adaptive.advance({}, activity="busy", base_seconds=900, max_seconds=None)


def test_cadence_shows_the_current_interval_and_next_run():
    state, _ = chain(["idle", "idle"])
    view = adaptive.cadence(state, base_seconds=900, max_seconds=None, next_run_at=123, lane="schedule:a")
    assert view == {"lane": "schedule:a", "base_interval_seconds": 900, "current_interval_seconds": 3600,
                    "max_interval_seconds": 86400, "adaptive": True, "idle_streak": 2,
                    "last_activity": "idle", "last_summary": "", "next_run_at": 123}
