"""The transcript import cache must accept database-backed pseudo-paths."""
from __future__ import annotations

import os
import pathlib

import pytest

from lib import transcript_import_cache


def _bump_mtime(path: pathlib.Path) -> None:
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))


class _TranscriptCompletionInput:
    """Audit INPUT double; the real cache owns all freshness/publication.

    This explicit private callable protocol is production-facing groundwork,
    not a positive native file/DB certificate supplied by the fixture.
    """
    _clarp_transcript_completion_v1 = True

    def __init__(self, path, owner, *, outcome="completed-changed"):
        self.context = (str(path), owner, "claude", "synthetic-session", "synthetic-db-epoch")
        self.generation = 1
        self.eligible = True
        self.outcome = outcome
        self.calls = 0
        self.before_return = None

    def _clarp_transcript_eligibility(self):
        return self.context, self.generation, self.eligible

    def __call__(self):
        self.calls += 1
        result = {"_clarp_transcript_completion_v1":
                  (self.context, self.generation, self.outcome)}
        if self.before_return is not None:
            self.before_return()
        return result


@pytest.mark.parametrize("entry", ["direct", "schedule", "snapshot"])
def test_due_transcript_authority_precedes_each_cached_signature_shortcut(tmp_path, entry):
    cache = transcript_import_cache
    cache.reset_for_tests()
    path = tmp_path / "audit-due.jsonl"
    path.write_text("{}\n")
    importer = _TranscriptCompletionInput(path, "audit-owner")
    key = f"audit-owner\0{path}"
    try:
        assert cache.import_if_changed(path, importer, owner="audit-owner")
        found, previous_validation = cache._validated_at.get(key, cache._epoch)
        assert found and previous_validation is not None
        importer.generation += 1
        importer.eligible = False
        if entry == "schedule":
            assert cache.schedule_import(path, importer, owner="audit-owner"), (
                "an unchanged signature discarded the due source's pending work")
        elif entry == "snapshot":
            assert not cache.import_for_snapshot(path, importer, owner="audit-owner", timeout=.02), (
                "an old signature certified a new audit-due generation")
        else:
            assert not cache.import_if_changed(path, importer, owner="audit-owner")
        assert cache._validated_at.get(key, cache._epoch) == (True, previous_validation), (
            "a deferred source acquired a new successful validation timestamp")
    finally:
        assert cache.reset_for_tests()


@pytest.mark.parametrize("outcome", ["deferred", "completed-unchanged"])
def test_worker_completion_state_controls_after_and_freshness(tmp_path, outcome, monkeypatch):
    cache = transcript_import_cache
    cache.reset_for_tests()
    monkeypatch.setattr(cache, "_BACKGROUND_MIN_INTERVAL", 0.0)
    path = tmp_path / "completion.jsonl"
    path.write_text("{}\n")
    importer = _TranscriptCompletionInput(path, "audit-owner", outcome=outcome)
    after = []
    key = f"audit-owner\0{path}"
    try:
        assert cache.schedule_import(path, importer, owner="audit-owner",
                                     after=lambda: after.append("completed"))
        assert cache.wait_for_background(2), "the worker must not wait for denied audit IO"
        assert after == ([] if outcome == "deferred" else ["completed"])
        if outcome == "deferred":
            assert key not in cache._imported
            assert not cache._validated_at.get(key, cache._epoch)[0]
            completed = cache._last_started.get("audit-owner")
            assert completed is None or completed[0] is None
        else:
            assert cache._validated_at.get(key, cache._epoch)[0]
    finally:
        assert cache.reset_for_tests()


@pytest.mark.parametrize("entry", ["direct", "snapshot"])
def test_new_audit_generation_before_publication_cannot_satisfy_freshness(tmp_path, entry):
    import threading

    cache = transcript_import_cache
    cache.reset_for_tests()
    path = tmp_path / "generation-race.jsonl"
    path.write_text("{}\n")
    importer = _TranscriptCompletionInput(path, "audit-owner")
    key = f"audit-owner\0{path}"
    try:
        assert cache.import_if_changed(path, importer, owner="audit-owner")
        previous_signature = cache._imported[key]
        previous_validation = cache._validated_at.get(key, cache._epoch)
        path.write_text("{}\n{}\n")
        changed = threading.Event()
        selected_workers = []
        previous_generation = importer.generation

        def audit_became_due():
            importer.generation += 1
            importer.eligible = False
            selected_workers.append(threading.current_thread())
            changed.set()

        importer.before_return = audit_became_due
        if entry == "direct":
            result = cache.import_if_changed(path, importer, owner="audit-owner")
        else:
            result = cache.import_for_snapshot(path, importer, owner="audit-owner", timeout=.02)
        assert changed.wait(2), "the selected callback never reached the generation change"
        assert importer.generation == previous_generation + 1 and not importer.eligible
        if entry == "snapshot":
            worker = selected_workers[0]
            assert worker is not threading.current_thread()
            worker.join(2)
            assert not worker.is_alive(), "selected worker did not finish publication/cleanup"
        assert not result, "an old admission published completion after its audit generation changed"
        assert cache._imported[key] == previous_signature
        assert cache._validated_at.get(key, cache._epoch) == previous_validation
    finally:
        assert cache.reset_for_tests()


