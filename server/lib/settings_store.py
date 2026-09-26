"""Durable server-local settings: the only writer of the `settings` table.

Other modules keep their own key prefixes (desktop presence leases, activity
leases, the Oracle contact) but never touch the table themselves; they call
the functions below. `transaction()` lets a caller make a read-check-write
sequence atomic on this thread's connection.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

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


def get(key: str) -> str | None:
    """The stored value, or None when the key is absent (unlike get_text)."""
    row = conn().execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return str(row["value"]) if row is not None else None


def get_text(key: str, *, default: str = "") -> str:
    value = get(key)
    return default if value is None else value


def set_text(key: str, value: str, *, updated_at: int | None = None) -> None:
    """Upsert `key`. `updated_at` defaults to now; lease writers pass the
    clock they validated the report against so pruning and reads agree.

    An unchanged value is not rewritten: the upsert would take the database
    write lock only to store what is already there. A caller that passes
    `updated_at` is refreshing a lease, so only an identical stamp is skipped.
    """
    value = str(value)
    row = conn().execute("SELECT value, updated_at FROM settings WHERE key = ?", (key,)).fetchone()
    if row is not None and str(row["value"]) == value and (
            updated_at is None or int(row["updated_at"]) == int(updated_at)):
        return
    conn().execute(
        """INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
           ON CONFLICT(key) DO UPDATE SET
               value = excluded.value,
               updated_at = excluded.updated_at""",
        (key, value, now_ms() if updated_at is None else int(updated_at)),
    )


def set_text_if_blank(key: str, value: str) -> str:
    """Store `value` unless a non-blank value exists; return what is stored."""
    conn().execute(
        """INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
           ON CONFLICT(key) DO UPDATE SET
               value = excluded.value, updated_at = excluded.updated_at
           WHERE TRIM(settings.value) = ''""",
        (key, str(value), now_ms()),
    )
    return get_text(key)


def delete(key: str) -> None:
    conn().execute("DELETE FROM settings WHERE key = ?", (key,))


def prune_prefix(prefix: str, *, updated_before: int) -> int:
    """Delete every key under `prefix` last written before `updated_before`.

    Lease reporters call this on every report. Look before deleting so a
    report with nothing stale issues no DELETE.
    """
    if conn().execute("SELECT 1 FROM settings WHERE key LIKE ? AND updated_at < ? LIMIT 1",
                      (prefix + "%", int(updated_before))).fetchone() is None:
        return 0
    return conn().execute(
        "DELETE FROM settings WHERE key LIKE ? AND updated_at < ?",
        (prefix + "%", int(updated_before))).rowcount


def count_prefix(prefix: str) -> int:
    return int(conn().execute(
        "SELECT COUNT(*) FROM settings WHERE key LIKE ?", (prefix + "%",)).fetchone()[0])


def values_with_prefix(prefix: str, *, updated_after: int | None = None,
                       updated_through: int | None = None) -> list[str]:
    """Values under `prefix`, optionally bounded by (updated_after, updated_through]."""
    sql = "SELECT value FROM settings WHERE key LIKE ?"
    params: list[object] = [prefix + "%"]
    if updated_after is not None:
        sql += " AND updated_at > ?"
        params.append(int(updated_after))
    if updated_through is not None:
        sql += " AND updated_at <= ?"
        params.append(int(updated_through))
    return [str(row["value"]) for row in conn().execute(sql, tuple(params))]


@contextmanager
def transaction() -> Iterator[None]:
    """BEGIN IMMEDIATE on this thread's connection; commit or roll back."""
    c = conn()
    c.execute("BEGIN IMMEDIATE")
    try:
        yield
        c.execute("COMMIT")
    except BaseException:
        c.execute("ROLLBACK")
        raise
