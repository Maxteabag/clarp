"""Characterization tests for lib.clock (leaf wall-clock helpers)."""
from __future__ import annotations

import pytest

from lib import clock


def test_now_ms_is_integer_epoch_milliseconds(monkeypatch):
    monkeypatch.setattr(clock.time, "time", lambda: 1_700_000_000.9876)
    assert clock.now_ms() == 1_700_000_000_987


@pytest.mark.parametrize("ts_ms,expected", [
    (0, "1970-01-01T00:00:00.000Z"),
    (1, "1970-01-01T00:00:00.001Z"),
    (999, "1970-01-01T00:00:00.999Z"),
    (1_700_000_000_000, "2023-11-14T22:13:20.000Z"),
    (1_700_000_000_123, "2023-11-14T22:13:20.123Z"),
])
def test_iso_from_ms(ts_ms, expected):
    assert clock.iso_from_ms(ts_ms) == expected


def test_iso_from_ms_round_trips_now_ms():
    import datetime as dt
    ts = clock.now_ms()
    parsed = dt.datetime.strptime(clock.iso_from_ms(ts), "%Y-%m-%dT%H:%M:%S.%fZ")
    assert int(parsed.replace(tzinfo=dt.timezone.utc).timestamp() * 1000) == ts


def test_db_re_exports_the_same_now_ms():
    from lib import db
    assert db.now_ms is clock.now_ms
