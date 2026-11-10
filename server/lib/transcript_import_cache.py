"""Process-local coordination for importing backend transcript files.

The inotify transcript streamer normally imports changes before a client asks
for /log.  This cache lets /log reuse that SQLite state instead of reparsing an
unchanged JSONL file, while retaining /log as a fallback importer after missed
watch events or process startup.
"""
from __future__ import annotations

import pathlib
import math
import sys
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field

from .revisioned_cache import RevisionedCache

_guard = threading.Lock()
# Registered once by the Host composition root before scheduling. Deliberately
# survives cache/DB resets and repeated server-module imports in this process.
_host_started_at: float | None = None
_STARTUP_GRACE_SECONDS = 120.0
_LARGE_SOURCE_BYTES = 50 * 1024 * 1024


def note_host_started(origin: float) -> bool:
    """Bind the first valid Host-start origin; conflicting later origins lose.

    Runtime-only processes do not register a Host and acquire no startup gate.
    Invalid/future origins are programming errors; no wall-clock coercion.
    """
    global _host_started_at
    if (type(origin) not in (int, float) or not math.isfinite(origin)
            or origin < 0 or origin > time.monotonic()):
        raise ValueError("invalid Host startup monotonic origin")
    with _guard:
        if _host_started_at is not None:
            return False
        _host_started_at = float(origin)
        return True

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
# A timed-out snapshot must not pay its full wait again on every poll.
_snapshot_wait_until: RevisionedCache[str, float] = RevisionedCache(
    "transcript-snapshot-wait-cooldown", max_entries=1024)
_completion_retry_at: RevisionedCache[str, tuple[float, int]] = RevisionedCache(
    "transcript-completion-retry", max_entries=1024)
_completion_tokens: RevisionedCache[str, tuple] = RevisionedCache(
    "transcript-completion-token", max_entries=1024)
_SNAPSHOT_DEADLINE_COOLDOWN = 120.0


@dataclass(frozen=True)
class _DeferredImport:
    """An admitted source did not complete; deliberately distinct from False."""
    reason: str = "pending"


@dataclass
class _CompletedImport:
    """Accepted historical completion; only _guard may claim its after observer.

    Acceptance precedes arbitrary observers. A later authority generation may
    prevent cache publication, but cannot retroactively revoke this completion.
    Observer errors still propagate before successful cache bookkeeping.
    """
    changed: bool
    token: tuple
    key: str
    epoch: int
    value: object = None
    after_claimed: bool = False


@dataclass(frozen=True)
class OwnerImportBinding:
    """Native admission namespace, never a DB or transcript certificate."""
    database_path: str
    backend: str
    backend_session_id: str
    source_file: str | None

    def __post_init__(self):
        for value in (self.database_path, self.backend, self.backend_session_id):
            if type(value) is not str or not value or len(value.encode("utf-8")) > 4096:
                raise ValueError("invalid owner import binding")
        if self.backend != "codex":
            raise ValueError("owner rotation lease requires Codex")
        if self.source_file is not None and (type(self.source_file) is not str
                or not self.source_file or len(self.source_file.encode("utf-8")) > 4096):
            raise ValueError("invalid owner source binding")


class OwnerLeaseBusy(RuntimeError):
    """A conflicting operation or an unsafe lease transition was refused."""


class OwnerLeasePending(TimeoutError):
    def __init__(self, lease):
        super().__init__("owner imports still draining; lease remains held")
        self.lease = lease


@dataclass(frozen=True)
class OwnerDirtySnapshot:
    dirty: bool
    paths: tuple[str, ...]
    urgent: bool
    startup_exempt: bool
    first_wait_rank: float | None


@dataclass(frozen=True)
class FinalOwnerImportReceipt:
    completed: bool
    changed: bool


@dataclass(eq=False)
class OwnerImportLease:
    """Opaque process-local token. The controller owns durable boot fencing."""
    owner: str
    operation_id: str
    binding: OwnerImportBinding
    generation: int
    final_started: bool = False
    final_completed: bool = False
    closed: bool = False
    released_binding: OwnerImportBinding | None = None
    dirty: OwnerDirtySnapshot | None = None


@dataclass
class _OwnerAdmissionState:
    generation: int = 0
    binding: OwnerImportBinding | None = None
    lease: OwnerImportLease | None = None
    active: int = 0
    cleanup_uncertain: bool = False
    dirty: bool = False
    urgent: bool = False
    startup_exempt: bool = False
    first_wait_rank: float | None = None
    threads: dict = field(default_factory=dict)


