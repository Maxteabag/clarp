"""Process-local coordination for importing backend transcript files.

The inotify transcript streamer normally imports changes before a client asks
for /log.  This cache lets /log reuse that SQLite state instead of reparsing an
unchanged JSONL file, while retaining /log as a fallback importer after missed
watch events or process startup.
"""
from __future__ import annotations

import pathlib
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from .revisioned_cache import RevisionedCache

_guard = threading.Lock()
# Waits for admitted direct imports to finish (test reset); releases _guard while waiting.
_cond = threading.Condition(_guard)
_path_locks: dict[str, threading.Lock] = {}
_imported: dict[str, tuple[int, ...]] = {}
# When an import last chose its source version, including unchanged checks.
_validated_at: RevisionedCache[str, float] = RevisionedCache(
    "transcript-import-validation", max_entries=1024)
# Last actual worker import cost for this owner/path, excluding queue time.
# Unchanged version checks do not erase a previously measured slow import.
_last_import_duration: RevisionedCache[str, float] = RevisionedCache(
    "transcript-import-duration", max_entries=1024)


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
        observed_at = time.monotonic()
        before = _signature(path)
        with _guard:
            if _imported.get(key) == before:
                if _epoch == epoch:
                    _validated_at.put(key, epoch, observed_at)
                    _cond.notify_all()
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
                _validated_at.put(key, epoch, observed_at)
                _cond.notify_all()
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
    reset finds it all finished. ``threads`` are the Thread objects themselves,
    from the registry: a worker clears the module global, and a timer removes its
    _deferred entry, before its thread has exited."""
    epoch: int
    threads: tuple


# Test reset state (tests/conftest.py). The fence refuses new work while a reset
# drains; the owner token belongs to the reset call executing right now.
_epoch = 0
_resetting = False
_reset_owner: object | None = None
_drain: DrainRecord | None = None
_direct_active = 0
_running: tuple[str, int] | None = None
# Every worker and window-timer Thread this module started, registered under
# _guard in the same section that starts it. A thread never removes itself (it
# cannot report having fully exited); dead entries are pruned when a thread is
# started and when a reset clears. Its size is the live tracked threads plus
# those that died since the last prune: no history accumulates, but it is not a
# cap on live timers or unwinding threads.
_threads: set[threading.Thread] = set()


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
            for entry in _deferred.values():
                entry[1].cancel()
            # Under the fence nothing new starts, so the live registry is every
            # thread this reset has to outlive: armed timers, the worker, and
            # any worker or timer callback still unwinding off the globals.
            _drain = DrainRecord(_epoch, tuple(t for t in _threads if t.is_alive()))
            _deferred.clear()
            _pending.clear()
            _cond.notify_all()
        drain = _drain
    try:
        end = time.monotonic() + deadline_s
        for thread in drain.threads:
            thread.join(max(0.0, end - time.monotonic()))
        with _cond:
            while _direct_active and (remaining := end - time.monotonic()) > 0:
                _cond.wait(remaining)
            quiet = (all(not thread.is_alive() for thread in drain.threads)
                     and (_worker is None or not _worker.is_alive())
                     and _running is None and _direct_active == 0)
            if quiet:
                _path_locks.clear()
                _imported.clear()
                _validated_at.reset()
                _last_import_duration.reset()
                _last_started.clear()
                _deferred.clear()
                _pending.clear()
                _threads.difference_update([t for t in _threads if not t.is_alive()])
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
# A request is (path, importer, owner, after, urgent): ``after`` runs on the worker once
# the import returned normally, whether it imported or found the version current.
_pending: dict[str, tuple[tuple, int]] = {}
_worker: threading.Thread | None = None
_BACKGROUND_LIMIT = 64
# A file that keeps growing (a busy Codex rollout polled through /log) is
# re-imported at most once per window, then once more when the window
# closes, instead of a full re-parse per poll. Live text and tools reach
# clients through live items meanwhile (docs/live-items.md).
_BACKGROUND_MIN_INTERVAL = 10.0
# Historical name retained for the reset/ownership seam: now keyed by agent
# (ownerless calls by path), with (actual completion, duration). None reserves
# a bounded slot for admitted work whose first validation has not completed.
_last_started: dict[str, tuple[float | None, float]] = {}
# key -> (newest request, its window timer, epoch): the timer submits it.
_deferred: dict[str, tuple[tuple, threading.Timer, int]] = {}


def _queued_after(key: str):
    queued = _pending.get(key) or _deferred.get(key)
    return queued[0][3] if queued else None


def _start_tracked(thread: threading.Thread) -> bool:
    """Under _guard: register and start ``thread`` in this one section, so a
    reset never snapshots a registered thread that has not started. False, with
    nothing registered, when the thread could not start."""
    _threads.difference_update([t for t in _threads if not t.is_alive()])
    _threads.add(thread)
    try:
        thread.start()
    except RuntimeError:
        _threads.discard(thread)
        return False
    return True


def _ensure_worker() -> bool:
    """Under _guard. False when no worker runs and a new one could not start."""
    global _worker
    if _worker is None:
        worker = threading.Thread(target=_drain_background, name='transcript-import', daemon=True)
        if not _start_tracked(worker):
            return False
        _worker = worker
    return True


def _owner_key(key: str) -> str:
    return key.partition("\0")[0] or key


def _owner_deadline(key: str) -> float:
    completed, duration = _last_started.get(_owner_key(key), (None, 0.0))
    if completed is None:
        return 0.0
    return completed + max(_BACKGROUND_MIN_INTERVAL, 2 * duration)


def _reserve_owner(key: str, now: float) -> bool:
    """Under _guard; never evict an unexpired or admitted owner's window."""
    active = {_owner_key(k) for k in (*_pending, *_deferred)}
    if _running is not None:
        active.add(_owner_key(_running[0]))
    for owner, (completed, duration) in list(_last_started.items()):
        if owner not in active and (completed is None or
                completed + max(_BACKGROUND_MIN_INTERVAL, 2 * duration) <= now):
            del _last_started[owner]
    owner = _owner_key(key)
    if owner in _last_started:
        return True
    if len(_last_started) >= 4 * _BACKGROUND_LIMIT:
        return False
    _last_started[owner] = (None, 0.0)
    return True


