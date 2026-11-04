"""A transcript cursor save that fails (for example 'database is locked') is
logged, not swallowed, and retried until the persisted position is current:
a stale cursor makes a restarted watcher re-read, and re-speak, old text."""
from __future__ import annotations

import json
import sqlite3

from lib import db, transcript_watcher
from lib.transcript_watcher import TranscriptWatcher


class _LockedOnce:
    """db.conn() whose cursor_positions WRITE fails the given number of times
    (reads pass, so a slow verification read cannot hit the injected lock)."""

    def __init__(self, real, failures=1):
        self._real, self.failures = real, failures

    def execute(self, sql, *args):
        if "INSERT INTO cursor_positions" in sql and self.failures > 0:
            self.failures -= 1
            raise sqlite3.OperationalError("database is locked")
        return self._real.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._real, name)


def _line(text):
    return json.dumps({"type": "assistant",
                       "message": {"content": [{"type": "text", "text": text}]}}) + "\n"


def _persisted(session_id):
    row = db.conn().execute("SELECT position FROM cursor_positions WHERE backend_session_id=?",
                            (session_id,)).fetchone()
    return None if row is None else int(row[0])


def test_a_failed_cursor_save_is_logged_and_retried_on_the_next_tick(tmp_path, monkeypatch):
    path = tmp_path / "t.jsonl"
    path.write_text(_line("hello"))
    logged = []
    monkeypatch.setattr(transcript_watcher, "log_exception",
                        lambda event, exc, **kw: logged.append((event, type(exc).__name__)))
    real = db.conn()
    locked = _LockedOnce(real, failures=1)
    monkeypatch.setattr(db, "conn", lambda: locked)
    w = TranscriptWatcher(path, on_text=lambda _t: None, backend_session_id="sess-1",
                          persist_offset=True)

    assert w.tick() == 1
    assert _persisted("sess-1") is None, "control: the first save failed"
    assert logged and logged[0][1] == "OperationalError", "the failure was swallowed silently"

    assert w.tick() == 0                      # no new text; the save is retried
    assert _persisted("sess-1") == path.stat().st_size, "the persisted cursor stayed stale"


def test_repeated_failures_log_once_per_session_not_every_tick(tmp_path, monkeypatch):
    path = tmp_path / "t.jsonl"
    path.write_text(_line("hello"))
    logged = []
    monkeypatch.setattr(transcript_watcher, "log_exception",
                        lambda event, exc, **kw: logged.append(event))
    monkeypatch.setattr(transcript_watcher, "log", lambda *a, **k: None, raising=False)
    locked = _LockedOnce(db.conn(), failures=50)
    monkeypatch.setattr(db, "conn", lambda: locked)
    w = TranscriptWatcher(path, on_text=lambda _t: None, backend_session_id="sess-2",
                          persist_offset=True)
    for _ in range(10):
        w.tick()
    assert len(logged) == 1, logged


def test_an_idle_transcript_catches_up_through_the_dispatcher_retry(tmp_path, monkeypatch):
    """No further writes arrive after the failed save, so no inotify tick
    comes; the dispatcher's bounded wake retries it."""
    import sys
    import time
    import pytest
    if sys.platform != "linux":
        pytest.skip("inotify dispatcher")
    monkeypatch.setattr(transcript_watcher, "SAVE_RETRY_SEC", 0.2, raising=False)
    monkeypatch.setattr(transcript_watcher, "log_exception", lambda *a, **k: None)
    path = tmp_path / "t.jsonl"
    path.write_text(_line("hello"))
    locked = _LockedOnce(db.conn(), failures=1)
    monkeypatch.setattr(db, "conn", lambda: locked)
    pool = transcript_watcher.WatcherPool()
    w = TranscriptWatcher(path, on_text=lambda _t: None, backend_session_id="sess-3",
                          persist_offset=True)
    pool.add("agent", w)
    dispatcher = transcript_watcher.InotifyDispatcher(pool)
    # Registered before start: no registration wake reaches the loop, so only
    # the bounded retry wake can save the cursor (a dispatcher without it
    # sleeps until the next write, which never comes here).
    dispatcher.watch("agent", path)
    dispatcher.start()
    try:
        deadline = time.monotonic() + 10
        while _persisted("sess-3") != path.stat().st_size and time.monotonic() < deadline:
            time.sleep(0.05)
        assert _persisted("sess-3") == path.stat().st_size, "the idle cursor never caught up"
    finally:
        dispatcher.stop()