class _OwnerAdmissions:
    """Bounded protocol state, protected exclusively by the existing _guard.

    Active leases and stale-callback binding fences are never LRU/TTL evicted.
    Idle owners without a binding are removed. Capacity refuses admission;
    neither test reset nor an elapsed deadline releases a real lease.
    """
    def __init__(self):
        self.states = {}
        self.limit = 2048

    def state(self, owner):
        state = self.states.get(owner)
        if state is None:
            if len(self.states) >= self.limit:
                raise OwnerLeaseBusy("owner admission capacity exhausted")
            state = self.states[owner] = _OwnerAdmissionState()
        return state

    def begin(self, owner):
        state = self.state(owner)
        if state.lease is not None and not state.lease.closed:
            return None
        return self.enter(owner, state)

    def enter(self, owner, state):
        ident = threading.get_ident()
        state.active += 1
        state.threads[ident] = state.threads.get(ident, 0) + 1
        return owner, state, state.binding, ident

    def end(self, ticket):
        owner, state, _, ident = ticket
        state.active -= 1
        state.threads[ident] -= 1
        if not state.threads[ident]:
            del state.threads[ident]
        if (not state.active and state.binding is None and state.lease is None
                and not state.cleanup_uncertain):
            self.states.pop(owner, None)
        _cond.notify_all()

    def held(self, owner):
        state = self.states.get(owner)
        return state is not None and state.lease is not None and not state.lease.closed


_owner_admissions = _OwnerAdmissions()


def _binding_matches(owner, binding, path, importer):
    # Descriptors/probes are arbitrary caller code: always outside _guard.
    token = _completion_probe(importer)
    if token is None or len(token[0]) != 5:
        return False
    namespace, source, backend, agent, native = token[0]
    return (namespace == binding.database_path and backend == binding.backend
            and agent == owner and native == binding.backend_session_id
            and source == str(path)
            and (binding.source_file is None or source == binding.source_file))


@contextmanager
def _owner_admission(owner, path, importer):
    with _guard:
        try:
            ticket = _owner_admissions.begin(owner)
        except OwnerLeaseBusy:
            ticket = None
    if ticket is None:
        yield False
        return
    try:
        binding = ticket[2]
        valid = binding is None or _binding_matches(owner, binding, path, importer)
        if valid and binding is not None and binding.source_file is None:
            with _guard:
                state = ticket[1]
                if state.binding == binding:
                    state.binding = OwnerImportBinding(binding.database_path, binding.backend,
                                                       binding.backend_session_id, str(path))
                else:
                    valid = state.binding == OwnerImportBinding(
                        binding.database_path, binding.backend, binding.backend_session_id, str(path))
        yield valid
    finally:
        with _guard:
            _owner_admissions.end(ticket)


def _lease_state(lease):
    state = _owner_admissions.states.get(lease.owner) if type(lease) is OwnerImportLease else None
    if state is None or state.lease is not lease:
        raise OwnerLeaseBusy("unknown or retired owner lease")
    return state


def _owner_deadline_value(deadline):
    if type(deadline) not in (int, float) or not math.isfinite(deadline):
        raise ValueError("invalid owner lease monotonic deadline")
    return deadline


def acquire_owner_lease(agent, expected_binding, deadline, *, operation_id):
    """Block first, then drain; timeout preserves the same operation/token."""
    _owner_deadline_value(deadline)
    if (type(agent) is not str or not agent or len(agent.encode("utf-8")) > 4096
            or type(expected_binding) is not OwnerImportBinding or expected_binding.source_file is None
            or type(operation_id) is not str or not 1 <= len(operation_id) <= 128
            or not operation_id.isascii() or any(ord(c) < 33 or ord(c) > 126 for c in operation_id)):
        raise ValueError("invalid owner lease identity")
    with _guard:
        if _resetting:
            raise OwnerLeaseBusy("cache reset in progress")
        state = _owner_admissions.state(agent)
        old = state.lease
        if old is not None and old.operation_id == operation_id:
            if old.binding != expected_binding:
                raise OwnerLeaseBusy("operation binding changed")
            lease = old
        else:
            if old is not None and not old.closed:
                raise OwnerLeaseBusy("another owner lease is held")
            if state.binding is not None and state.binding != expected_binding:
                raise OwnerLeaseBusy("stale owner binding")
            lease = OwnerImportLease(agent, operation_id, expected_binding, state.generation)
            state.lease = lease
            # Park timers in the existing waiting slots, never in a second queue.
            for key, (request, timer, epoch) in list(_deferred.items()):
                if request[2] == agent:
                    timer.cancel()
                    del _deferred[key]
                    _pending[key] = (request, epoch)
            _cond.notify_all()
    return wait_owner_lease(lease, deadline)


