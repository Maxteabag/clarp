"""Characterization tests for lib.calendar_request (agent calendar-write validation)."""
from __future__ import annotations

import dataclasses

import pytest

from lib.calendar_request import (CalendarRequest, CalendarRequestError,
                                  build_calendar_request)

_FULL = {
    "session": " theo ", "title": "Standup", "start": "2026-09-25T09:00:00+02:00",
    "end": "2026-09-25T09:15:00+02:00", "time_zone": "Europe/Oslo",
    "location": "Room 4", "notes": "bring coffee", "url": "https://x.example",
    "all_day": 1, "calendar": "Work",
}


def test_build_full_request_strips_and_coerces():
    req = build_calendar_request(_FULL, "req-1")
    assert req == CalendarRequest(
        request_id="req-1", session="theo", title="Standup",
        start="2026-09-25T09:00:00+02:00", end="2026-09-25T09:15:00+02:00",
        time_zone="Europe/Oslo", location="Room 4", notes="bring coffee",
        url="https://x.example", all_day=True, calendar="Work")


def test_build_minimal_request_defaults_optionals():
    req = build_calendar_request(
        {"session": "theo", "title": "T", "start": "s", "end": "e"}, "r")
    assert (req.time_zone, req.location, req.notes, req.url, req.calendar) == ("", "", "", "", "")
    assert req.all_day is False


@pytest.mark.parametrize("missing", ["session", "title", "start", "end"])
@pytest.mark.parametrize("value", [None, "", "   "])
def test_required_fields(missing, value):
    data = {**_FULL, missing: value}
    with pytest.raises(CalendarRequestError, match=f"{missing} required"):
        build_calendar_request(data, "r")


def test_required_field_absent_entirely():
    data = dict(_FULL)
    del data["title"]
    with pytest.raises(CalendarRequestError):
        build_calendar_request(data, "r")


def test_error_is_a_value_error():
    assert issubclass(CalendarRequestError, ValueError)


def test_timezone_alias_only_used_when_time_zone_blank():
    data = {**_FULL, "time_zone": "", "timezone": "UTC"}
    assert build_calendar_request(data, "r").time_zone == "UTC"
    data = {**_FULL, "time_zone": "Europe/Oslo", "timezone": "UTC"}
    assert build_calendar_request(data, "r").time_zone == "Europe/Oslo"
    data = {k: v for k, v in _FULL.items() if k != "time_zone"}
    assert build_calendar_request({**data, "timezone": "UTC"}, "r").time_zone == "UTC"


@pytest.mark.parametrize("raw,expected", [
    (True, True), (False, False), (1, True), (0, False), ("yes", True),
    ("", False), ("false", True),   # any non-empty string is truthy
    (None, False),
])
def test_all_day_uses_python_truthiness(raw, expected):
    assert build_calendar_request({**_FULL, "all_day": raw}, "r").all_day is expected


def test_non_string_values_are_stringified():
    req = build_calendar_request({**_FULL, "title": 42, "notes": 3.5}, "r")
    assert req.title == "42" and req.notes == "3.5"


def test_as_event_wire_shape():
    event = build_calendar_request(_FULL, "req-9").as_event("calendar-request")
    assert list(event) == ["type", "request_id", "session", "title", "start", "end",
                           "time_zone", "location", "notes", "url", "all_day", "calendar"]
    assert event["type"] == "calendar-request"
    assert event["request_id"] == "req-9"
    assert event["all_day"] is True
    assert event["session"] == "theo"


def test_request_is_frozen():
    req = build_calendar_request(_FULL, "r")
    with pytest.raises(dataclasses.FrozenInstanceError):
        req.title = "changed"