@pytest.mark.parametrize("value", [None, 0, False,
    {"_clarp_transcript_completion_v1": "deferred"}])
def test_unopted_callback_results_keep_legacy_completion(tmp_path, value, monkeypatch):
    cache = transcript_import_cache
    cache.reset_for_tests()
    monkeypatch.setattr(cache, "_BACKGROUND_MIN_INTERVAL", 0.0)
    path = tmp_path / "legacy.jsonl"
    path.write_text("{}\n")
    after = []
    try:
        assert cache.schedule_import(path, lambda: value, owner="legacy",
                                     after=lambda: after.append("completed"))
        assert cache.wait_for_background(2)
        assert after == ["completed"]
        assert cache._validated_at.get(f"legacy\0{path}", cache._epoch)[0]
    finally:
        assert cache.reset_for_tests()


@pytest.mark.parametrize("entry", ["direct", "worker"])
def test_accepted_completion_is_not_retroactively_deferred_by_its_observer(tmp_path, entry):
    cache = transcript_import_cache
    assert cache.reset_for_tests()
    path = tmp_path / "observer-generation.jsonl"
    path.write_text("{}\n")
    importer = _TranscriptCompletionInput(path, "observer")
    events, after = [], []

    def completed(value):
        events.append(value)
        importer.generation += 1
        importer.eligible = False

    importer._clarp_transcript_completed = completed
    try:
        if entry == "direct":
            result = cache.import_if_changed(path, importer, owner="observer")
        else:
            assert cache.schedule_import(path, importer, owner="observer",
                                         after=lambda: after.append("accepted"))
            assert cache.wait_for_background(2)
            result = after == ["accepted"]
        assert len(events) == 1 and not importer.eligible
        assert result, "a success already observed was retroactively returned as deferred"
        assert not cache._validated_at.get(f"observer\0{path}", cache._epoch)[0], (
            "the old acceptance refreshed a newly ineligible generation")
    finally:
        assert cache.reset_for_tests()


@pytest.mark.parametrize("observer", ["completion", "after"])
def test_completion_observers_release_authority_and_cache_locks(tmp_path, observer):
    import threading

    cache = transcript_import_cache
    assert cache.reset_for_tests()
    path = tmp_path / "observer-locks.jsonl"
    path.write_text("{}\n")
    importer = _TranscriptCompletionInput(path, "observer")
    authority = threading.RLock()
    importer._clarp_transcript_fence = lambda: authority
    entered, acquired, stop = threading.Event(), threading.Event(), threading.Event()
    threads, observed, after = [], [], []

    def observe(*_):
        def contender():
            if stop.is_set() or not authority.acquire(timeout=1):
                return
            try:
                if stop.is_set() or not cache._guard.acquire(timeout=1):
                    return
                try:
                    if not stop.is_set():
                        acquired.set()
                finally:
                    cache._guard.release()
            finally:
                authority.release()

        thread = threading.Thread(target=contender)
        threads.append(thread)
        thread.start()
        entered.set()
        observed.append(acquired.wait(2))

    if observer == "completion":
        importer._clarp_transcript_completed = observe

    def on_after():
        if observer == "after":
            observe()
        after.append("accepted")

    try:
        assert cache.schedule_import(path, importer, owner="observer", after=on_after)
        assert entered.wait(3), "the actual observer was not invoked"
        assert cache.wait_for_background(4)
        for thread in threads:
            thread.join(2)
            assert not thread.is_alive()
        assert observed == [True], "observer IO retained authority or cache lock"
        assert after == ["accepted"]
    finally:
        stop.set()
        try:
            assert cache.reset_for_tests()
        finally:
            errors = []
            for thread in threads:
                try:
                    # A failing start can leave an honestly unstarted object.
                    if thread.ident is not None:
                        thread.join(3)
                    assert not thread.is_alive(), "unknown/live observer contender"
                except Exception as exc:
                    errors.append(exc)
            if errors:
                raise ExceptionGroup("observer contender cleanup failed", errors)


