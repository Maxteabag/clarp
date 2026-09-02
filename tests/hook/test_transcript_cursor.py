"""B8, B9, B11: hook position-tracking, first-run silence, concurrency."""

from __future__ import annotations

import threading

import pytest

from lib.transcript_cursor import (
    CursorStoreError,
    TranscriptCursor,
    reset_spoken_first_all,
    write_assistant_jsonl,
)


@pytest.fixture
def cursor(tmp_path):
    pos_dir = tmp_path / "positions"
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("")  # empty file
    c = TranscriptCursor(pos_dir, "sess-1")
    return c, transcript


def test_first_run_returns_no_text_and_pins_position(cursor):
    """B9: brand-new session must not replay history."""
    c, transcript = cursor
    write_assistant_jsonl(transcript, ["one", "two", "three"])
    with c.locked():
        r = c.advance(transcript)
    assert r.first_run is True
    assert r.texts == []
    assert c.read_position() == transcript.stat().st_size


def test_second_run_returns_only_new_text(cursor):
    """After first-run silence, subsequent advances must report new text."""
    c, transcript = cursor
    write_assistant_jsonl(transcript, ["historical 1", "historical 2"])
    with c.locked():
        c.advance(transcript)
    # Now append new content.
    write_assistant_jsonl(transcript, ["new one", "new two"])
    with c.locked():
        r = c.advance(transcript)
    assert r.first_run is False
    assert r.texts == ["new one", "new two"]


def test_concurrent_advances_do_not_double_speak(cursor):
    """B11: two hooks racing on the same cursor each get a disjoint slice."""
    c, transcript = cursor
    # Bootstrap so we're past first-run.
    with c.locked():
        c.advance(transcript)

    write_assistant_jsonl(transcript, ["chunk one", "chunk two", "chunk three"])

    results: list[list[str]] = []
    barrier = threading.Barrier(2)

    def worker():
        barrier.wait()
        with c.locked():
            r = c.advance(transcript)
            results.append(r.texts)

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start(); t2.start()
    t1.join(); t2.join()

    # Exactly one of the threads should see all three; the other should
    # see none (the second to acquire the lock observes position at EOF).
    sizes = sorted(len(r) for r in results)
    assert sizes == [0, 3], f"expected one full one empty, got {results}"


def test_handles_text_in_list_content(cursor):
    """Newer Claude versions emit content as a list of {type, text} blocks."""
    c, transcript = cursor
    with c.locked():
        c.advance(transcript)
    # Append a list-content entry.
    import json as _json
    with transcript.open("a") as f:
        f.write(_json.dumps({
            "type": "assistant",
            "message": {"content": [
                {"type": "thinking", "thinking": "ignored"},
                {"type": "text", "text": "hello there"},
                {"type": "tool_use", "name": "Bash"},
            ]},
        }) + "\n")
    with c.locked():
        r = c.advance(transcript)
    assert r.texts == ["hello there"]


def test_user_entries_are_ignored(cursor):
    c, transcript = cursor
    with c.locked():
        c.advance(transcript)
    import json as _json
    with transcript.open("a") as f:
        f.write(_json.dumps({
            "type": "user", "message": {"content": "i typed this"},
        }) + "\n")
        f.write(_json.dumps({
            "type": "assistant", "message": {"content": "ok"},
        }) + "\n")
    with c.locked():
        r = c.advance(transcript)
    assert r.texts == ["ok"]


def test_cursor_store_read_failure_is_not_misreported_as_first_run(cursor, monkeypatch):
    c, _ = cursor
    from lib import db

    monkeypatch.setattr(db, "conn", lambda: (_ for _ in ()).throw(RuntimeError("db down")))

    with pytest.raises(CursorStoreError, match="read_position failed"):
        c.read_position()


def test_cursor_store_write_failure_is_not_swallowed(cursor, monkeypatch):
    c, _ = cursor
    from lib import db

    monkeypatch.setattr(db, "conn", lambda: (_ for _ in ()).throw(RuntimeError("db down")))

    with pytest.raises(CursorStoreError, match="write_position failed"):
        c.write_position(42)


def test_cursor_reset_failure_is_not_reported_as_zero_updates(monkeypatch):
    from lib import db

    monkeypatch.setattr(db, "conn", lambda: (_ for _ in ()).throw(RuntimeError("db down")))

    with pytest.raises(CursorStoreError, match="reset_spoken_first_all failed"):
        reset_spoken_first_all()