def _defer_locked(key: str, request: tuple, epoch: int, wait: float) -> bool:
    """Arm one tracked timer; replacing dirty work never moves its deadline."""
    if key in _deferred:
        _, timer, old_epoch = _deferred[key]
        _deferred[key] = (request, timer, old_epoch)
        return True
    timer = threading.Timer(wait, _schedule_deferred)
    timer.args = (key, epoch, timer)
    timer.daemon = True
    if not _start_tracked(timer):
        return False
    _deferred[key] = (request, timer, epoch)
    return True


def schedule_import(path: pathlib.Path, importer: Callable[[], None], *, owner: str = '',
                    after: Callable[[], None] | None = None, urgent: bool = False) -> bool:
    """Coalesce normal work by agent; urgent RM-N12 requests bypass at drain.

    Dirty signatures, locks and latest-winner callbacks remain per owner/path.
    Every successful validation opens an adaptive completion-based normal window.
    False means refused: the source version remains unread and retryable.
    """
    key = f'{owner}\0{path}'
    signature = _signature(path)
    full = start_failed = False
    with _guard:
        if _resetting:
            return False
        queued = _pending.get(key) or _deferred.get(key)
        if after is None:
            if _imported.get(key) == signature:
                return False
            after = queued[0][3] if queued else None
        # Once requested, urgency belongs to the queued token, not to whichever
        # normal request last supplied the newest importer/callback.
        urgent = urgent or bool(queued and queued[0][4])
        request = (path, importer, owner, after, urgent)
        now = time.monotonic()
        if (key not in _pending and key not in _deferred
                and len(set(_pending) | set(_deferred)) >= _BACKGROUND_LIMIT):
            full = True
        elif not _reserve_owner(key, now):
            full = True
        elif not urgent and key not in _pending and (wait := _owner_deadline(key) - now) > 0:
            start_failed = not _defer_locked(key, request, _epoch, wait)
        else:
            if key in _deferred:
                _, timer, _ = _deferred.pop(key)
                timer.cancel()
            _pending[key] = (request, _epoch)
            if not _ensure_worker():
                del _pending[key]
                start_failed = True
    if start_failed:
        from .log import log
        log("transcriptImportThreadStartFail", f"owner={owner}")
        return False
    if full:
        from .log import log
        log("transcriptImportQueueFull", f"owner={owner}")
        return False
    return True


