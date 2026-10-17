"""Process-local coordination for importing backend transcript files.

The inotify transcript streamer normally imports changes before a client asks
for /log.  This cache lets /log reuse that SQLite state instead of reparsing an
unchanged JSONL file, while retaining /log as a fallback importer after missed
watch events or process startup.
"""
from __future__ import annotations

import pathlib
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

_guard = threading.Lock()
# Waits for admitted direct imports to finish (test reset); releases _guard while waiting.
_cond = threading.Condition(_guard)
_path_locks: dict[str, threading.Lock] = {}
_imported: dict[str, tuple[int, ...]] = {}


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


def _signature(path: pathlib.Path) -> tuple[int, ...]:
    source = source_path(path)
    stat = source.stat()
    signature: tuple[int, ...] = (stat.st_mtime_ns, stat.st_size)
    if source != path:
        # A database in WAL mode leaves the main file untouched until a
        # checkpoint; new rows are only in <database>-wal. A stopped OpenCode
        # server never checkpoints, so its last step stayed out of history.
        try:
            wal = pathlib.Path(f"{source}-wal").stat()
        except OSError:
            pass
        else:
            signature += (wal.st_mtime_ns, wal.st_size)
    return signature


def _import_admitted(path: pathlib.Path, importer: Callable[[], None], owner: str,
                     epoch: int) -> bool:
    """The import itself, for a caller already admitted under ``epoch``: the
    worker's popped item, or a direct call that passed import_if_changed's
    admission. Bookkeeping written by a call admitted before a test reset is
    dropped, so it can never land in the next epoch's state."""
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
            if _epoch == epoch:
                _imported[key] = before
        return True


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
    global _direct_active
    with _guard:
        if _resetting:
            raise RuntimeError("transcript import reset in progress")
        _direct_active += 1
        epoch = _epoch
    try:
        return _import_admitted(path, importer, owner, epoch)
    finally:
        with _guard:
            _direct_active -= 1
            _cond.notify_all()


@dataclass(frozen=True)
class DrainRecord:
    """What a test reset waits for: kept across a timed-out reset until a later
    reset finds it all finished. ``worker`` is the Thread object itself, because
    the worker clears the module global before its thread has exited."""
    epoch: int
    timers: tuple
    worker: threading.Thread | None


# Test reset state (tests/conftest.py). The fence refuses new work while a reset
# drains; the owner token belongs to the reset call executing right now.
_epoch = 0
_resetting = False
_reset_owner: object | None = None
_drain: DrainRecord | None = None
_direct_active = 0
_running: tuple[str, int] | None = None


def reset_for_tests(deadline_s: float = 5.0) -> bool:
    """Close admission, wait for admitted work and timers (one deadline), then
    clear state and reopen, all only when quiescent. Returns False, with the
    fence and the drain record kept, when something is still running; a later
    call resumes the same drain. A concurrent call returns False untouched."""
    global _epoch, _resetting, _reset_owner, _drain
    token = object()
    with _guard:
        if _reset_owner is not None:
            return False
        _reset_owner = token
        if not _resetting:
            _resetting = True
            _epoch += 1
            timers = tuple(entry[1] for entry in _deferred.values())
            for timer in timers:
                timer.cancel()
            _drain = DrainRecord(_epoch, timers, _worker)
            _deferred.clear()
            _pending.clear()
        drain = _drain
    try:
        end = time.monotonic() + deadline_s
        for timer in drain.timers:
            timer.join(max(0.0, end - time.monotonic()))
        if drain.worker is not None:
            drain.worker.join(max(0.0, end - time.monotonic()))
        with _cond:
            while _direct_active and (remaining := end - time.monotonic()) > 0:
                _cond.wait(remaining)
            quiet = (all(not timer.is_alive() for timer in drain.timers)
                     and (drain.worker is None or not drain.worker.is_alive())
                     and (_worker is None or not _worker.is_alive())
                     and _running is None and _direct_active == 0)
            if quiet:
                _path_locks.clear()
                _imported.clear()
                _last_started.clear()
                _deferred.clear()
                _pending.clear()
                _drain = None
                _resetting = False
                return True
        from .log import log
        log("transcriptImportResetNotQuiescent", f"epoch={drain.epoch}")
        return False
    finally:
        with _guard:
            if _reset_owner is token:
                _reset_owner = None


# One fallback importer per process; coalesce callers by owner/path. Watcher
# imports share import_if_changed's version lock and remain authoritative.
# A request is (path, importer, owner, after): ``after`` runs on the worker once
# the import returned normally, whether it imported or found the version current.
_pending: dict[str, tuple[tuple, int]] = {}
_worker: threading.Thread | None = None
_BACKGROUND_LIMIT = 64
# A file that keeps growing (a busy Codex rollout polled through /log) is
# re-imported at most once per window, then once more when the window
# closes, instead of a full re-parse per poll. Live text and tools reach
# clients through live items meanwhile (docs/live-items.md).
_BACKGROUND_MIN_INTERVAL = 5.0
_last_started: dict[str, float] = {}
# key -> (newest request, its window timer, epoch): the timer submits it.
_deferred: dict[str, tuple[tuple, threading.Timer, int]] = {}