def wait_owner_lease(lease, deadline):
    _owner_deadline_value(deadline)
    with _cond:
        state = _lease_state(lease)
        while state.active:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or threading.get_ident() in state.threads:
                raise OwnerLeasePending(lease)
            _cond.wait(remaining)
            state = _lease_state(lease)
        if state.cleanup_uncertain:
            raise OwnerLeaseBusy("owner connection cleanup is uncertain")
        return lease


def _park_owner_import(path, importer, owner, after=None, urgent=False, startup_exempt=False):
    """No stat/probe/observer on a blocked owner; retain bounded dirty intent."""
    key = f"{owner}\0{path}"
    with _guard:
        if _resetting or not _owner_admissions.held(owner):
            return False
        state = _owner_admissions.states[owner]
        state.dirty = True
        state.urgent |= urgent
        state.startup_exempt |= startup_exempt
        if state.first_wait_rank is None:
            state.first_wait_rank = time.monotonic()
        queued = _pending.get(key) or _deferred.get(key)
        waiting = set(_pending) | set(_deferred)
        if _running is not None and _running[2]:
            waiting.add(_running[0])
        if key not in waiting and len(waiting) >= _BACKGROUND_LIMIT:
            return False
        old = queued[0] if queued else None
        request = (path, importer, owner, after if after is not None else old[3] if old else None,
                   urgent or bool(old and old[4]), old[5] if old else time.monotonic(),
                   startup_exempt or bool(old and old[6]), old[7] if old else 0)
        if key in _deferred:
            _, timer, _ = _deferred.pop(key)
            timer.cancel()
        _pending[key] = (request, _epoch)
        return True


def run_final_owner_import(lease, path, importer, *, after=None):
    """One explicit old import, counted through observers; never auto-replayed."""
    # Validate the token/actual binding before reserving its one final attempt.
    with _guard:
        state = _lease_state(lease)
        if lease.closed or state.active or state.cleanup_uncertain or lease.final_started:
            raise OwnerLeaseBusy("owner lease not drained or final import already attempted")
        ticket = _owner_admissions.enter(lease.owner, state)
    try:
        if not _binding_matches(lease.owner, lease.binding, path, importer):
            raise OwnerLeaseBusy("final importer does not match old binding")
        with _guard:
            if _resetting:
                raise OwnerLeaseBusy("cache reset in progress")
            lease.final_started = True
            epoch = _epoch
        result = _import_admitted(path, importer, lease.owner, epoch)
        complete = isinstance(result, _CompletedImport)
        if complete and after is not None:
            after()
        with _guard:
            lease.final_completed = complete
        return FinalOwnerImportReceipt(complete, result.changed if complete else False)
    finally:
        with _guard:
            _owner_admissions.end(ticket)


def release_owner_lease(lease, new_binding):
    """Controller cutover/verified rollback only; fence old callbacks first."""
    if type(new_binding) is not OwnerImportBinding:
        raise ValueError("invalid released binding")
    with _guard:
        state = _lease_state(lease)
        if lease.closed:
            if lease.released_binding != new_binding:
                raise OwnerLeaseBusy("released binding changed")
            return lease.dirty
        if state.active or state.cleanup_uncertain:
            raise OwnerLeaseBusy("owner lease still draining or cleanup uncertain")
        if (new_binding.database_path != lease.binding.database_path
                or new_binding.backend != lease.binding.backend):
            raise OwnerLeaseBusy("owner namespace cannot change")
        if new_binding != lease.binding and not lease.final_completed:
            raise OwnerLeaseBusy("cutover requires completed final old import")
        # Retain scalar dirty work only. Old closures never run under new UUIDs.
        requests = [entry[0] for entry in (*_pending.values(), *_deferred.values())
                    if entry[0][2] == lease.owner]
        ranks = [r[5] for r in requests]
        if state.first_wait_rank is not None:
            ranks.append(state.first_wait_rank)
        dirty = OwnerDirtySnapshot(state.dirty or bool(requests),
                                   tuple(str(r[0]) for r in requests),
                                   state.urgent or any(r[4] for r in requests),
                                   state.startup_exempt or any(r[6] for r in requests),
                                   min(ranks, default=None))
        prefix = lease.owner + "\0"
        keys = {k for k in (*_imported, *_pending, *_deferred) if k.startswith(prefix)}
        for key in keys:
            _imported.pop(key, None)
            _validated_at.pop(key)
            _last_import_duration.pop(key)
            _snapshot_wait_until.pop(key)
            _completion_retry_at.pop(key)
            _completion_tokens.pop(key)
            _pending.pop(key, None)
            deferred = _deferred.pop(key, None)
            if deferred is not None:
                deferred[1].cancel()
        _last_started.pop(lease.owner, None)
        state.generation += 1
        state.binding = new_binding
        lease.released_binding, lease.dirty, lease.closed = new_binding, dirty, True
        _cond.notify_all()
        # Existing unrelated pending work may have lost its sole worker.
        if _pending:
            _ensure_worker()
        return dirty


