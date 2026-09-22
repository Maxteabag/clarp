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


def test_background_coalesces_growth_without_losing_new_version(tmp_path):
    import threading
    cache = transcript_import_cache
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
