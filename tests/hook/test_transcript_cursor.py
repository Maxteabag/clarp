"""Hook cursor spoken_first reset and error reporting."""

from __future__ import annotations

import pytest

from lib.transcript_cursor import CursorStoreError, reset_spoken_first_all


def test_reset_spoken_first_all_clears_persisted_flags():
    from lib import db

    c = db.conn()
    c.execute(
        """INSERT INTO cursor_positions (backend_session_id, position, spoken_first, updated_at)
           VALUES ('sess-a', 120, 1, 1000), ('sess-b', 45, 0, 1000)"""
    )
    touched = reset_spoken_first_all()
    assert touched == 2
    rows = c.execute(
        "SELECT backend_session_id, position, spoken_first FROM cursor_positions ORDER BY backend_session_id"
    ).fetchall()
    assert [(r["backend_session_id"], r["position"], r["spoken_first"]) for r in rows] == [
        ("sess-a", 120, 0),
        ("sess-b", 45, 0),
    ]


def test_cursor_reset_failure_is_not_reported_as_zero_updates(monkeypatch):
    from lib import db

    monkeypatch.setattr(db, "conn", lambda: (_ for _ in ()).throw(RuntimeError("db down")))

    with pytest.raises(CursorStoreError, match="reset_spoken_first_all failed"):
        reset_spoken_first_all()
