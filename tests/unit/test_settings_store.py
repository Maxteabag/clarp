"""settings_store writes only what changed, so idle rewrites take no write lock."""
from __future__ import annotations

import uuid

from lib import db, desktop_presence, settings_store
from lib.transcript_cursor import TranscriptCursor


def _writes(action):
    statements: list[str] = []
    c = db.conn()
    c.set_trace_callback(statements.append)
    try:
        action()
    finally:
        c.set_trace_callback(None)
    return [s for s in statements if not s.lstrip().upper().startswith("SELECT")]


def test_unchanged_setting_issues_no_write_statement():
    settings_store.set_text("lock-test", "same")
    assert _writes(lambda: settings_store.set_text("lock-test", "same")) == []
    assert _writes(lambda: settings_store.set_text("lock-test", "other"))
    assert settings_store.get("lock-test") == "other"


def test_lease_refresh_with_a_new_stamp_still_writes():
    settings_store.set_text("lease-test", "v", updated_at=1000)
    assert _writes(lambda: settings_store.set_text("lease-test", "v", updated_at=1000)) == []
    assert _writes(lambda: settings_store.set_text("lease-test", "v", updated_at=2000))
    assert settings_store.values_with_prefix("lease-test", updated_after=1500) == ["v"]


def test_presence_reports_delete_only_when_a_lease_is_stale(monkeypatch):
    now = [10_000_000]
    monkeypatch.setattr(db, "now_ms", lambda: now[0])
    report = lambda instance, seq: desktop_presence.update(  # noqa: E731
        principal="administrator", instance_id=instance, sequence=seq,
        active=True, sent_at_ms=now[0])
    first, second = str(uuid.uuid4()), str(uuid.uuid4())
    report(first, 1)
    now[0] += 1000
    assert not [s for s in _writes(lambda: report(first, 2)) if s.startswith("DELETE")]
    now[0] += desktop_presence.TOMBSTONE_MS + 1
    assert [s for s in _writes(lambda: report(second, 1)) if s.startswith("DELETE")]


def test_unchanged_cursor_position_issues_no_write(tmp_path):
    cursor = TranscriptCursor(tmp_path, "lock-test-session")
    cursor.write_position(42)
    assert _writes(lambda: cursor.write_position(42)) == []
    cursor.write_position(43)
    assert cursor.read_position() == 43