def import_for_snapshot(path: pathlib.Path, importer: Callable[[], None], *,
                        owner: str, timeout: float) -> bool:
    """Give this owner's observed source version a bounded chance to land.

    Called outside SQLite transactions. The condition releases _guard while
    waiting; the worker keeps its per-path lock, batching and writer fairness.
    A newer import chosen after this request also covers its observed bytes.
    Failure or a busy worker falls back without joining unrelated owners.
    """
    started = time.monotonic()
    deadline = started + timeout
    signature = _signature(path)
    key = f'{owner}\0{path}'
    with _guard:
        epoch = _epoch
        if not _resetting and _imported.get(key) == signature:
            return True
        _, recent_duration = _last_import_duration.get(key, epoch)
    if recent_duration is not None and recent_duration > timeout:
        # Known slow histories retain today's asynchronous/coalesced refresh.
        # Waiting the whole bound again would add latency without fresh rows.
        schedule_import(path, importer, owner=owner)
        print(f"snapshotImportSkip code=recent_slow "
              f"duration_ms={round(recent_duration * 1000)} "
              f"bound_ms={round(timeout * 1000)} source_bytes={signature[1]}",
              file=sys.stderr, flush=True)
        return False
    admitted = schedule_import(path, importer, owner=owner, urgent=True)
    code = "unavailable"
    with _cond:
        while epoch == _epoch and not _resetting:
            _, validated_at = _validated_at.get(key, epoch)
            if (_imported.get(key) == signature
                    or (validated_at is not None and validated_at >= started)):
                return True
            if not admitted:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                code = "deadline"
                break
            _cond.wait(remaining)
    # Journal only: synchronous telemetry writes can themselves wait on SQLite
    # and extend the deadline precisely when the importer is under contention.
    elapsed_ms = round((time.monotonic() - started) * 1000)
    print(f"snapshotImportTimeout code={code} wait_ms={elapsed_ms} "
          f"source_bytes={signature[1]}", file=sys.stderr, flush=True)
    return False


def _schedule_deferred(key: str, epoch: int, token: threading.Timer) -> None:
    """Requeue newest dirty work; drain rechecks the owner's current deadline."""
    failed_owner = None
    with _guard:
        entry = _deferred.get(key)
        if _resetting or epoch != _epoch or entry is None or entry[1] is not token:
            return
        request = entry[0]
        del _deferred[key]
        # This key already owns a waiting slot and a reserved cooldown entry.
        _pending[key] = (request, epoch)
        if not _ensure_worker():
            del _pending[key]
            failed_owner = request[2]
    if failed_owner is not None:
        from .log import log
        log("transcriptImportThreadStartFail", f"owner={failed_owner}")


def is_active(path: pathlib.Path, *, owner: str = '') -> bool:
    """Whether the worker holds a request for (owner, path): queued, waiting for
    its window, or running."""
    key = f'{owner}\0{path}'
    with _guard:
        return key in _pending or key in _deferred or (_running is not None and _running[0] == key)


def _drain_background():
    global _worker, _running
    from . import db
    from .log import log, log_exception
    try:
        while True:
            failed = []
            selected = None
            with _guard:
                now = time.monotonic()
                for key, (request, item_epoch) in list(_pending.items()):
                    if _resetting or item_epoch != _epoch:
                        del _pending[key]
                        continue
                    wait = _owner_deadline(key) - now
                    if not request[4] and wait > 0:
                        if not _defer_locked(key, request, item_epoch, wait):
                            failed.append(request[2])
                        del _pending[key]
                        continue
                    del _pending[key]
                    _running = (key, item_epoch)
                    selected = (key, request, item_epoch)
                    break
            for failed_owner in failed:
                log('transcriptImportThreadStartFail', f'owner={failed_owner}')
            if selected is None:
                # Never sleep or hold the worker behind an ineligible owner.
                # Close before clearing: reset must outlive every real thread.
                db.close_local()
                with _guard:
                    if not _pending:
                        _worker = None
                        return
                continue
            key, (path, importer, owner, after, urgent), item_epoch = selected
            started = time.monotonic()
            try:
                try:
                    _import_admitted(path, importer, owner, item_epoch)
                    completed = time.monotonic()
                    with _guard:
                        if _epoch == item_epoch and not _resetting:
                            _last_started[_owner_key(key)] = (
                                completed, max(0.0, completed - started))
                except Exception as exc:
                    log_exception('backgroundTranscriptImportFail', exc, detail=owner)
                else:
                    # Successful unchanged validation counts too; callback
                    # duration/error never changes the completed import window.
                    with _guard:
                        current = _epoch == item_epoch and not _resetting
                    if current and after is not None:
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
