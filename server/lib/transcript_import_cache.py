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


def source_path(path: pathlib.Path) -> pathlib.Path:
    """The file whose mtime/size track ``path``.

    Backends that keep every session in one database describe a transcript as
    ``<database>#<session_id>`` (see ``opencode_transcript``). That pseudo-path
    is not a file; its change signature is the database's.
    """
    raw = str(path)
    if "#" in raw and not path.exists():
        return pathlib.Path(raw.split("#", 1)[0])
    return path


def source_size(path: pathlib.Path) -> int:
    return source_path(path).stat().st_size


def _signature(path: pathlib.Path) -> tuple[int, int]:
    stat = source_path(path).stat()
    return stat.st_mtime_ns, stat.st_size


def import_if_changed(path: pathlib.Path, importer: Callable[[], None],
                      *, owner: str = "") -> bool:
    """Run importer once per owner for the current file version.

    ``owner`` is the agent the turns are stored under. The import writes rows
    for that agent, so a file already imported for one agent is still unread
    for another: an agent created on an existing conversation (resume, restore)
    would otherwise be skipped and open with an empty history until the
    transcript next changed.

    A per-path lock prevents /log and the inotify thread from parsing the same
    growing transcript concurrently. Failed imports are deliberately not
    cached, so the next watcher tick or request retries them.
    """
    key = f"{owner}\0{path}"
    with _guard:
        path_lock = _path_locks.setdefault(key, threading.Lock())
    with path_lock:
        before = _signature(path)
        with _guard:
            if _imported.get(key) == before:
                return False
        # A busy writer must not cost the whole import; imports are idempotent.
        from . import db
        db.retry_locked(importer)
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


# One fallback importer per process; coalesce callers by owner/path. Watcher
# imports share import_if_changed's version lock and remain authoritative.
_pending: dict[str, tuple[pathlib.Path, Callable[[], None], str]] = {}
_worker: threading.Thread | None = None
_BACKGROUND_LIMIT = 64


def schedule_import(path: pathlib.Path, importer: Callable[[], None], *, owner: str = '') -> bool:
    global _worker
    key = f'{owner}\0{path}'
    signature = _signature(path)
    with _guard:
        if _imported.get(key) == signature:
            return False
        if key not in _pending and len(_pending) >= _BACKGROUND_LIMIT:
            # The next request/watcher can retry; never create unbounded threads.
            return False
        _pending[key] = (path, importer, owner)
        if _worker is None:
            _worker = threading.Thread(target=_drain_background, name='transcript-import', daemon=True)
            _worker.start()
    return True


def _drain_background():
    global _worker
    from . import db
    from .log import log_exception
    try:
        while True:
            with _guard:
                if not _pending:
                    _worker = None
                    return
                key = next(iter(_pending))
                path, importer, owner = _pending.pop(key)
            try:
                import_if_changed(path, importer, owner=owner)
            except Exception as exc:
                log_exception('backgroundTranscriptImportFail', exc, detail=owner)
            finally:
                db.close_local()
    finally:
        db.close_local()


def wait_for_background(timeout: float = 5) -> bool:
    with _guard:
        worker = _worker
    if worker:
        worker.join(timeout)
        return not worker.is_alive()
    return True
