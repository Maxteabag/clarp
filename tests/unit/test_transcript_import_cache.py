"""The transcript import cache must accept database-backed pseudo-paths."""
from __future__ import annotations

import os
import pathlib

from lib import transcript_import_cache


def _bump_mtime(path: pathlib.Path) -> None:
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))


def test_pseudo_path_signature_tracks_the_database(tmp_path):
    transcript_import_cache.reset_for_tests()
    db = tmp_path / "opencode.db"
    db.write_bytes(b"v1")
    pseudo = pathlib.Path(f"{db}#ses_1")
    calls: list[int] = []

    assert transcript_import_cache.import_if_changed(pseudo, lambda: calls.append(1))
    assert not transcript_import_cache.import_if_changed(pseudo, lambda: calls.append(1))
    assert calls == [1]

    db.write_bytes(b"v2 with more bytes")
    _bump_mtime(db)
    assert transcript_import_cache.import_if_changed(pseudo, lambda: calls.append(2))
    assert calls == [1, 2]
    assert transcript_import_cache.source_size(pseudo) == len(b"v2 with more bytes")


def test_pseudo_path_signature_sees_rows_still_in_the_wal(tmp_path):
    """OpenCode's database is in WAL mode: a stopped server never checkpoints,
    so its last step changed only <database>-wal and never reached history."""
    transcript_import_cache.reset_for_tests()
    db = tmp_path / "opencode.db"
    db.write_bytes(b"main")
    wal = tmp_path / "opencode.db-wal"
    wal.write_bytes(b"")
    pseudo = pathlib.Path(f"{db}#ses_1")
    calls: list[int] = []
    assert transcript_import_cache.import_if_changed(pseudo, lambda: calls.append(1))

    wal.write_bytes(b"frame for the stopped step")
    _bump_mtime(wal)
    assert transcript_import_cache.import_if_changed(pseudo, lambda: calls.append(2))
    assert calls == [1, 2]


def test_plain_file_is_unaffected(tmp_path):
    transcript_import_cache.reset_for_tests()
    path = tmp_path / "conversation.jsonl"
    path.write_text("one")
    assert transcript_import_cache.source_path(path) == path
    assert transcript_import_cache.import_if_changed(path, lambda: None)
    assert not transcript_import_cache.import_if_changed(path, lambda: None)


def test_real_file_containing_hash_is_still_a_file(tmp_path):
    path = tmp_path / "odd#name.jsonl"
    path.write_text("x")
    assert transcript_import_cache.source_path(path) == path


def test_same_file_is_imported_again_for_a_different_agent(tmp_path):
    """An agent created on a conversation another agent already imported must
    still get its own rows; the cache used to skip it as unchanged."""
    transcript_import_cache.reset_for_tests()
    path = tmp_path / "session.jsonl"
    path.write_text("{}\n")
    calls = []
    assert transcript_import_cache.import_if_changed(path, lambda: calls.append("a"), owner="a")
    assert not transcript_import_cache.import_if_changed(path, lambda: calls.append("a"), owner="a")
    assert transcript_import_cache.import_if_changed(path, lambda: calls.append("b"), owner="b")
    assert not transcript_import_cache.import_if_changed(path, lambda: calls.append("b"), owner="b")
    assert calls == ["a", "b"]


def test_background_coalesces_growth_without_losing_new_version(tmp_path, monkeypatch):
    import threading
    cache = transcript_import_cache
    monkeypatch.setattr(cache, "_BACKGROUND_MIN_INTERVAL", 0.0)
    path = tmp_path / 'growing'
    path.write_text('first')
    entered, release = threading.Event(), threading.Event()
    calls = []
    def importer():
        calls.append(path.read_text())
        if len(calls) == 1:
            entered.set()
            assert release.wait(3)
    cache.schedule_import(path, importer, owner='one')
    try:
        assert entered.wait(1)
        path.write_text('second version')
        for _ in range(30):
            cache.schedule_import(path, importer, owner='one')
    finally:
        release.set()
        assert cache.wait_for_background(4)
    assert calls == ['first', 'second version']
    assert not cache.schedule_import(path, importer, owner='one')
    assert cache.schedule_import(path, importer, owner='other')
    assert cache.wait_for_background(4)
    assert calls[-1] == 'second version'
    assert len(calls) == 3


def test_background_failed_import_remains_retryable(tmp_path):
    cache = transcript_import_cache
    path = tmp_path / 'retry'
    path.write_text('retained')
    def failed():
        raise OSError('offline fixture failure')
    cache.schedule_import(path, failed, owner='retry')
    assert cache.wait_for_background(3)
    calls = []
    assert cache.schedule_import(path, lambda: calls.append('ok'), owner='retry')
    assert cache.wait_for_background(3)
    assert calls == ['ok']


