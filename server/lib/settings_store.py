"""Durable server-local settings."""
from __future__ import annotations

from .db import conn, now_ms


def get_bool(key: str, *, default: bool = False) -> bool:
    return get_text(key, default="true" if default else "false").strip().lower() in {
        "1", "true", "yes", "on"
    }


def set_bool(key: str, value: bool) -> None:
    set_text(key, "true" if value else "false")


def get_int(key: str, *, default: int = 0, minimum: int | None = None,
            maximum: int | None = None) -> int:
    raw = get_text(key, default=str(default)).strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = int(default)
    if minimum is not None:
        value = max(int(minimum), value)
    if maximum is not None:
        value = min(int(maximum), value)
    return value


def set_int(key: str, value: int) -> None:
    set_text(key, str(int(value)))


def get_text(key: str, *, default: str = "") -> str:
    row = conn().execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return str(row["value"]) if row is not None else default


def set_text(key: str, value: str) -> None:
    conn().execute(
        """INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
           ON CONFLICT(key) DO UPDATE SET
               value = excluded.value,
               updated_at = excluded.updated_at""",
        (key, str(value), now_ms()),
    )
