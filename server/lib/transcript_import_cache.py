"""Process-local coordination for importing backend transcript files.

The inotify transcript streamer normally imports changes before a client asks
for /log.  This cache lets /log reuse that SQLite state instead of reparsing an
unchanged JSONL file, while retaining /log as a fallback importer after missed
watch events or process startup.
"""
from __future__ import annotations

import pathlib
import threading
from collections.abc import Callable

_guard = threading.Lock()
_path_locks: dict[str, threading.Lock] = {}
_imported: dict[str, tuple[int, int]] = {}


def _signature(path: pathlib.Path) -> tuple[int, int]:
    stat = path.stat()
    return stat.st_mtime_ns, stat.st_size


def import_if_changed(path: pathlib.Path, importer: Callable[[], None]) -> bool:
    """Run importer once for the current file version.

    A per-path lock prevents /log and the inotify thread from parsing the same
    growing transcript concurrently. Failed imports are deliberately not
    cached, so the next watcher tick or request retries them.
    """
    key = str(path)
    with _guard:
        path_lock = _path_locks.setdefault(key, threading.Lock())
    with path_lock:
        before = _signature(path)
        with _guard:
            if _imported.get(key) == before:
                return False
        importer()
        with _guard:
            # Mark the version we actually chose to import. If the backend
            # appended more bytes while parsing, the next call observes a new
            # signature and imports again instead of incorrectly treating those
            # trailing bytes as covered.
            _imported[key] = before
        return True


def reset_for_tests() -> None:
    with _guard:
        _path_locks.clear()
        _imported.clear()
