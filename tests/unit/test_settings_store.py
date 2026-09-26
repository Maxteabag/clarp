"""settings_store writes only what changed, so idle rewrites take no write lock."""
from __future__ import annotations

import uuid

from lib import application_activity, db, desktop_presence, settings_store
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


def test_presence_reports_prune_on_a_timer(monkeypatch):
    now = [10_000_000]
    monkeypatch.setattr(db, "now_ms", lambda: now[0])
    settings_store._pruned_at.clear()
    report = lambda seq: desktop_presence.update(  # noqa: E731
        principal="administrator", instance_id=instance, sequence=seq,
        active=True, sent_at_ms=now[0])
    instance = str(uuid.uuid4())
    report(1)
    now[0] += 1000
    assert not [s for s in _writes(lambda: report(2)) if s.startswith("DELETE")]
    now[0] += settings_store.PRUNE_INTERVAL_MS
    assert [s for s in _writes(lambda: report(3)) if s.startswith("DELETE")]


def test_activity_cap_prunes_stale_rows_before_refusing(monkeypatch):
    now = [20_000_000]
    monkeypatch.setattr(db, "now_ms", lambda: now[0])
    for i in range(256):
        settings_store.set_text(f"{application_activity.PREFIX}stale{i}", "{}", updated_at=0)
    settings_store._pruned_at[application_activity.PREFIX] = now[0]
    assert application_activity.report(
        "administrator", str(uuid.uuid4()), 1, True, 0, now[0])["accepted"]


def test_unchanged_cursor_position_issues_no_write(tmp_path):
    cursor = TranscriptCursor(tmp_path, "lock-test-session")
    cursor.write_position(42)
    assert _writes(lambda: cursor.write_position(42)) == []
    cursor.write_position(43)
    assert cursor.read_position() == 43