def _completion_fence(importer):
    """Native audit owners may fence a probe/publication, never importer IO."""
    if getattr(importer, "_clarp_transcript_completion_v1", None) is not True:
        return nullcontext()
    factory = getattr(importer, "_clarp_transcript_fence", None)
    return factory() if factory is not None else nullcontext()


def _completion_probe(importer):
    """Eligibility for a shortcut, not a file, DB or effects certificate.

    Only explicit source-aware callables participate. Legacy callback results
    (including None/zero/False/dicts) remain completely uninterpreted.
    """
    if getattr(importer, "_clarp_transcript_completion_v1", None) is not True:
        return None
    probe = getattr(importer, "_clarp_transcript_eligibility", None)
    token = probe() if callable(probe) else None
    if (not isinstance(token, tuple) or len(token) != 3
            or not isinstance(token[0], tuple) or type(token[2]) is not bool):
        return ((), None, False)
    return token


def _completion_current(token, current) -> bool:
    return token is None and current is None or (
        token is not None and current is not None
        and token[2] and current[2] and token[:2] == current[:2])


@contextmanager
def _completion_guard(importer):
    with _completion_fence(importer):
        token = _completion_probe(importer)
        with _guard:
            yield token


def _completion_result(value, token):
    if token is None:
        return True  # generic callback: retain the legacy normal-return contract
    if not isinstance(value, dict):
        return _DeferredImport("invalid_result")
    result = value.get("_clarp_transcript_completion_v1")
    if not isinstance(result, tuple) or len(result) != 3 or result[:2] != token[:2]:
        return _DeferredImport("invalid_result")
    if result[2] == "completed-changed":
        return True
    if result[2] == "completed-unchanged":
        return False
    return _DeferredImport("pending" if result[2] == "deferred" else "invalid_result")


def _completion_matches(key, token, epoch):
    return token is None or (
        token[2] and _completion_tokens.get(key, epoch) == (True, token[:2]))


def _record_completion_token(key, token, epoch):
    if token is None:
        _completion_tokens.pop(key)  # a generic callback is not audit authority
    else:
        _completion_tokens.put(key, epoch, token[:2])


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


def _import_admitted(path: pathlib.Path, importer: Callable[[], object], owner: str,
                     epoch: int) -> bool | _DeferredImport | _CompletedImport:
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
        with _completion_fence(importer):
            token = _completion_probe(importer)  # outside the cache guard
            if token is not None and not token[2]:
                return _DeferredImport()
            with _guard:
                if _imported.get(key) == before and _completion_matches(key, token, epoch):
                    if _epoch == epoch:
                        _validated_at.put(key, epoch, observed_at)
                        _record_completion_token(key, token, epoch)
                        _completion_retry_at.pop(key)
                        _cond.notify_all()
                    return False if token is None else _CompletedImport(False, token, key, epoch)
        # A busy writer must not cost the whole import; imports are idempotent.
        from . import db
        if token is None:
            value = db.retry_locked(importer)
        else:
            from .transcript_incremental import _call_in_cache_epoch
            value = db.retry_locked(lambda: _call_in_cache_epoch(importer, epoch))
        result = _completion_result(value, token)
        with _completion_fence(importer):
            current = _completion_probe(importer)
            if isinstance(result, _DeferredImport) or not _completion_current(token, current):
                return result if isinstance(result, _DeferredImport) else _DeferredImport("superseded")
            with _guard:
                if token is not None and (_epoch != epoch or _resetting):
                    return _DeferredImport("reset")
                # The acceptance linearization point. No observer has run yet;
                # the receipt remains historical even if its observer sees a
                # subsequent generation. It grants no future shortcut itself.
                accepted = (None if token is None else
                            _CompletedImport(result, token, key, epoch, value))
        if accepted is not None:
            on_completed = getattr(importer, "_clarp_transcript_completed", None)
            if on_completed is not None:
                # Neither native authority nor cache guard spans arbitrary IO.
                # Failure propagates with no successful cache publication.
                on_completed(value)
        with _completion_fence(importer):
            current = _completion_probe(importer)
            with _guard:
                # Keep the chosen version, not bytes appended during import.
                # An accepted old generation is still completed, but never
                # supplies fresh validation for the new generation.
                if (_epoch == epoch and not _resetting
                        and _completion_current(token, current)):
                    _imported[key] = before
                    _validated_at.put(key, epoch, observed_at)
                    _record_completion_token(key, token, epoch)
                    _completion_retry_at.pop(key)
                    state = _owner_admissions.states.get(owner)
                    if state is not None and not _owner_admissions.held(owner):
                        state.dirty = state.urgent = state.startup_exempt = False
                        state.first_wait_rank = None
                    _cond.notify_all()
        return result if accepted is None else accepted