def test_failed_completion_observer_does_not_publish_cache_success(tmp_path):
    cache = transcript_import_cache
    assert cache.reset_for_tests()
    path = tmp_path / "observer-failure.jsonl"
    path.write_text("{}\n")
    importer = _TranscriptCompletionInput(path, "observer")

    def failed(_):
        raise OSError("owned observer failure")

    importer._clarp_transcript_completed = failed
    try:
        with pytest.raises(OSError, match="owned observer failure"):
            cache.import_if_changed(path, importer, owner="observer")
        assert f"observer\0{path}" not in cache._imported
        assert not cache._validated_at.get(f"observer\0{path}", cache._epoch)[0]
    finally:
        assert cache.reset_for_tests()


def test_running_deferred_import_keeps_capacity_for_its_retry(tmp_path, monkeypatch):
    import threading

    cache = transcript_import_cache
    assert cache.reset_for_tests()
    monkeypatch.setattr(cache, "_BACKGROUND_MIN_INTERVAL", 0.0)
    path = tmp_path / "retry-capacity.jsonl"
    path.write_text("{}\n")
    importer = _TranscriptCompletionInput(path, "reserved", outcome="deferred")
    entered, release = threading.Event(), threading.Event()
    waiting_entered, waiting_release = threading.Event(), threading.Event()
    after = []

    def hold_import():
        entered.set()
        if not release.wait(5):
            raise AssertionError("test did not release the admitted import")

    def waiting():
        waiting_entered.set()
        if not waiting_release.wait(5):
            raise AssertionError("test did not release the waiting import")

    def completed():
        after.append("completed")

    importer.before_return = hold_import
    try:
        assert cache.schedule_import(path, importer, owner="reserved", after=completed)
        assert entered.wait(2)
        admitted = [cache.schedule_import(path, waiting, owner=f"waiting-{i}")
                    for i in range(64)]
        assert sum(admitted) == 63, "running retry reservation allowed a 65th owned intent"
        release.set()
        assert waiting_entered.wait(2)
        key = f"reserved\0{path}"
        with cache._guard:
            pending, deferred = cache._pending.get(key), cache._deferred.get(key)
            assert pending is not None or deferred is not None, "completion lost its admitted retry"
            retained = pending[0] if pending is not None else deferred[0]
            retained_epoch = pending[1] if pending is not None else deferred[2]
            assert retained_epoch == cache._epoch
            assert retained[:3] == (path, importer, "reserved") and retained[3] is completed
            rank = retained[5]
            assert len(set(cache._pending) | set(cache._deferred)) <= 64
        assert after == []
        importer.outcome = "completed-unchanged"
        importer.before_return = None
        assert cache.schedule_import(path, importer, owner="reserved", urgent=True)
        with cache._guard:
            latest = cache._pending.get(key) or cache._deferred.get(key)
            assert latest is not None and latest[0][3] is completed and latest[0][5] == rank
        waiting_release.set()
        assert cache.wait_for_background(4)
        assert after == ["completed"], "the reserved dirty intent did not complete once"
    finally:
        release.set()
        waiting_release.set()
        assert cache.reset_for_tests()


@pytest.mark.parametrize("reenter", ["lookup", "replace"])
def test_worker_marker_descriptor_runs_before_locked_request_consumption(tmp_path, reenter):
    import threading

    cache = transcript_import_cache
    assert cache.reset_for_tests()
    path = tmp_path / "marker-descriptor.jsonl"
    path.write_text("{}\n")
    observations, calls, after = [], [], []

    class GenericImporter:
        replaced = False

        @property
        def _clarp_transcript_completion_v1(self):
            if threading.current_thread().name == "transcript-import":
                unlocked = cache._guard.acquire(blocking=False)
                if unlocked:
                    cache._guard.release()
                # A broken baseline must report the held lock, not deadlock
                # irreapably by entering is_active under that same lock.
                active = cache.is_active(path, owner="marker") if unlocked else None
                observations.append((unlocked, active))
                if unlocked and reenter == "replace" and not self.replaced:
                    self.replaced = True
                    assert cache.schedule_import(
                        path, lambda: calls.append("replacement"), owner="marker",
                        after=lambda: after.append("replacement"))
            return False  # a generic callable, not an opted-in completion

        def __call__(self):
            calls.append("original")

    try:
        assert cache.schedule_import(path, GenericImporter(), owner="marker",
                                     after=lambda: after.append("original"))
        assert cache.wait_for_background(3)
        assert observations and all(unlocked and active for unlocked, active in observations), (
            "worker evaluated user marker under its guard or after losing active ownership")
        expected = ["replacement" if reenter == "replace" else "original"]
        assert calls == expected and after == expected
    finally:
        assert cache.reset_for_tests()


