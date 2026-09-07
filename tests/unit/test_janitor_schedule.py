from datetime import datetime

import pytest

from lib.janitor_schedule import compute_next_run, preview_next_runs


def ms(value):
    return int(datetime.fromisoformat(value).timestamp() * 1000)


def test_ordinary_local_schedule_preview_is_strict_and_ordered():
    start = ms("2026-09-04T08:30:00+02:00")
    assert preview_next_runs("30 8 * * 1-5", "Europe/Oslo", start) == [
        ms("2026-09-07T08:30:00+02:00"), ms("2026-09-08T08:30:00+02:00"),
        ms("2026-09-09T08:30:00+02:00")]


def test_nonexistent_spring_occurrence_is_skipped():
    assert compute_next_run("30 2 * * *", "Europe/Oslo", ms("2026-03-28T03:00:00+01:00")) == ms("2026-03-30T02:30:00+02:00")


def test_fall_repeated_hour_occurs_only_on_first_fold_even_after_restart():
    first = ms("2026-10-25T02:30:00+02:00")
    assert compute_next_run("30 2 * * *", "Europe/Oslo", ms("2026-10-25T01:30:00+02:00")) == first
    tomorrow = ms("2026-10-26T02:30:00+01:00")
    assert compute_next_run("30 2 * * *", "Europe/Oslo", first) == tomorrow
    assert compute_next_run("30 2 * * *", "Europe/Oslo", ms("2026-10-25T02:10:00+01:00")) == tomorrow


def test_fall_minutely_schedule_skips_second_fold():
    assert compute_next_run("* * * * *", "Europe/Oslo", ms("2026-10-25T02:59:00+02:00")) == ms("2026-10-25T03:00:00+01:00")


@pytest.mark.parametrize("cron,zone", [("broken", "UTC"), ("0 99 * * *", "UTC"), ("* * * * *", "Not/AZone")])
def test_invalid_schedule_rejected(cron, zone):
    with pytest.raises(ValueError):
        compute_next_run(cron, zone, 0)


def test_impossible_date_has_bounded_search():
    assert compute_next_run("0 0 31 2 *", "UTC", ms("2026-01-01T00:00:00+00:00")) is None
