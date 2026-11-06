"""Snapshot freshness must not wait for unrelated imports or hold writers."""
import threading
import time

from lib import transcript_import_cache as cache


def test_urgent_snapshot_promotes_deferred_import_and_keeps_watcher_after(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "_BACKGROUND_MIN_INTERVAL", 60)
    path = tmp_path / "source"
    path.write_text("first")
    imported, after = [], []
    importer = lambda: imported.append(path.read_text())
    assert cache.schedule_import(path, importer, owner="owner")
    assert cache.wait_for_background(2)
    path.write_text("second version")
    assert cache.schedule_import(path, importer, owner="owner", after=lambda: after.append("watcher"))
    assert imported == ["first"]
    assert cache.import_for_snapshot(path, importer, owner="owner", timeout=1)
    assert cache.wait_for_background(2)
    assert imported == ["first", "second version"]
    assert after == ["watcher"]


def test_snapshot_wait_finishes_for_its_path_while_another_owner_stays_blocked(tmp_path):
    own, other = tmp_path / "own", tmp_path / "other"
    own.write_text("own bytes")
    other.write_text("other bytes")
    entered, release, other_entered, other_release, done = [threading.Event() for _ in range(5)]
    outcomes = []

    def importer():
        entered.set()
        assert release.wait(3)

    def unrelated():
        other_entered.set()
        assert other_release.wait(3)

    assert cache.schedule_import(own, importer, owner="own")
    assert entered.wait(1)
    assert cache.schedule_import(other, unrelated, owner="other")

    def wait():
        outcomes.append(cache.import_for_snapshot(own, importer, owner="own", timeout=1))
        done.set()

    waiter = threading.Thread(target=wait)
    waiter.start()
    try:
        release.set()
        assert other_entered.wait(1)
        assert done.wait(0.5), "a snapshot must not join the worker for other owners"
        assert outcomes == [True]
    finally:
        release.set()
        other_release.set()
        waiter.join(2)
        assert cache.wait_for_background(2)


def test_blocked_snapshot_returns_within_bound_and_logs_only_codes(tmp_path, capsys):
    from lib import db
    path = tmp_path / "private-canary-path"
    path.write_text("unimported private-canary-bytes")
    entered, release = threading.Event(), threading.Event()
    def blocked():
        database = db.conn()
        database.execute("BEGIN IMMEDIATE")
        try:
            entered.set()
            assert release.wait(3)
        finally:
            database.execute("ROLLBACK")

    assert cache.schedule_import(path, blocked, owner="private-canary-owner")
    assert entered.wait(1)
    try:
        started = time.monotonic()
        assert not cache.import_for_snapshot(path, blocked, owner="private-canary-owner", timeout=0.05)
        elapsed = time.monotonic() - started
        assert 0.04 <= elapsed < 1
        captured = capsys.readouterr().err
        assert "snapshotImportTimeout" in captured
        assert "code=deadline" in captured
        assert "private-canary" not in captured
    finally:
        release.set()
        assert cache.wait_for_background(2)


def test_recent_slow_history_skips_wait_then_recovers_after_a_fast_import(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cache, "_BACKGROUND_MIN_INTERVAL", 0)
    path = tmp_path / "private-canary-source"
    path.write_text("first")
    second_entered, second_release = threading.Event(), threading.Event()
    imported = []

    def importer():
        if not imported:
            time.sleep(0.6)  # a measured import longer than the test's 0.5 s bound
        elif len(imported) == 1:
            second_entered.set()
            assert second_release.wait(3)
        imported.append(path.read_text())

    assert cache.schedule_import(path, importer, owner="private-canary-owner")
    assert cache.wait_for_background(2)
    path.write_text("second")
    try:
        started = time.monotonic()
        assert not cache.import_for_snapshot(path, importer, owner="private-canary-owner", timeout=0.5)
        assert time.monotonic() - started < 0.3
        assert second_entered.wait(3), "skip must schedule refresh after its adaptive window"
        captured = capsys.readouterr().err
        assert "snapshotImportSkip code=recent_slow" in captured
        assert "private-canary" not in captured
    finally:
        second_release.set()
        assert cache.wait_for_background(2)
    path.write_text("third")
    assert cache.import_for_snapshot(path, importer, owner="private-canary-owner", timeout=0.5)
    assert cache.wait_for_background(2)
    assert imported == ["first", "second", "third"]


def test_deadline_cooldown_skips_repeated_waits_for_120s_without_losing_refresh(
        tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace
    path = tmp_path / 'private-canary-source'
    path.write_text('dirty source')
    entered, release = threading.Event(), threading.Event()
    def blocked():
        entered.set()
        assert release.wait(3)
    assert cache.schedule_import(path, blocked, owner='owner')
    assert entered.wait(1)
    clock, waits = [0.0], []
    def elapse_wait(timeout):
        waits.append(timeout)
        clock[0] += timeout
    try:
        with monkeypatch.context() as patch:
            patch.setattr(cache, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
            patch.setattr(cache._cond, 'wait', elapse_wait)
            assert not cache.import_for_snapshot(path, blocked, owner='owner', timeout=.05)
            assert len(waits) == 1
            path.write_text('newest dirty source still needs import')
            assert not cache.import_for_snapshot(path, blocked, owner='owner', timeout=.05)
            assert len(waits) == 1, 'a repeat snapshot waited again during its cooldown'
            clock[0] = 120.049
            assert not cache.import_for_snapshot(path, blocked, owner='owner', timeout=.05)
            assert len(waits) == 1
            clock[0] = 120.05
            assert not cache.import_for_snapshot(path, blocked, owner='owner', timeout=.05)
            assert len(waits) == 2, 'waiting must become eligible at the fixed expiry'
            captured = capsys.readouterr().err
            assert 'snapshotImportSkip code=deadline_cooldown' in captured
            assert 'private-canary' not in captured
    finally:
        release.set()
        assert cache.wait_for_background(2)
    # The refusal to wait is not source coverage: admitted work kept newest bytes.
    assert cache.import_for_snapshot(path, blocked, owner='owner', timeout=.05)