def _import_if_changed_unleased(path: pathlib.Path, importer: Callable[[], object],
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
        result = _import_admitted(path, importer, owner, epoch)
        if isinstance(result, _DeferredImport):
            # Direct requests may serve their existing committed snapshot.
            # Completion is retried by the same bounded owner/path scheduler.
            schedule_import(path, importer, owner=owner, startup_exempt=True)
            return False
        return result.changed if isinstance(result, _CompletedImport) else result
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
# Third field reserves one waiting slot for an opted-in running request that
# may return deferred. Generic callbacks retain their existing waiting capacity.
_running: tuple[str, int, bool] | None = None
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
                     and _running is None and _direct_active == 0
                     and not any(v.active or (v.lease is not None and not v.lease.closed)
                                 for v in _owner_admissions.states.values()))
            if quiet:
                _owner_admissions.states.clear()
                _path_locks.clear()
                _imported.clear()
                _validated_at.reset()
                _last_import_duration.reset()
                _snapshot_wait_until.reset()
                _completion_retry_at.reset()
                _completion_tokens.reset()
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
# A request is (path, importer, owner, after, urgent, first_wait_rank,
# startup_exempt, observed_source_bytes): ``after`` runs on the worker once
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


def _background_interval(duration: float) -> float:
    if duration > 5.0:
        return min(300.0, max(_BACKGROUND_MIN_INTERVAL, 6 * duration))
    return max(_BACKGROUND_MIN_INTERVAL, 2 * duration)


def _owner_deadline(key: str) -> float:
    completed, duration = _last_started.get(_owner_key(key), (None, 0.0))
    if completed is None:
        return 0.0
    return completed + _background_interval(duration)


def _completion_deadline(key: str) -> float:
    _, retry = _completion_retry_at.get(key, _epoch)
    return retry[0] if retry is not None else 0.0


def _request_deadline(key: str, request: tuple, *, size: int | None = None) -> float:
    # Urgency bypasses the ordinary owner/startup window, never completion
    # refusal pacing. Timers, coalescing, promotion and drain share this policy.
    deadline = _completion_deadline(key)
    if not request[4]:
        deadline = max(deadline, _owner_deadline(key))
    if (_host_started_at is not None and not request[4] and not request[6]
            and (request[7] if size is None else size) >= _LARGE_SOURCE_BYTES):
        deadline = max(deadline, _host_started_at + _STARTUP_GRACE_SECONDS)
    return deadline


def _reserve_owner(key: str, now: float) -> bool:
    """Under _guard; never evict an unexpired or admitted owner's window."""
    active = {_owner_key(k) for k in (*_pending, *_deferred)}
    if _running is not None:
        active.add(_owner_key(_running[0]))
    for owner, (completed, duration) in list(_last_started.items()):
        if owner not in active and (completed is None or
                completed + _background_interval(duration) <= now):
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
    old = _deferred.get(key)
    if old is not None and _request_deadline(key, request) >= _request_deadline(key, old[0]):
        _deferred[key] = (request, old[1], old[2])
        return True
    # Explicit user provenance (or a source shrinking below the threshold)
    # can remove a startup-only wait while preserving the786 owner deadline.
    timer = threading.Timer(wait, _schedule_deferred)
    timer.args = (key, epoch, timer)
    timer.daemon = True
    if not _start_tracked(timer):
        if old is not None:
            _deferred[key] = (request, old[1], old[2])  # newest intent, old live wake
        return False
    if old is not None:
        old[1].cancel()
    _deferred[key] = (request, timer, epoch)
    return True