@pytest.mark.parametrize("urgent,startup_exempt,expected", [
    (True, False, 1.0), (False, False, 300.0), (False, True, 300.0)])
def test_completion_retry_respects_urgent_owner_window_bypass(
        tmp_path, monkeypatch, urgent, startup_exempt, expected):
    from types import SimpleNamespace

    cache = transcript_import_cache
    assert cache.reset_for_tests()
    path = tmp_path / "urgent-retry.jsonl"
    path.write_text("{}\n")
    importer = _TranscriptCompletionInput(path, "urgent")
    importer.eligible = False
    key = f"urgent\0{path}"
    # Only the cache's monotonic lookup changes; native thread/SQLite clocks do not.
    monkeypatch.setattr(cache, "time", SimpleNamespace(monotonic=lambda: 1000.0))
    with cache._guard:
        cache._last_started["urgent"] = (1000.0, 50.0)
        cache._completion_retry_at.put(key, cache._epoch, (1001.0, 1))
    try:
        assert cache.schedule_import(path, importer, owner="urgent", urgent=urgent,
                                     startup_exempt=startup_exempt)
        with cache._guard:
            request, timer, _ = cache._deferred[key]
            assert request[4] is urgent
            assert timer.interval == expected, "urgent retry inherited the normal owner wait"
    finally:
        assert cache.reset_for_tests()


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
    finished = threading.Event()
    calls = []
    def importer():
        calls.append(path.read_text())
        if len(calls) == 1:
            entered.set()
            assert release.wait(3)
        else:
            finished.set()
    cache.schedule_import(path, importer, owner='one')
    try:
        assert entered.wait(1)
        path.write_text('second version')
        for _ in range(30):
            cache.schedule_import(path, importer, owner='one')
    finally:
        release.set()
        assert finished.wait(4)  # adaptive completion window can defer the worker
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


def _fence(cache):
    """(owner, drain) of a reset in progress, read under _guard as reset_for_tests
    writes them (one _guard section sets _reset_owner, _resetting, then _drain);
    None until that section is complete. An unguarded read can land inside it."""
    with cache._guard:
        if cache._resetting and cache._drain is not None:
            return cache._reset_owner, cache._drain
    return None


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
    timer = cache._deferred[f"a\0{path}"][1]               # its callback is held before _guard
    try:
        assert cache.reset_for_tests(deadline_s=0.2) is False
        first = cache._drain
        assert first is not None and timer in first.threads   # the held timer is retained
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
        assert _wait_until(lambda: _fence(cache) is not None)
        owner_token, drain = _fence(cache)
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
    result: dict = {}
    resetter = _threading.Thread(target=lambda: result.setdefault("ok", cache.reset_for_tests()))
    try:
        assert closing.wait(2)
        worker = cache._worker
        assert worker is not None                     # not cleared before the close returns
        resetter.start()
        assert _wait_until(lambda: _fence(cache) is not None)
        _, drain = _fence(cache)
        assert resetter.is_alive() and worker in drain.threads
    finally:
        release.set()                                 # never leave the worker held
        if resetter.ident is not None:
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
    """S-2 (b): a window timer removed its own _deferred entry, could not start
    its worker and is logging that outside _guard. Reset must wait for the timer thread
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
        if event == "transcriptImportThreadStartFail":
            logging.set()
            assert release.wait(5)
        return real_log(event, *args, **kwargs)

    monkeypatch.setattr(log_module, "log", held_log)
    path.write_text("1\n2\n")
    _bump_mtime(path)
    assert cache.schedule_import(path, lambda: None, owner="a", after=lambda: None)
    timer = cache._deferred[f"a\0{path}"][1]
    class NoWorker(_threading.Thread):
        def start(self):
            raise RuntimeError("synthetic worker start failure")

    monkeypatch.setattr(cache, "threading", _threading_with(Thread=NoWorker))
    try:
        assert logging.wait(3)
        assert f"a\0{path}" not in cache._deferred and timer.is_alive()
        assert cache.reset_for_tests(deadline_s=0.2) is False
        assert timer in cache._drain.threads
    finally:
        release.set()                                           # never leave the timer held
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