def _queued_after(key: str):
    queued = _pending.get(key) or _deferred.get(key)
    return queued[0][3] if queued else None


def _ensure_worker() -> None:
    global _worker
    if _worker is None:
        _worker = threading.Thread(target=_drain_background, name='transcript-import', daemon=True)
        _worker.start()


def schedule_import(path: pathlib.Path, importer: Callable[[], None], *, owner: str = '',
                    after: Callable[[], None] | None = None) -> bool:
    """Import ``path`` for ``owner`` on the background worker. With ``after``,
    the request is queued even when this version is already imported, so
    ``after`` still runs once the worker's import returns; never after a failed
    import. False when refused (queue full, or a test reset in progress)."""
    key = f'{owner}\0{path}'
    signature = _signature(path)
    full = False
    with _guard:
        if _resetting:
            return False
        if after is None:
            if _imported.get(key) == signature:
                return False
            # A caller without an after step (/log) must not drop a queued one.
            after = _queued_after(key)
        request = (path, importer, owner, after)
        last = _last_started.get(key)
        wait = _BACKGROUND_MIN_INTERVAL - (time.monotonic() - last) if last is not None else 0.0
        if wait > 0 and key not in _pending:
            if key in _deferred:
                _, timer, epoch = _deferred[key]
                _deferred[key] = (request, timer, epoch)   # the newest request wins
            else:
                timer = threading.Timer(wait, _schedule_deferred)
                timer.args = (key, _epoch, timer)
                timer.daemon = True
                _deferred[key] = (request, timer, _epoch)
                timer.start()
            return True
        if key not in _pending and len(_pending) >= _BACKGROUND_LIMIT:
            # The next request/watcher can retry; never create unbounded threads.
            full = True
        else:
            _pending[key] = (request, _epoch)
            _ensure_worker()
    if full:
        if after is not None:
            from .log import log
            log("transcriptImportQueueFull", f"owner={owner}")
        return False
    return True


def _schedule_deferred(key: str, epoch: int, token: threading.Timer) -> None:
    """The window closed: admit the newest request, unless a test reset began
    since this timer was armed (a stale timer touches nothing)."""
    full_owner = None
    with _guard:
        entry = _deferred.get(key)
        if _resetting or epoch != _epoch or entry is None or entry[1] is not token:
            return
        request = entry[0]
        del _deferred[key]
        _last_started.pop(key, None)
        path, importer, owner, after = request
        if after is None and _imported.get(key) == _signature(path):
            return
        if key not in _pending and len(_pending) >= _BACKGROUND_LIMIT:
            full_owner = owner if after is not None else None
        else:
            if after is None and key in _pending:
                request = (path, importer, owner, _pending[key][0][3])
            _pending[key] = (request, _epoch)
            _ensure_worker()
    if full_owner is not None:
        from .log import log
        log("transcriptImportQueueFull", f"owner={full_owner}")


def is_active(path: pathlib.Path, *, owner: str = '') -> bool:
    """Whether the worker holds a request for (owner, path): queued, waiting for
    its window, or running."""
    key = f'{owner}\0{path}'
    with _guard:
        return key in _pending or key in _deferred or (_running is not None and _running[0] == key)


def _drain_background():
    global _worker, _running
    from . import db
    from .log import log_exception
    try:
        while True:
            with _guard:
                if not _pending:
                    _worker = None
                    return
                key = next(iter(_pending))
                (path, importer, owner, after), item_epoch = _pending.pop(key)
                if _resetting or item_epoch != _epoch:
                    continue                        # invalidated by a test reset
                _running = (key, item_epoch)
            started = time.monotonic()
            try:
                try:
                    _import_admitted(path, importer, owner, item_epoch)
                    with _guard:
                        # Only a completed import opens the window: a failed one
                        # stays retryable at once.
                        if _epoch == item_epoch:
                            _last_started[key] = started
                            if len(_last_started) > 4 * _BACKGROUND_LIMIT:
                                _last_started.pop(next(iter(_last_started)))
                except Exception as exc:
                    log_exception('backgroundTranscriptImportFail', exc, detail=owner)
                else:
                    if after is not None:
                        try:
                            after()
                        except Exception as exc:
                            log_exception('backgroundTranscriptAfterFail', exc, detail=owner)
            finally:
                with _guard:
                    _running = None
                    _cond.notify_all()
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