def _rearm_owner_locked(key: str, now: float) -> list[str]:
    """A successful faster import can shorten every path's owner deadline."""
    failures = []
    owner = _owner_key(key)
    for waiting_key, entry in list(_deferred.items()):
        if _owner_key(waiting_key) != owner:
            continue
        request, timer, epoch = entry
        del _deferred[waiting_key]
        if _defer_locked(waiting_key, request, epoch,
                         max(0.0, _request_deadline(waiting_key, request) - now)):
            timer.cancel()
        else:
            # Preserve the original retry/callback if a replacement timer
            # cannot start; the dirty version is never marked covered here.
            _deferred[waiting_key] = entry
            failures.append(request[2])
    return failures


def _schedule_import_unleased(path: pathlib.Path, importer: Callable[[], object], *, owner: str = '',
                    after: Callable[[], None] | None = None, urgent: bool = False,
                    startup_exempt: bool = False) -> bool:
    """Coalesce normal work by agent; urgent RM-N12 requests bypass at drain.

    Dirty signatures, locks and latest-winner callbacks remain per owner/path.
    Every successful validation opens an adaptive completion-based normal window.
    False means refused: the source version remains unread and retryable.
    """
    key = f'{owner}\0{path}'
    signature = _signature(path)
    full = start_failed = False
    with _completion_guard(importer) as eligible:
        if _resetting:
            return False
        if eligible is not None and eligible[2]:
            # An explicit ready admission can rearm pending work. This only
            # removes retry pacing; it neither settles audit debt nor completes
            # the import, its callbacks or the existing 786 owner window.
            _completion_retry_at.pop(key)
        queued = _pending.get(key) or _deferred.get(key)
        if after is None:
            if (_imported.get(key) == signature
                    and _completion_matches(key, eligible, _epoch)):
                return False
            after = queued[0][3] if queued else None
        # Once requested, urgency belongs to the queued token, not to whichever
        # normal request last supplied the newest importer/callback.
        urgent = urgent or bool(queued and queued[0][4])
        now = time.monotonic()
        # Keep the first accepted wait rank through coalescing and timers.
        # A hot path's new callback request goes behind an older waiting path.
        rank = queued[0][5] if queued else now
        # The exemption belongs only to this owner/path's queued user intent;
        # it does not grant urgency or bypass the ordinary owner backoff.
        startup_exempt = startup_exempt or bool(queued and queued[0][6])
        request = (path, importer, owner, after, urgent, rank, startup_exempt, signature[1])
        waiting = set(_pending) | set(_deferred)
        if _running is not None and _running[2]:
            waiting.add(_running[0])
        if key not in waiting and len(waiting) >= _BACKGROUND_LIMIT:
            full = True
        elif not _reserve_owner(key, now):
            full = True
        elif key not in _pending and (wait := _request_deadline(key, request) - now) > 0:
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