def test_background_queue_is_bounded_and_deferred_owner_can_retry(tmp_path, monkeypatch):
    import threading
    cache = transcript_import_cache
    monkeypatch.setattr(cache, '_BACKGROUND_LIMIT', 1)
    path = tmp_path / 'bounded'
    path.write_text('one')
    entered, release = threading.Event(), threading.Event()
    calls = []
    def blocking():
        entered.set()
        assert release.wait(3)
    cache.schedule_import(path, blocking, owner='active')
    try:
        assert entered.wait(1)
        assert cache.schedule_import(path, lambda: calls.append('pending'), owner='pending')
        assert not cache.schedule_import(path, lambda: calls.append('deferred'), owner='deferred')
    finally:
        release.set()
        assert cache.wait_for_background(4)
    assert calls == ['pending']
    assert cache.schedule_import(path, lambda: calls.append('deferred'), owner='deferred')
    assert cache.wait_for_background(4)
    assert calls == ['pending', 'deferred']


def test_a_locked_database_retries_instead_of_dropping_the_import(tmp_path, monkeypatch):
    import sqlite3
    from lib import db as database
    transcript_import_cache.reset_for_tests()
    monkeypatch.setattr(database, "SQLITE_LOCK_RETRY_SLEEP_SEC", 0)
    path = tmp_path / "t.jsonl"
    path.write_text("{}\n")
    attempts: list[int] = []

    def importer():
        attempts.append(1)
        if len(attempts) == 1:
            raise sqlite3.OperationalError("database is locked")

    assert transcript_import_cache.import_if_changed(path, importer)
    assert attempts == [1, 1]


def test_background_imports_of_a_growing_file_are_coalesced_with_a_trailing_import(tmp_path, monkeypatch):
    import time

    transcript_import_cache.reset_for_tests()
    monkeypatch.setattr(transcript_import_cache, "_BACKGROUND_MIN_INTERVAL", 0.4)
    path = tmp_path / "rollout.jsonl"
    path.write_text("1\n")
    imported: list[int] = []

    def importer():
        imported.append(len(path.read_text().splitlines()))

    assert transcript_import_cache.schedule_import(path, importer, owner="a1")
    assert transcript_import_cache.wait_for_background(2)
    for n in range(2, 6):            # the file keeps growing; /log keeps asking
        path.write_text("".join(f"{i}\n" for i in range(1, n + 1)))
        _bump_mtime(path)
        transcript_import_cache.schedule_import(path, importer, owner="a1")
        time.sleep(0.02)
    assert imported == [1]           # no import inside the window
    deadline = time.monotonic() + 3
    while len(imported) < 2 and time.monotonic() < deadline:
        time.sleep(0.05)
    transcript_import_cache.wait_for_background(2)
    assert imported == [1, 5]        # one trailing import sees the latest file


def test_background_after_runs_once_the_import_has_completed(tmp_path, monkeypatch):
    """The streamer's reclassify runs as an import's `after`: only after the
    import returned, in order, on the import worker (not the inotify thread)."""
    import threading
    cache = transcript_import_cache
    cache.reset_for_tests()
    monkeypatch.setattr(cache, "_BACKGROUND_MIN_INTERVAL", 0.0)
    path = tmp_path / "after.jsonl"
    path.write_text("{}\n")
    order: list[str] = []
    assert cache.schedule_import(path, lambda: order.append("import"), owner="a",
                                 after=lambda: order.append(threading.current_thread().name))
    assert cache.wait_for_background(3)
    assert order == ["import", "transcript-import"]


def test_background_after_runs_when_the_version_is_already_imported(tmp_path, monkeypatch):
    """/log usually imports first. Today the reclassify still runs on every
    transcript change, so `after` must not be skipped as 'already current'."""
    cache = transcript_import_cache
    cache.reset_for_tests()
    monkeypatch.setattr(cache, "_BACKGROUND_MIN_INTERVAL", 0.0)
    path = tmp_path / "current.jsonl"
    path.write_text("{}\n")
    assert cache.import_if_changed(path, lambda: None, owner="a")      # /log's import
    calls: list[str] = []
    assert cache.schedule_import(path, lambda: calls.append("import"), owner="a",
                                 after=lambda: calls.append("after"))
    assert cache.wait_for_background(3)
    assert calls == ["after"]


