"""Table-driven decisions for the Claude account failover plan."""
import pytest

from lib.policies.failover import (
    FAILED, NOT_CHECKED, Check, FailoverPlan, FailoverSettings, Kill, Park,
    PendingWork, Release, Resume)

SETTINGS = FailoverSettings(recheck_seconds=60.0, max_park_seconds=1800.0)
STOPPED = PendingWork("sonnet", stopped=True)
RUNNING = PendingWork("sonnet", stopped=False)


def plan(**kw):
    return FailoverPlan(parked_since=100.0, **kw)


@pytest.mark.parametrize("verdict", [NOT_CHECKED, True, False, None, FAILED])
def test_nothing_owned_ends_recovery_in_every_phase(verdict):
    assert plan().decide([], verdict, 100.0, SETTINGS) == Release("no-owned-work")


def test_live_owned_work_is_killed_before_any_check():
    assert plan().decide([STOPPED, RUNNING], NOT_CHECKED, 100.0, SETTINGS) == Kill(
        "owned-work-still-running")


@pytest.mark.parametrize("next_check,now,expected", [
    (0.0, 100.0, Check(("sonnet",))),
    (100.0, 100.0, Check(("sonnet",))),           # due exactly now
    (160.0, 100.0, Park("recheck-not-due", 60.0)),
    (160.0, 159.5, Park("recheck-not-due", 0.5)),
])
def test_check_runs_only_when_due(next_check, now, expected):
    assert plan(next_check=next_check).decide([STOPPED], NOT_CHECKED, now, SETTINGS) == expected


def test_check_covers_every_pending_model_once_sorted():
    pending = [PendingWork("sonnet", True), PendingWork("opus", True), PendingWork("sonnet", True)]
    assert plan().decide(pending, NOT_CHECKED, 100.0, SETTINGS) == Check(("opus", "sonnet"))


def test_verified_account_resumes_when_every_model_was_checked():
    checked = plan(checked_models=("sonnet",))
    assert checked.decide([STOPPED], True, 100.0, SETTINGS) == Resume("account-verified")


def test_unchecked_model_parks_for_another_complete_check():
    checked = plan(checked_models=("sonnet",))
    pending = [STOPPED, PendingWork("opus", True)]
    assert checked.decide(pending, True, 100.0, SETTINGS) == Park("unchecked-model", 60.0)


@pytest.mark.parametrize("verdict,reason", [
    (False, "no-account"),
    (None, "inconclusive"),
    (FAILED, "recovery-failed"),
])
def test_no_verified_account_parks_inside_the_cap(verdict, reason):
    assert plan().decide([STOPPED], verdict, 100.0, SETTINGS) == Park(reason, 60.0)
    assert plan().decide([STOPPED], verdict, 100.0 + 1799.0, SETTINGS) == Park(reason, 60.0)


@pytest.mark.parametrize("verdict", [False, None, FAILED])
def test_park_expires_at_the_cap_and_releases_the_work(verdict):
    assert plan().decide([STOPPED], verdict, 1900.0, SETTINGS) == Release("park-expired")


def test_unchecked_model_also_expires_rather_than_parking_for_ever():
    checked = plan(checked_models=("sonnet",))
    pending = [STOPPED, PendingWork("opus", True)]
    assert checked.decide(pending, True, 1900.0, SETTINGS) == Release("park-expired")


def test_a_verified_account_resumes_even_past_the_cap():
    checked = plan(checked_models=("sonnet",))
    assert checked.decide([STOPPED], True, 1900.0, SETTINGS) == Resume("account-verified")


def test_sentinels_are_distinct_from_selector_verdicts():
    assert NOT_CHECKED is not FAILED
    for value in (True, False, None):
        assert NOT_CHECKED != value and FAILED != value
    assert repr(NOT_CHECKED) == "NOT_CHECKED" and repr(FAILED) == "FAILED"