def _import_for_snapshot_unleased(path: pathlib.Path, importer: Callable[[], object], *,
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
    with _completion_guard(importer) as token:
        epoch = _epoch
        if (not _resetting and _imported.get(key) == signature
                and _completion_matches(key, token, epoch)):
            return True
        _, recent_duration = _last_import_duration.get(key, epoch)
        _, wait_until = _snapshot_wait_until.get(key, epoch)
    if wait_until is not None and started < wait_until:
        # Ordinary scheduling preserves dirty work/callbacks without renewing
        # urgency or the fixed cooldown on every snapshot poll.
        schedule_import(path, importer, owner=owner, startup_exempt=True)
        print(f"snapshotImportSkip code=deadline_cooldown "
              f"source_bytes={signature[1]}", file=sys.stderr, flush=True)
        return False
    if recent_duration is not None and recent_duration > timeout:
        # Known slow histories retain today's asynchronous/coalesced refresh.
        # Waiting the whole bound again would add latency without fresh rows.
        schedule_import(path, importer, owner=owner, startup_exempt=True)
        print(f"snapshotImportSkip code=recent_slow "
              f"duration_ms={round(recent_duration * 1000)} "
              f"bound_ms={round(timeout * 1000)} source_bytes={signature[1]}",
              file=sys.stderr, flush=True)
        return False
    admitted = schedule_import(path, importer, owner=owner, urgent=True)
    code = "unavailable"
    while True:
        locked = False
        try:
            # Acquire the native authority fence before the cache guard. Drop
            # that fence before waiting, but retain the guard until wait()
            # atomically releases it: no lost completion/reset notification.
            with _completion_fence(importer):
                token = _completion_probe(importer)
                _cond.acquire()
                locked = True
                if epoch != _epoch or _resetting or _owner_admissions.held(owner):
                    break
                _, validated_at = _validated_at.get(key, epoch)
                if (_completion_matches(key, token, epoch)
                        and (_imported.get(key) == signature
                             or (validated_at is not None and validated_at >= started))):
                    return True
                if not admitted:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    code = "deadline"
                    _snapshot_wait_until.put(
                        key, epoch, time.monotonic() + _SNAPSHOT_DEADLINE_COOLDOWN)
                    break
            _cond.wait(remaining)
        finally:
            if locked:
                _cond.release()
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
        if not _owner_admissions.held(request[2]) and not _ensure_worker():
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


def _retry_completion(key, request, epoch):
    """Keep one bounded, paced owner/path intent; never a successful window."""
    from .log import log
    failed = False
    with _guard:
        if _resetting or epoch != _epoch:
            return
        newer = _pending.get(key) or _deferred.get(key)
        if newer is not None:
            latest = newer[0]
            request = (*latest[:3], request[3] if latest[3] is None else latest[3],
                       latest[4] or request[4], min(latest[5], request[5]),
                       latest[6] or request[6], latest[7])
        _, previous = _completion_retry_at.get(key, epoch)
        attempts = min(7, (previous[1] if previous else 0) + 1)
        wait = min(60.0, 2.0 ** (attempts - 1))
        _completion_retry_at.put(key, epoch, (time.monotonic() + wait, attempts))
        if key in _pending or _owner_admissions.held(request[2]):
            _pending[key] = (request, epoch)
        elif not _defer_locked(key, request, epoch,
                               max(0.0, _request_deadline(key, request) - time.monotonic())):
            failed = True  # no freshness; the source remains refused/retryable
    if failed:
        log('transcriptImportThreadStartFail', f'owner={request[2]}')


def _finish_background_import(key, request, epoch, result, started, completed):
    from .log import log, log_exception
    importer, owner, after = request[1:4]
    if isinstance(result, _DeferredImport):
        return False
    accepted = isinstance(result, _CompletedImport)
    if not accepted and getattr(importer, "_clarp_transcript_completion_v1", None) is True:
        return False
    with _guard:
        if epoch != _epoch or _resetting:
            return True  # reset owns old work; never requeue across epochs
        if accepted:
            if result.key != key or result.epoch != epoch:
                return False
            if result.after_claimed:
                return True
            result.after_claimed = True
        imported = result.changed if accepted else result
        previous_deadline = _owner_deadline(key)
        previous_cost = _last_started.get(_owner_key(key), (None, 0.0))[1]
        cost = max(0.0, completed - started) if imported else previous_cost
        _last_started[_owner_key(key)] = (completed, cost)
        failed = (_rearm_owner_locked(key, completed)
                  if _owner_deadline(key) < previous_deadline else [])
        if imported:
            _last_import_duration.put(key, epoch, max(0.0, completed - started))
    for failed_owner in failed:
        log('transcriptImportThreadStartFail', f'owner={failed_owner}')
    if after is not None:
        try:
            # One accepted observer, outside both cache/native authority locks.
            # Its binding/intent guards still decide which client work applies.
            after()
        except Exception as exc:
            log_exception('backgroundTranscriptAfterFail', exc, detail=owner)
    return True


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
                # Due timer threads may get CPU in any order. Promote all due
                # keys here so an older path cannot lose its place merely
                # because its timer callback ran after a hotter path's.
                for key, (request, timer, item_epoch) in list(_deferred.items()):
                    if _request_deadline(key, request) <= now:
                        timer.cancel()
                        del _deferred[key]
                        _pending[key] = (request, item_epoch)
                waiting = sorted(_pending.items(), key=lambda item: item[1][0][5])
            for key, (request, item_epoch) in waiting:
                # Retain the waiting slot/rank while stat and user marker
                # descriptors run outside _guard. Either may reenter/replace.
                # Revalidate the exact request token and epoch before consuming
                # it: a replacement/reset during stat must keep its own intent.
                capacity_refused = False
                with _guard:
                    try:
                        ticket = _owner_admissions.begin(request[2])
                    except OwnerLeaseBusy:
                        ticket = None
                        capacity_refused = True
                        current = _pending.get(key)
                        if current is not None and current[0] is request:
                            del _pending[key]
                if capacity_refused:
                    log("transcriptImportQueueFull", f"owner={request[2]}")
                if ticket is None:
                    continue
                size = request[7]
                selection_error = None
                reserves_retry = False
                try:
                    if ticket[2] is not None and not _binding_matches(
                            request[2], ticket[2], request[0], request[1]):
                        raise OwnerLeaseBusy("stale worker binding")
                    reserves_retry = (
                        getattr(request[1], "_clarp_transcript_completion_v1", None) is True)
                    if (_host_started_at is not None and not request[4] and not request[6]
                            and time.monotonic() < _host_started_at + _STARTUP_GRACE_SECONDS):
                        size = source_size(request[0])
                except Exception as exc:
                    selection_error = exc
                except BaseException:
                    with _guard:
                        _owner_admissions.end(ticket)
                    raise
                with _guard:
                    current = _pending.get(key)
                    if current is None or current[0] is not request or current[1] != item_epoch:
                        _owner_admissions.end(ticket)
                        continue
                    if _resetting or item_epoch != _epoch:
                        del _pending[key]
                        _owner_admissions.end(ticket)
                        continue
                    if selection_error is not None:
                        del _pending[key]
                        _owner_admissions.end(ticket)
                    else:
                        now = time.monotonic()
                        request = (*request[:7], size)
                        wait = _request_deadline(key, request) - now
                        if wait > 0:
                            if not _defer_locked(key, request, item_epoch, wait):
                                failed.append(request[2])
                            del _pending[key]
                            _owner_admissions.end(ticket)
                            continue
                        del _pending[key]
                        _running = (key, item_epoch, reserves_retry)
                        selected = (key, request, item_epoch, ticket)
                if selection_error is not None:
                    log_exception('backgroundTranscriptImportFail', selection_error, detail=request[2])
                if selected is not None:
                    break
            for failed_owner in failed:
                log('transcriptImportThreadStartFail', f'owner={failed_owner}')
            if selected is None:
                # Never sleep or hold the worker behind an ineligible owner.
                # Close before clearing: reset must outlive every real thread.
                db.close_local()
                with _guard:
                    if not any(not _owner_admissions.held(entry[0][2])
                               for entry in _pending.values()):
                        _worker = None
                        return
                continue
            key, (path, importer, owner, after, urgent, rank, startup_exempt, size), item_epoch, ticket = selected
            started = time.monotonic()
            try:
                try:
                    imported = _import_admitted(path, importer, owner, item_epoch)
                    completed = time.monotonic()
                    if not _finish_background_import(
                            key, (path, importer, owner, after, urgent, rank, startup_exempt, size),
                            item_epoch, imported, started, completed):
                        _retry_completion(
                            key, (path, importer, owner, after, urgent, rank, startup_exempt, size),
                            item_epoch)
                except Exception as exc:
                    log_exception('backgroundTranscriptImportFail', exc, detail=owner)
            finally:
                try:
                    db.close_local()
                except BaseException:
                    with _guard:
                        ticket[1].cleanup_uncertain = True
                    raise
                finally:
                    with _guard:
                        _running = None
                        _owner_admissions.end(ticket)
                        _cond.notify_all()
    finally:
        try:
            db.close_local()
        finally:
            with _guard:
                if _worker is threading.current_thread():
                    _worker = None
                _cond.notify_all()


def wait_for_background(timeout: float = 5) -> bool:
    with _guard:
        worker = _worker
    if worker:
        worker.join(timeout)
        return not worker.is_alive()
    return True


def import_if_changed(path, importer, *, owner=""):
    with _owner_admission(owner, path, importer) as admitted:
        if not admitted:
            _park_owner_import(path, importer, owner, startup_exempt=True)
            return False
        return _import_if_changed_unleased(path, importer, owner=owner)


def schedule_import(path, importer, *, owner="", after=None, urgent=False, startup_exempt=False):
    with _owner_admission(owner, path, importer) as admitted:
        if not admitted:
            return _park_owner_import(path, importer, owner, after, urgent, startup_exempt)
        return _schedule_import_unleased(path, importer, owner=owner, after=after,
                                         urgent=urgent, startup_exempt=startup_exempt)


def import_for_snapshot(path, importer, *, owner, timeout):
    with _owner_admission(owner, path, importer) as admitted:
        if not admitted:
            _park_owner_import(path, importer, owner, startup_exempt=True)
            return False
        return _import_for_snapshot_unleased(path, importer, owner=owner, timeout=timeout)
