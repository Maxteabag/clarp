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
