"""Validation helpers for agent-requested calendar writes."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CalendarRequest:
    request_id: str
    session: str
    title: str
    start: str
    end: str
    time_zone: str
    location: str
    notes: str
    url: str
    all_day: bool
    calendar: str

    def as_event(self, event_type: str) -> dict[str, Any]:
        return {
            "type": event_type,
            "request_id": self.request_id,
            "session": self.session,
            "title": self.title,
            "start": self.start,
            "end": self.end,
            "time_zone": self.time_zone,
            "location": self.location,
            "notes": self.notes,
            "url": self.url,
            "all_day": self.all_day,
            "calendar": self.calendar,
        }


class CalendarRequestError(ValueError):
    pass


def build_calendar_request(data: dict[str, Any], request_id: str) -> CalendarRequest:
    session = _required_string(data, "session")
    title = _required_string(data, "title")
    start = _required_string(data, "start")
    end = _required_string(data, "end")
    return CalendarRequest(
        request_id=request_id,
        session=session,
        title=title,
        start=start,
        end=end,
        time_zone=_optional_string(data, "time_zone") or _optional_string(data, "timezone"),
        location=_optional_string(data, "location"),
        notes=_optional_string(data, "notes"),
        url=_optional_string(data, "url"),
        all_day=bool(data.get("all_day", False)),
        calendar=_optional_string(data, "calendar"),
    )


def _required_string(data: dict[str, Any], key: str) -> str:
    value = _optional_string(data, key)
    if not value:
        raise CalendarRequestError(f"{key} required")
    return value


def _optional_string(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if value is None:
        return ""
    return str(value).strip()