def test_background_after_never_runs_after_a_failed_import(tmp_path, monkeypatch):
    cache = transcript_import_cache
    cache.reset_for_tests()
    monkeypatch.setattr(cache, "_BACKGROUND_MIN_INTERVAL", 0.0)
    path = tmp_path / "fails.jsonl"
    path.write_text("{}\n")
    calls: list[str] = []

    def failed():
        raise OSError("offline fixture failure")

    cache.schedule_import(path, failed, owner="a", after=lambda: calls.append("after"))
    assert cache.wait_for_background(3)
    assert calls == []


def test_background_keeps_the_newest_import_and_after_for_a_pending_key(tmp_path, monkeypatch):
    import threading
    cache = transcript_import_cache
    cache.reset_for_tests()
    monkeypatch.setattr(cache, "_BACKGROUND_MIN_INTERVAL", 0.0)
    busy = tmp_path / "busy.jsonl"
    busy.write_text("{}\n")
    path = tmp_path / "pending.jsonl"
    path.write_text("{}\n")
    entered, release = threading.Event(), threading.Event()
    calls: list[str] = []

    def blocking():
        entered.set()
        assert release.wait(3)

    cache.schedule_import(busy, blocking, owner="busy")
    try:
        assert entered.wait(1)
        cache.schedule_import(path, lambda: calls.append("old import"), owner="a",
                              after=lambda: calls.append("old after"))
        cache.schedule_import(path, lambda: calls.append("new import"), owner="a",
                              after=lambda: calls.append("new after"))
    finally:
        release.set()
        assert cache.wait_for_background(4)
    assert calls == ["new import", "new after"]


def test_a_later_request_without_after_keeps_the_pending_after(tmp_path, monkeypatch):
    """/log schedules the same (owner, path) without an `after`. Replacing the
    streamer's pending request must not drop the streamer's reclassify."""
    import threading
    cache = transcript_import_cache
    cache.reset_for_tests()
    monkeypatch.setattr(cache, "_BACKGROUND_MIN_INTERVAL", 0.0)
    busy = tmp_path / "busy.jsonl"
    busy.write_text("{}\n")
    path = tmp_path / "shared.jsonl"
    path.write_text("{}\n")
    entered, release = threading.Event(), threading.Event()
    calls: list[str] = []

    def blocking():
        entered.set()
        assert release.wait(3)

    cache.schedule_import(busy, blocking, owner="busy")
    try:
        assert entered.wait(1)
        cache.schedule_import(path, lambda: calls.append("streamer import"), owner="a",
                              after=lambda: calls.append("streamer after"))
        cache.schedule_import(path, lambda: calls.append("log import"), owner="a")
    finally:
        release.set()
        assert cache.wait_for_background(4)
    assert calls == ["log import", "streamer after"]


def test_reset_cancels_an_armed_deferred_import(tmp_path, monkeypatch):
    """A deferred import whose window timer is still armed must not run after
    reset_for_tests(): it would submit into the next test's database. The armed
    timer object itself is joined (no sleep), so a slow runner cannot mask it."""
    cache = transcript_import_cache
    assert cache.reset_for_tests()
    monkeypatch.setattr(cache, "_BACKGROUND_MIN_INTERVAL", 0.3)
    path = tmp_path / "armed.jsonl"
    path.write_text("1\n")
    calls: list[int] = []
    assert cache.schedule_import(path, lambda: calls.append(1), owner="a")
    assert cache.wait_for_background(2)
    path.write_text("1\n2\n")
    _bump_mtime(path)
    assert cache.schedule_import(path, lambda: calls.append(2), owner="a")   # inside the window: timer armed
    timer = cache._deferred[f"a\0{path}"][1]
    assert cache.reset_for_tests()
    timer.join(2)
    assert not timer.is_alive()
    assert cache.wait_for_background(2)
    assert calls == [1]


def test_a_deferred_request_whose_file_vanished_is_not_lost_on_the_timer_thread(tmp_path, monkeypatch):
    """SR-2: the window timer stats the file outside _guard; a vanished file does
    not raise on the timer thread (losing the request): the request is still
    submitted and its import failure is logged by the worker as usual."""
    import threading
    cache = transcript_import_cache
    assert cache.reset_for_tests()
    monkeypatch.setattr(cache, "_BACKGROUND_MIN_INTERVAL", 0.2)
    path = tmp_path / "gone.jsonl"
    path.write_text("1\n")
    _opened_window(cache, path)
    escaped: list = []
    monkeypatch.setattr(threading, "excepthook", lambda args: escaped.append(args.exc_type))
    path.write_text("1\n2\n")
    _bump_mtime(path)
    assert cache.schedule_import(path, lambda: None, owner="a")
    timer = cache._deferred[f"a\0{path}"][1]
    path.unlink()
    timer.join(2)
    assert cache.wait_for_background(3)
    assert escaped == []


