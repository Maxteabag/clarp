"""Issue 360, R49: a reset time out of calendar range (milliseconds, inf, nan)
raised out of the time normalizer and broke the whole limits record. It now
reads as no reset time; the rest of the record stands."""
from __future__ import annotations

import pytest

from lib import backend_usage_store as store


@pytest.mark.parametrize("value", [1_800_000_000_000, 1.8e15, float("inf"), float("-inf"),
                                   float("nan"), 10 ** 400, "1800000000000", "inf", "nan"])
def test_an_out_of_range_reset_time_reads_as_none(value):
    assert store._normalize_time(value) == ""


def test_an_ordinary_reset_time_still_reads():
    assert store._normalize_time(1_800_000_000) == "2027-01-15T08:00:00Z"
    assert store._normalize_time("1800000000") == "2027-01-15T08:00:00Z"
    assert store._normalize_time("2027-01-15T08:00:00+00:00") == "2027-01-15T08:00:00Z"
