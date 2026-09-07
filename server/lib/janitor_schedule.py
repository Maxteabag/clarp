"""Janitor local-time schedules. Legacy schedules keep their UTC contract."""
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .scheduler import parse_cron


def compute_next_run(expression: str, timezone_name: str, from_ms: int) -> int | None:
    """Strictly next occurrence; skip gaps and run repeated local minutes once.

    Enumerating local dates and round-tripping fold=0 through UTC avoids both
    nonexistent spring times and duplicate autumn times, including after restart.
    DOM/DOW preserve the existing Host parser's AND semantics.
    """
    minutes, hours, days, months, weekdays = parse_cron(expression)
    try:
        zone = ZoneInfo(timezone_name)
    except (ValueError, ZoneInfoNotFoundError, TypeError) as exc:
        raise ValueError("Choose a valid IANA timezone") from exc
    first_date = datetime.fromtimestamp(from_ms / 1000, zone).date()
    for offset in range(367):
        date = first_date + timedelta(days=offset)
        if date.month not in months or date.day not in days or (date.weekday() + 1) % 7 not in weekdays:
            continue
        for hour in sorted(hours):
            for minute in sorted(minutes):
                local = datetime.combine(date, time(hour, minute), zone).replace(fold=0)
                utc = local.astimezone(timezone.utc)
                if utc.astimezone(zone).replace(tzinfo=None) != local.replace(tzinfo=None):
                    continue
                candidate = int(utc.timestamp() * 1000)
                if candidate > from_ms:
                    return candidate
    return None


def preview_next_runs(expression: str, timezone_name: str, from_ms: int, count: int = 3) -> list[int]:
    result = []
    for _ in range(max(0, min(count, 10))):
        value = compute_next_run(expression, timezone_name, from_ms)
        if value is None:
            break
        result.append(value)
        from_ms = value
    return result