# ---- r2 design: deferred newest pair, is_active, and the test-reset protocol (§6) ----

import threading as _threading
import time as _time


def _wait_until(predicate, timeout=3.0):
    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        if predicate():
            return True
        _time.sleep(0.01)
    return predicate()


def _opened_window(cache, path, owner="a"):
    """Complete one import so the per-key window is open for the next request."""
    assert cache.schedule_import(path, lambda: None, owner=owner)
    assert cache.wait_for_background(3)


def test_the_deferred_window_keeps_the_newest_import_and_after(tmp_path, monkeypatch):
    """L-2: two requests inside an open window; the timer submits the NEWEST pair."""
    cache = transcript_import_cache
    assert cache.reset_for_tests()
    monkeypatch.setattr(cache, "_BACKGROUND_MIN_INTERVAL", 0.2)
    path = tmp_path / "window.jsonl"
    path.write_text("1\n")
    _opened_window(cache, path)
    path.write_text("1\n2\n")
    _bump_mtime(path)
    calls: list[str] = []
    assert cache.schedule_import(path, lambda: calls.append("old import"), owner="a",
                                 after=lambda: calls.append("old after"))
    assert cache.schedule_import(path, lambda: calls.append("new import"), owner="a",
                                 after=lambda: calls.append("new after"))
    assert _wait_until(lambda: calls == ["new import", "new after"])
    assert cache.wait_for_background(3)
    assert calls == ["new import", "new after"]


def test_is_active_covers_pending_and_running(tmp_path, monkeypatch):
    cache = transcript_import_cache
    assert cache.reset_for_tests()
    monkeypatch.setattr(cache, "_BACKGROUND_MIN_INTERVAL", 0.0)
    path = tmp_path / "active.jsonl"
    path.write_text("{}\n")
    entered, release = _threading.Event(), _threading.Event()

    def blocking():
        entered.set()
        assert release.wait(3)

    assert not cache.is_active(path, owner="a")
    cache.schedule_import(path, blocking, owner="a")
    try:
        assert entered.wait(2)
        assert cache.is_active(path, owner="a")            # running
    finally:
        release.set()
        assert cache.wait_for_background(3)
    assert not cache.is_active(path, owner="a")


def test_a_request_during_reset_is_refused_and_new_work_runs_after(tmp_path, monkeypatch):
    """§6 (ii): an armed timer's callback has ENTERED (held before _guard). While
    reset drains, a new same-key request is refused with no pending entry, timer
    or worker. After reset returns True the old importer never ran; new
    same-key work submitted then runs exactly once."""
    cache = transcript_import_cache
    assert cache.reset_for_tests()
    monkeypatch.setattr(cache, "_BACKGROUND_MIN_INTERVAL", 0.2)
    path = tmp_path / "k.jsonl"
    path.write_text("1\n")
    _opened_window(cache, path)
    entered, release = _threading.Event(), _threading.Event()
    original = cache._schedule_deferred

    def held(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(cache, "_schedule_deferred", held)
    path.write_text("1\n2\n")
    _bump_mtime(path)
    calls: list[str] = []
    assert cache.schedule_import(path, lambda: calls.append("old"), owner="a")
    assert entered.wait(2)
    result: dict = {}
    resetter = _threading.Thread(target=lambda: result.setdefault("ok", cache.reset_for_tests()))
    resetter.start()
    try:
        assert _wait_until(lambda: cache._resetting)
        assert not cache.schedule_import(path, lambda: calls.append("during"), owner="a")
        key = f"a\0{path}"
        assert key not in cache._pending and key not in cache._deferred
        with pytest.raises(RuntimeError):
            cache.import_if_changed(path, lambda: calls.append("direct"), owner="a")
        assert resetter.is_alive()                         # cannot succeed while the callback is held
    finally:
        release.set()
        resetter.join(5)
    assert result["ok"] is True and calls == []
    assert cache.schedule_import(path, lambda: calls.append("new"), owner="a")
    assert cache.wait_for_background(3)
    assert calls == ["new"]


def test_an_import_finishing_during_reset_leaves_no_stale_state(tmp_path, monkeypatch):
    """§6 (iii): an admitted import of a FIXED key finishes while reset drains.
    The same per-key lock object is kept meanwhile; afterwards caches, rate state
    and path locks are empty, the worker thread has really exited, and the SAME
    unchanged file version imports again for real."""
    cache = transcript_import_cache
    assert cache.reset_for_tests()
    monkeypatch.setattr(cache, "_BACKGROUND_MIN_INTERVAL", 0.0)
    path = tmp_path / "fixed.jsonl"
    path.write_text("{}\n")
    entered, release = _threading.Event(), _threading.Event()
    calls: list[str] = []

    def blocking():
        calls.append("first")
        entered.set()
        assert release.wait(5)

    cache.schedule_import(path, blocking, owner="a")
    assert entered.wait(2)
    key = f"a\0{path}"
    lock_before = cache._path_locks[key]
    worker = cache._worker
    result: dict = {}
    resetter = _threading.Thread(target=lambda: result.setdefault("ok", cache.reset_for_tests()))
    resetter.start()
    try:
        assert _wait_until(lambda: cache._resetting)
        assert resetter.is_alive() and cache._path_locks[key] is lock_before
    finally:
        release.set()
        resetter.join(5)
    assert result["ok"] is True
    assert not worker.is_alive()
    assert key not in cache._imported and key not in cache._last_started and not cache._path_locks
    assert cache.import_if_changed(path, lambda: calls.append("again"), owner="a")
    assert calls == ["first", "again"]


def test_an_admitted_worker_item_passes_the_fence_and_writes_no_old_epoch_bookkeeping(tmp_path, monkeypatch):
    """§6 (iv): the worker popped (admitted) its item before the fence closed and
    enters the import body after it. The import runs to completion (it is not a
    fresh direct admission), but its old-epoch bookkeeping is SUPPRESSED (P-c).
    A fresh direct import while fenced is refused. A real direct import admitted
    before the fence keeps reset draining, so reset succeeds only after both end."""
    cache = transcript_import_cache
    assert cache.reset_for_tests()
    monkeypatch.setattr(cache, "_BACKGROUND_MIN_INTERVAL", 0.0)
    path = tmp_path / "boundary.jsonl"
    path.write_text("{}\n")
    other = tmp_path / "direct.jsonl"
    other.write_text("{}\n")
    at_body, go_body = _threading.Event(), _threading.Event()
    direct_in, direct_go = _threading.Event(), _threading.Event()
    original = cache._import_admitted
    calls: list[str] = []

    def held_admitted(path_, importer, owner, epoch):
        if owner == "a":                                # only the worker's item is held at body entry
            at_body.set()
            assert go_body.wait(5)
        return original(path_, importer, owner, epoch)

    monkeypatch.setattr(cache, "_import_admitted", held_admitted)

    def direct_importer():
        direct_in.set()
        assert direct_go.wait(5)

    direct = _threading.Thread(target=lambda: cache.import_if_changed(other, direct_importer, owner="d"))
    direct.start()
    assert direct_in.wait(2)                            # admitted direct call in flight
    cache.schedule_import(path, lambda: calls.append("admitted"), owner="a")
    assert at_body.wait(2)                              # worker admitted, before the import body
    result: dict = {}
    resetter = _threading.Thread(target=lambda: result.setdefault("ok", cache.reset_for_tests()))
    resetter.start()
    try:
        assert _wait_until(lambda: cache._resetting)
        with pytest.raises(RuntimeError):
            cache.import_if_changed(other, lambda: calls.append("fresh"), owner="e")
        go_body.set()
        assert _wait_until(lambda: calls == ["admitted"] and cache._running is None)
        key = f"a\0{path}"
        assert key not in cache._imported and key not in cache._last_started   # P-c suppressed
        assert resetter.is_alive()                      # the direct import still runs
    finally:
        go_body.set()
        direct_go.set()
        direct.join(5)
        resetter.join(5)
    assert result["ok"] is True and calls == ["admitted"]


def test_a_timed_out_reset_fails_closed_and_a_later_reset_resumes_the_same_drain(tmp_path, monkeypatch):
    """§6 (v): reset times out on a held importer: False, the fence and drain record
    stay, admission is refused. After release a later reset continues the SAME
    epoch and drain record and reopens; one new request runs."""
    cache = transcript_import_cache
    assert cache.reset_for_tests()
    monkeypatch.setattr(cache, "_BACKGROUND_MIN_INTERVAL", 0.0)
    path = tmp_path / "slow.jsonl"
    path.write_text("{}\n")
    entered, release = _threading.Event(), _threading.Event()

    def blocking():
        entered.set()
        assert release.wait(5)

    cache.schedule_import(path, blocking, owner="a")
    assert entered.wait(2)
    try:
        assert cache.reset_for_tests(deadline_s=0.2) is False
        drain = cache._drain
        assert cache._resetting and drain is not None and cache._running is not None
        assert cache._reset_owner is None
        assert not cache.schedule_import(path, lambda: None, owner="b")
    finally:
        release.set()
    assert cache.reset_for_tests() is True
    assert cache._drain is None and not cache._resetting
    assert drain.epoch == cache._epoch                     # resumed, not re-bumped
    calls: list[str] = []
    assert cache.schedule_import(path, lambda: calls.append("new"), owner="a")
    assert cache.wait_for_background(3)
    assert calls == ["new"]


def test_a_timer_held_across_two_resets(tmp_path, monkeypatch):
    """§6 (vi): a timer callback held past reset #1 keeps reset #2 failing closed
    with the drain record unchanged; after release reset #3 joins the retained
    handle and reopens."""
    cache = transcript_import_cache
    assert cache.reset_for_tests()
    monkeypatch.setattr(cache, "_BACKGROUND_MIN_INTERVAL", 0.2)
    path = tmp_path / "timer.jsonl"
    path.write_text("1\n")
    _opened_window(cache, path)
    entered, release = _threading.Event(), _threading.Event()
    original = cache._schedule_deferred

    def held(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(cache, "_schedule_deferred", held)
    path.write_text("1\n2\n")
    _bump_mtime(path)
    calls: list[str] = []
    cache.schedule_import(path, lambda: calls.append("old"), owner="a")
    assert entered.wait(2)
    try:
        assert cache.reset_for_tests(deadline_s=0.2) is False
        first = cache._drain
        assert first is not None and len(first.timers) == 1
        assert cache.reset_for_tests(deadline_s=0.2) is False
        assert cache._drain is first                       # unchanged record
    finally:
        release.set()
    assert cache.reset_for_tests() is True
    assert cache._drain is None and calls == []
    assert cache.schedule_import(path, lambda: calls.append("new"), owner="a")
    assert cache.wait_for_background(3)
    assert calls == ["new"]


def test_a_concurrent_reset_is_refused_without_touching_the_owner_or_drain(tmp_path, monkeypatch):
    """§6 (vii): while one reset drains (another thread), a second call returns
    False at once and never clears the first call's owner token or its record."""
    cache = transcript_import_cache
    assert cache.reset_for_tests()
    monkeypatch.setattr(cache, "_BACKGROUND_MIN_INTERVAL", 0.0)
    path = tmp_path / "busy.jsonl"
    path.write_text("{}\n")
    entered, release = _threading.Event(), _threading.Event()

    def blocking():
        entered.set()
        assert release.wait(5)

    cache.schedule_import(path, blocking, owner="a")
    assert entered.wait(2)
    result: dict = {}
    first = _threading.Thread(target=lambda: result.setdefault("ok", cache.reset_for_tests()))
    first.start()
    try:
        assert _wait_until(lambda: cache._reset_owner is not None)
        owner_token, drain = cache._reset_owner, cache._drain
        assert cache.reset_for_tests() is False                 # R2 refused
        assert cache._reset_owner is owner_token and cache._drain is drain
        assert cache.reset_for_tests() is False                 # R3 refused too, R1 still owns
        assert cache._reset_owner is owner_token and cache._drain is drain
    finally:
        release.set()
        first.join(5)
    assert result["ok"] is True and cache._reset_owner is None


def test_reset_waits_for_an_admitted_direct_import(tmp_path):
    """§6 (viii): reset cannot report quiescence while a direct import runs."""
    cache = transcript_import_cache
    assert cache.reset_for_tests()
    path = tmp_path / "direct.jsonl"
    path.write_text("{}\n")
    entered, release = _threading.Event(), _threading.Event()

    def blocking():
        entered.set()
        assert release.wait(5)

    direct = _threading.Thread(target=lambda: cache.import_if_changed(path, blocking, owner="a"))
    direct.start()
    assert entered.wait(2)
    result: dict = {}
    resetter = _threading.Thread(target=lambda: result.setdefault("ok", cache.reset_for_tests()))
    resetter.start()
    try:
        assert _wait_until(lambda: cache._resetting)
        assert resetter.is_alive()
    finally:
        release.set()
        direct.join(5)
        resetter.join(5)
    assert result["ok"] is True


def test_reset_waits_for_a_worker_still_closing_its_connection(tmp_path, monkeypatch):
    """§6 (f): the worker finished its item and is closing its DB connection.
    It still owns _worker until that close returns, so reset waits for the thread
    instead of reading a cleared global as 'exited'."""
    from lib import db as database
    cache = transcript_import_cache
    assert cache.reset_for_tests()
    monkeypatch.setattr(cache, "_BACKGROUND_MIN_INTERVAL", 0.0)
    path = tmp_path / "tail.jsonl"
    path.write_text("{}\n")
    closing, release = _threading.Event(), _threading.Event()
    original_close = database.close_local

    worker_closes: list[int] = []

    def held_close():
        # Hold only the IDLE close: the worker's first close is the per-item one
        # after the import (where _worker is set in every version); the second is
        # the idle branch's, which must run before _worker is cleared.
        if _threading.current_thread().name == "transcript-import":
            worker_closes.append(1)
            if len(worker_closes) == 2:
                closing.set()
                assert release.wait(5)
        return original_close()

    monkeypatch.setattr(database, "close_local", held_close)
    cache.schedule_import(path, lambda: None, owner="a")
    assert closing.wait(2)
    worker = cache._worker
    assert worker is not None                         # not cleared before the close returns
    result: dict = {}
    resetter = _threading.Thread(target=lambda: result.setdefault("ok", cache.reset_for_tests()))
    resetter.start()
    try:
        assert _wait_until(lambda: cache._resetting)
        assert resetter.is_alive() and worker in cache._drain.threads
    finally:
        release.set()
        resetter.join(5)
    assert result["ok"] is True and not worker.is_alive()


def test_a_popped_item_is_never_invisible_to_is_active_before_it_runs(tmp_path, monkeypatch):
    """r7/ADD-1 (R-1): the worker pops _pending and installs _running in the SAME
    _guard section. Every time the worker releases _guard before the importer
    starts, the request is still in _pending or _running, so reconcile can never
    read is_active() False and resubmit a request that is about to run."""
    cache = transcript_import_cache
    assert cache.reset_for_tests()
    path = tmp_path / "gap.jsonl"
    path.write_text("{}\n")
    key = f"a\0{path}"
    started = _threading.Event()
    gaps: list[str] = []
    real = cache._guard

    class CheckedGuard:
        def __enter__(self):
            return real.__enter__()

        def __exit__(self, *exc):
            if _threading.current_thread().name == "transcript-import" and not started.is_set():
                running = cache._running
                if not (key in cache._pending or (running is not None and running[0] == key)):
                    gaps.append("released with the request in neither _pending nor _running")
            return real.__exit__(*exc)

    monkeypatch.setattr(cache, "_guard", CheckedGuard())
    assert cache.schedule_import(path, started.set, owner="a")
    assert started.wait(3)
    assert cache.wait_for_background(3)
    assert gaps == []


# ---- S-2: thread-lifetime registry ----

def test_reset_waits_for_a_worker_tail_after_it_cleared_the_global(tmp_path, monkeypatch):
    """S-2 (a): the worker cleared _worker and is still unwinding (its outer
    finally closes the connection). Reset must still wait for that thread object:
    it is in the registry even though the global no longer names it."""
    from lib import db as database
    cache = transcript_import_cache
    assert cache.reset_for_tests()
    monkeypatch.setattr(cache, "_BACKGROUND_MIN_INTERVAL", 0.0)
    path = tmp_path / "unwind.jsonl"
    path.write_text("{}\n")
    unwinding, release = _threading.Event(), _threading.Event()
    original_close = database.close_local

    def held_close():
        # Hold only a close the worker reaches AFTER it cleared _worker (its outer
        # finally), identified by that state rather than by a call count: the
        # per-item and idle-branch closes both run while _worker is this thread.
        me = _threading.current_thread()
        if me.name == "transcript-import" and cache._worker is not me and not unwinding.is_set():
            unwinding.set()
            assert release.wait(5)
        return original_close()

    monkeypatch.setattr(database, "close_local", held_close)
    assert cache.schedule_import(path, lambda: None, owner="a")
    try:
        # From the registry, not _worker: the worker may already have cleared
        # _worker by now. The reset at the start pruned every earlier thread.
        (worker,) = cache._threads
        assert unwinding.wait(3)
        assert cache._worker is None and worker.is_alive()
        assert cache.reset_for_tests(deadline_s=0.2) is False     # the tail is waited for
        assert cache._resetting and worker in cache._drain.threads
    finally:
        release.set()                       # never leave the worker held, whatever failed
    assert cache.reset_for_tests()
    assert not worker.is_alive()


def test_reset_waits_for_a_timer_callback_after_it_removed_its_entry(tmp_path, monkeypatch):
    """S-2 (b): a window timer removed its own _deferred entry, found the queue
    full and is logging that outside _guard. Reset must wait for the timer thread
    although no _deferred entry names it any more."""
    from lib import log as log_module
    cache = transcript_import_cache
    assert cache.reset_for_tests()
    monkeypatch.setattr(cache, "_BACKGROUND_MIN_INTERVAL", 0.2)
    path = tmp_path / "full.jsonl"
    path.write_text("1\n")
    _opened_window(cache, path)
    logging, release = _threading.Event(), _threading.Event()
    real_log = log_module.log

    def held_log(event, *args, **kwargs):
        if event == "transcriptImportQueueFull":
            logging.set()
            assert release.wait(5)
        return real_log(event, *args, **kwargs)

    monkeypatch.setattr(log_module, "log", held_log)
    path.write_text("1\n2\n")
    _bump_mtime(path)
    assert cache.schedule_import(path, lambda: None, owner="a", after=lambda: None)
    timer = cache._deferred[f"a\0{path}"][1]
    monkeypatch.setattr(cache, "_BACKGROUND_LIMIT", 0)          # the window closes onto a full queue
    assert logging.wait(3)
    assert f"a\0{path}" not in cache._deferred and timer.is_alive()
    try:
        assert cache.reset_for_tests(deadline_s=0.2) is False
        assert timer in cache._drain.threads
    finally:
        release.set()
    assert cache.reset_for_tests()
    assert not timer.is_alive()


def test_the_registry_prunes_finished_threads_instead_of_keeping_history(tmp_path, monkeypatch):
    """S-2 (c): pruning keeps the registry from growing with completed history:
    after each of several sequential worker lifetimes it holds only the latest
    (dead) worker, never the earlier ones. This is not a bound on live threads."""
    cache = transcript_import_cache
    assert cache.reset_for_tests()
    monkeypatch.setattr(cache, "_BACKGROUND_MIN_INTERVAL", 0.0)
    seen: list = []
    for n in range(5):
        path = tmp_path / f"seq-{n}.jsonl"
        path.write_text("{}\n")
        assert cache.schedule_import(path, lambda: None, owner="a")
        (worker,) = cache._threads          # started in schedule's section; dead ones pruned there
        assert cache.wait_for_background(3)
        worker.join(3)
        assert worker not in seen
        seen.append(worker)
        assert cache._threads == {worker}


def _threading_with(**overrides):
    import types
    return types.SimpleNamespace(**{**vars(_threading), **overrides})


def test_a_worker_that_fails_to_start_leaves_no_live_worker_or_pending_owner(tmp_path, monkeypatch):
    """S-2: Thread.start() can fail (no threads left). The request is refused
    and nothing claims a live worker: no _worker, no pending entry, nothing in
    the registry, so a later request starts a worker normally."""
    cache = transcript_import_cache
    assert cache.reset_for_tests()
    monkeypatch.setattr(cache, "_BACKGROUND_MIN_INTERVAL", 0.0)

    class NoStart(_threading.Thread):
        def start(self):
            raise RuntimeError("can't start new thread")

    path = tmp_path / "nostart.jsonl"
    path.write_text("{}\n")
    monkeypatch.setattr(cache, "threading", _threading_with(Thread=NoStart))
    assert cache.schedule_import(path, lambda: None, owner="a") is False
    assert cache._worker is None and not cache._pending and not cache._threads
    monkeypatch.setattr(cache, "threading", _threading)
    calls: list[int] = []
    assert cache.schedule_import(path, lambda: calls.append(1), owner="a")
    assert cache.wait_for_background(3) and calls == [1]


def test_a_window_timer_that_fails_to_start_is_not_left_armed(tmp_path, monkeypatch):
    """S-2: a timer that cannot start is refused, not left in _deferred or the
    registry (where it would block every later request and every reset)."""
    cache = transcript_import_cache
    assert cache.reset_for_tests()
    monkeypatch.setattr(cache, "_BACKGROUND_MIN_INTERVAL", 0.2)
    path = tmp_path / "notimer.jsonl"
    path.write_text("1\n")
    _opened_window(cache, path)

    made: list = []

    class NoStart(_threading.Timer):
        def start(self):
            made.append(self)
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr(cache, "threading", _threading_with(Timer=NoStart))
    path.write_text("1\n2\n")
    _bump_mtime(path)
    assert cache.schedule_import(path, lambda: None, owner="a") is False
    assert f"a\0{path}" not in cache._deferred
    assert len(made) == 1 and made[0] not in cache._threads
    monkeypatch.setattr(cache, "threading", _threading)
    assert cache.reset_for_tests()
