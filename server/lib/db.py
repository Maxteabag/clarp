"""SQLite-backed source of truth for agent/runtime/turn/clip state.

Replaces the previous file-marker state and ~/.config/clarp/agents.json
with a single ACID store at ~/.local/share/clarp/state.sqlite.

Schema is created on first open. user_version drives migrations: to change the
schema, edit _SCHEMA_SQL in db_schema.py, bump _SCHEMA_VERSION there, and add a
`_migrate_to_vN` in db_migrations.py that upgrades an existing database (see
`_migrate`). Both stay reachable as `db._SCHEMA_SQL`, `db._migrate_to_vN` etc.

Two things that are easy to get wrong:

* `tests/unit/test_db_migrations.py` builds an old database by *undoing* the
  current schema (`_shape_as_v61`). A new column therefore needs a matching
  `DROP COLUMN` there, or every migration test fails on a duplicate column.
* If two branches bump to the same version, both define `_migrate_to_vN` and
  Python keeps only the last one — the other migration silently becomes dead
  code and never runs. Merge the two bodies into one function rather than
  renumbering, and guard each `ALTER` with a `PRAGMA table_info` check so a
  re-run is a no-op instead of a crash. Merging is not enough on its own: any
  database already stamped with the colliding version skips the merged
  function entirely, so the guarded adds have to be repeated one version
  later to reach it (see `_migrate_to_v69`).

The hooks and the server both use this module — they share the file via
WAL mode + a short busy_timeout. Concurrent writes serialise without
losing rows. Boot recovery raises that timeout on its own connection so an
exclusive WAL checkpoint in the HTTP process cannot skip INTERRUPTED marks.
"""
from __future__ import annotations

import itertools
import os
import pathlib
import sqlite3
import sys
import threading
import time
import traceback
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TypeVar

from .timing import (
    SQLITE_BUSY_TIMEOUT_MS,
    SQLITE_CONNECT_TIMEOUT_SEC,
    SQLITE_LOCK_RETRIES,
    SQLITE_LOCK_RETRY_SLEEP_SEC,
)
from . import xdg
from .clock import now_ms  # noqa: F401  (re-exported as db.now_ms)


DB_PATH = pathlib.Path(os.environ.get(
    "CLAUDE_PWA_DB",
    str(xdg.data_dir() / "state.sqlite"),
))

_LOCAL = threading.local()  # per-thread connection store
_CONN_LOCK = threading.Lock()
_MIGRATED = False


_LOCK_REPORT_INTERVAL_SEC = 30.0
_TRANSACTION_LOCK = threading.Lock()


@dataclass(frozen=True)
class _TransactionOwner:
    connection_id: int
    thread_id: int
    thread_name: str
    started_monotonic: float
    begin_sql: str
    last_sql: str
    stack: tuple[str, ...]
    # Opened by a transcript import's write batch (RM-N7; the long-wait log).
    is_import: bool = False
    # Confirmation time, not BEGIN time. For a deferred DML upgrade it is a
    # lower bound: SQLite acquired the writer sometime before execute returned.
    write_started_monotonic: float | None = None


_TRANSACTION_OWNERS: dict[int, _TransactionOwner] = {}
_LAST_LOCK_REPORT_AT = 0.0


def begin_request_metrics(*, enabled: bool) -> None:
    _LOCAL.request_metrics = ({"query_count": 0, "sqlite_ms": 0.0,
                               "max_query_ms": 0.0, "max_query": "",
                               "query_templates": {}}
                              if enabled else None)


def finish_request_metrics() -> dict:
    value = getattr(_LOCAL, "request_metrics", None)
    _LOCAL.request_metrics = None
    if not value:
        return {}
    templates = value.pop("query_templates", {})
    repeated = sorted(
        ({"count": count, "sql": sql} for sql, count in templates.items()
         if count >= 5), key=lambda row: row["count"], reverse=True)[:5]
    if repeated:
        value["repeated_queries"] = repeated
    return dict(value)


def _record_query_metric(sql: object, started: float | None) -> None:
    metrics = getattr(_LOCAL, "request_metrics", None)
    if metrics is None or started is None:
        return
    duration = (time.perf_counter() - started) * 1000
    metrics["query_count"] += 1
    metrics["sqlite_ms"] += duration
    if duration > metrics["max_query_ms"]:
        metrics["max_query_ms"] = duration
        metrics["max_query"] = _sql_template(sql)
    templates = metrics["query_templates"]
    template = _sql_template(sql)
    if template in templates or len(templates) < 128:
        templates[template] = templates.get(template, 0) + 1


def _sql_template(sql: object) -> str:
    """Return a bounded SQL template without ever including bound values."""
    compact = " ".join(str(sql).split())
    return compact[:240]


def _record_transaction_begin(connection: sqlite3.Connection, sql: object,
                              *, write_confirmed: bool = False) -> None:
    template = _sql_template(sql)
    now = time.monotonic()
    owner = _TransactionOwner(
        connection_id=id(connection),
        thread_id=threading.get_ident(),
        thread_name=threading.current_thread().name,
        started_monotonic=now,
        begin_sql=template,
        last_sql=template,
        stack=tuple(traceback.format_stack(limit=14)[:-1]),
        is_import=bool(getattr(_LOCAL, "import_batch", False)),
        write_started_monotonic=(now if write_confirmed or template.upper().startswith(_LOCK_BEGINS) else None),
    )
    with _TRANSACTION_LOCK:
        _TRANSACTION_OWNERS[id(connection)] = owner


def _record_transaction_statement(connection: sqlite3.Connection, sql: object) -> None:
    with _TRANSACTION_LOCK:
        owner = _TRANSACTION_OWNERS.get(id(connection))
        if owner is not None:
            _TRANSACTION_OWNERS[id(connection)] = _TransactionOwner(
                **{**owner.__dict__, "last_sql": _sql_template(sql)}
            )


def _record_transaction_admission(connection: sqlite3.Connection, sql: object) -> None:
    """Confirm only successful explicit BEGINs or writes, before logging.

    A deferred BEGIN/SAVEPOINT is not proof of a writer. A successful DML
    inside it confirms the writer by statement return, never at attempted SQL.
    """
    if not connection.in_transaction:
        return
    template = _sql_template(sql)
    verb = template.partition(" ")[0].upper()
    with _TRANSACTION_LOCK:
        owner = _TRANSACTION_OWNERS.get(id(connection))
    if verb in {"BEGIN", "SAVEPOINT"}:
        if owner is None:
            _record_transaction_begin(connection, template)
    elif verb in _WRITE_VERBS:
        if owner is None:
            _record_transaction_begin(connection, template, write_confirmed=True)
        elif owner.write_started_monotonic is None:
            with _TRANSACTION_LOCK:
                _TRANSACTION_OWNERS[id(connection)] = _TransactionOwner(
                    **{**owner.__dict__, "write_started_monotonic": time.monotonic()})


def _clear_transaction_owner(connection: sqlite3.Connection) -> None:
    with _TRANSACTION_LOCK:
        _TRANSACTION_OWNERS.pop(id(connection), None)
    # Every tracked transaction end passes here: write the telemetry rows this
    # thread's emits queued while it held the transaction (RM-N8b).
    if not thread_holds_transaction():
        from .log import flush_deferred
        flush_deferred()


def thread_holds_transaction() -> bool:
    """Whether this thread's connection is inside a transaction now."""
    connection = getattr(_LOCAL, "conn", None)
    try:
        return connection is not None and connection.in_transaction
    except sqlite3.ProgrammingError:  # the tracked close has released the transaction
        return False


def _report_database_locked(waiting_sql: object) -> None:
    """Log transaction owners to stderr without touching SQLite recursively."""
    global _LAST_LOCK_REPORT_AT
    if getattr(_LOCAL, "busy_expected", False):
        # A best_effort_write() gave up on purpose. Reporting it would also
        # spend the shared throttle window and hide a real wait behind it.
        return
    now = time.monotonic()
    waiting = _sql_template(waiting_sql)
    with _TRANSACTION_LOCK:
        transactions = tuple(_TRANSACTION_OWNERS.values())
        owners = tuple(o for o in transactions if o.write_started_monotonic is not None)
        snapshots = tuple(o for o in transactions if o.write_started_monotonic is None)
        if now - _LAST_LOCK_REPORT_AT < _LOCK_REPORT_INTERVAL_SEC:
            return
        _LAST_LOCK_REPORT_AT = now
    waiter = threading.current_thread()
    print(
        "sqlite_lock_wait "
        f"waiter_thread={waiter.name!r} waiter_ident={threading.get_ident()} "
        f"sql={waiting!r} owner_count={len(owners)} snapshot_count={len(snapshots)}",
        file=sys.stderr,
        flush=True,
    )
    for owner in (*owners, *snapshots):
        confirmed = owner.write_started_monotonic is not None
        event = "sqlite_lock_owner" if confirmed else "sqlite_lock_snapshot"
        write_age = (f"write_age_sec={now - owner.write_started_monotonic:.3f} "
                     if confirmed else "state=read_or_deferred ")
        stack = "".join(owner.stack).replace("\n", "\\n")
        print(
            f"{event} "
            f"connection={owner.connection_id} thread={owner.thread_name!r} "
            f"thread_ident={owner.thread_id} age_sec={now - owner.started_monotonic:.3f} "
            f"{write_age}"
            f"begin_sql={owner.begin_sql!r} last_sql={owner.last_sql!r} "
            f"stack={stack!r}",
            file=sys.stderr,
            flush=True,
        )


# Bumped on every write statement executed in this process (the Host runs
# SQLite in autocommit mode, so commit() is not a reliable signal). On its own
# it is blind to the six hook scripts and the runtime process that share
# state.sqlite, so cache readers combine it with SQLite's own cross-process
# change counter through `change_stamp()` below.
_WRITE_GENERATION = 0
_WRITE_VERBS = frozenset({"INSERT", "UPDATE", "DELETE", "REPLACE"})
_WRITE_GENERATION_LOCK = threading.Lock()


def _bump_write_generation() -> None:
    global _WRITE_GENERATION
    with _WRITE_GENERATION_LOCK:
        _WRITE_GENERATION += 1


def write_generation() -> int:
    return _WRITE_GENERATION


# One process-wide connection whose only job is `PRAGMA data_version`. SQLite
# keeps that counter per connection and bumps it when *another* connection
# commits, so reading it from a connection nobody writes through makes every
# commit in the process, the hooks and the runtime visible as one monotonic
# value. Reading it from the per-thread request connections would not work:
# each has its own count and a write on the same connection leaves it flat.
_STAMP_LOCK = threading.Lock()
_STAMP_CONN: sqlite3.Connection | None = None
_STAMP_FAILURES = itertools.count(1)


def _stamp_connection() -> sqlite3.Connection:
    global _STAMP_CONN
    if _STAMP_CONN is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(DB_PATH), timeout=SQLITE_CONNECT_TIMEOUT_SEC,
                              isolation_level=None, check_same_thread=False)
        con.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
        _STAMP_CONN = con
    return _STAMP_CONN


def _close_stamp_connection() -> None:
    global _STAMP_CONN
    if _STAMP_CONN is not None:
        try:
            _STAMP_CONN.close()
        except sqlite3.Error:
            pass
        _STAMP_CONN = None


def change_stamp() -> tuple[int, int]:
    """A value that differs whenever state.sqlite may have changed.

    ``(data_version, write_generation)``: the first half moves when any other
    connection - another thread, a hook process, the runtime - commits; the
    second moves on every write statement this process issues, which also
    covers a thread reading its own uncommitted rows inside BEGIN IMMEDIATE.
    Cache entries keyed by this stamp can never outlive a write, and a quiet
    Host still answers from memory.
    """
    with _STAMP_LOCK:
        try:
            row = _stamp_connection().execute("PRAGMA data_version").fetchone()
            data_version = int(row[0]) if row else 0
        except sqlite3.Error:
            # Unknown is never equal to anything cached: a fresh negative
            # value per failure keeps readers recomputing until it recovers.
            _close_stamp_connection()
            data_version = -next(_STAMP_FAILURES)
        return data_version, _WRITE_GENERATION


# RM-N7. SQLite's busy handler sleeps 1, 2, 5, ... 50 then 100 ms between
# attempts, so a waiter on its long steps keeps missing a transcript import's
# short gaps and can wait out the whole import. A statement that only acquires
# the write lock (and has done nothing when it fails with BUSY) is retried here
# every few milliseconds instead, up to the same deadline, so it gets in at the
# next gap. Statements inside a transaction keep SQLite's own handling.
_LOCK_POLL_SECONDS = 0.002
_LONG_WRITE_WAIT_SECONDS = 0.25
_LOCK_BEGINS = ("BEGIN IMMEDIATE", "BEGIN EXCLUSIVE")
def note_import_holds_lock(holding: bool) -> None:
    """Called by the transcript import on its own thread just before it opens
    a write batch, and when the batch ends: the transaction that thread opens
    meanwhile is recorded as an import's (for the long-wait log line; another
    process's import is invisible here)."""
    _LOCAL.import_batch = holding


def _import_holds_lock() -> bool:
    """Whether a transcript import of this process holds a transaction now."""
    me = threading.get_ident()
    with _TRANSACTION_LOCK:
        return any(o.is_import and o.write_started_monotonic is not None and o.thread_id != me
                   for o in _TRANSACTION_OWNERS.values())


def _process_name() -> str:
    """The program, never its arguments: the basename of argv[0]."""
    argv = getattr(sys, "argv", None) or [""]
    return os.path.basename(str(argv[0]))[:40] or "?"


def _is_lock_contention(exc: BaseException) -> bool:
    """Plain SQLITE_BUSY only: another connection holds the write lock, so a
    later attempt can succeed. Extended codes are not contention a poll can
    wait out (SQLITE_BUSY_SNAPSHOT: this connection's own read snapshot is
    stale, e.g. an unfinished autocommit SELECT cursor), and they fail at once
    as before."""
    return isinstance(exc, sqlite3.OperationalError) and \
        getattr(exc, "sqlite_errorcode", 0) == sqlite3.SQLITE_BUSY \
        and "database is locked" in str(exc).lower()


class _PrefetchedCursor:
    """Only an autocommit RETURNING result, exhausted before wait-log I/O.

    The audited callers use fetchone/fetchall. Keep the small result cursor
    interface (including metadata and iteration), without wrapping any other
    statement or an explicit transaction's cursor.
    """

    def __init__(self, cursor: sqlite3.Cursor, rows: list):
        self._cursor = cursor
        self._rows = iter(rows)
        self._closed = False

    @property
    def rowcount(self):
        return self._cursor.rowcount

    @property
    def lastrowid(self):
        return self._cursor.lastrowid

    @property
    def description(self):
        return self._cursor.description

    def __iter__(self):
        return self

    def __next__(self):
        if self._closed:
            raise sqlite3.ProgrammingError("Cannot operate on a closed cursor.")
        return next(self._rows)

    def fetchone(self):
        return next(self, None)

    def fetchall(self):
        return list(self)

    def close(self):
        self._cursor.close()
        self._rows = iter(())
        self._closed = True


class _TrackedConnection(sqlite3.Connection):
    """Connection that records explicit transaction ownership for diagnostics."""

    def _only_acquires_lock(self, template: str) -> bool:
        """Whether this statement, failing with BUSY, has done nothing: a
        locking BEGIN, or a single write in autocommit mode with no
        transaction open (no implicit BEGIN: isolation_level is None)."""
        if self.in_transaction or self.isolation_level is not None:
            return False
        upper = template.upper()
        return upper.startswith(_LOCK_BEGINS) or upper.partition(" ")[0] in _WRITE_VERBS

    def _execute_polled(self, sql, parameters, template: str):
        """Run a lock-acquiring statement, retrying plain BUSY every few ms
        until the connection's own busy deadline; then fail exactly as SQLite
        would. A wait over 250 ms logs one sqliteWriteWait line, admitted or
        not; whether this process's import held the lock is sampled while
        waiting. The line names the process by the basename of argv[0] only:
        arguments can carry message text."""
        deadline_ms = int(super().execute("PRAGMA busy_timeout").fetchone()[0])
        if deadline_ms <= 0:
            return super().execute(sql, parameters)
        started = time.monotonic()
        saw_import = admitted = False
        super().execute("PRAGMA busy_timeout = 0")
        try:
            while True:
                try:
                    cursor = super().execute(sql, parameters)
                    admitted = True
                    # Successful write admission must be visible while the
                    # finally block restores the budget and emits diagnostics.
                    _record_transaction_admission(self, template)
                    break
                except sqlite3.OperationalError as exc:
                    if not _is_lock_contention(exc) or \
                            (time.monotonic() - started) * 1000 >= deadline_ms:
                        raise
                    saw_import = saw_import or _import_holds_lock()
                time.sleep(_LOCK_POLL_SECONDS)
            # A polled write has result columns only with RETURNING. SQLite
            # still holds its autocommit writer until that cursor reaches DONE,
            # despite in_transaction=False. Fetch outside the BUSY retry loop:
            # a fetch error must never replay an already-admitted write.
            if not self.in_transaction and cursor.description is not None:
                return _PrefetchedCursor(cursor, cursor.fetchall())
            return cursor
        finally:
            super().execute(f"PRAGMA busy_timeout = {deadline_ms}")
            waited = time.monotonic() - started
            if waited > _LONG_WRITE_WAIT_SECONDS:
                from .log import sqlite_write_wait
                sqlite_write_wait(
                    f"waited_ms={round(waited * 1000)} "
                    f"outcome={'admitted' if admitted else 'gave_up'} "
                    f"import_in_process={saw_import} "
                    f"thread={threading.current_thread().name!r} sql={template[:60]!r} "
                    f"process={_process_name()!r}", self)

    def execute(self, sql, parameters=(), /):  # type: ignore[override]
        template = _sql_template(sql)
        measured = getattr(_LOCAL, "request_metrics", None) is not None
        started = time.perf_counter() if measured else None
        _record_transaction_statement(self, template)  # current attempted SQL
        try:
            if self._only_acquires_lock(template):
                cursor = self._execute_polled(sql, parameters, template)
            else:
                cursor = super().execute(sql, parameters)
            _record_transaction_admission(self, template)
        except sqlite3.OperationalError as exc:
            if not self.in_transaction:
                _clear_transaction_owner(self)
            if "database is locked" in str(exc).lower():
                _report_database_locked(template)
            raise
        except Exception:
            # SQLite's ON CONFLICT ROLLBACK can end a transaction below the
            # Python commit/rollback overrides. Do not retain a false holder.
            if not self.in_transaction:
                _clear_transaction_owner(self)
            raise
        finally:
            _record_query_metric(template, started)
        verb = template.partition(" ")[0].upper()
        if verb in _WRITE_VERBS:
            _bump_write_generation()
        if verb in {"COMMIT", "ROLLBACK", "END", "RELEASE"} and not self.in_transaction:
            _clear_transaction_owner(self)
        return cursor

    def executemany(self, sql, seq_of_parameters, /):  # type: ignore[override]
        template = _sql_template(sql)
        if template.partition(" ")[0].upper() in _WRITE_VERBS:
            _bump_write_generation()
        measured = getattr(_LOCAL, "request_metrics", None) is not None
        started = time.perf_counter() if measured else None
        _record_transaction_statement(self, template)
        try:
            cursor = super().executemany(sql, seq_of_parameters)
            _record_transaction_admission(self, template)
        except sqlite3.OperationalError as exc:
            if not self.in_transaction:
                _clear_transaction_owner(self)
            if "database is locked" in str(exc).lower():
                _report_database_locked(template)
            raise
        except Exception:
            if not self.in_transaction:
                _clear_transaction_owner(self)
            raise
        finally:
            _record_query_metric(template, started)
        return cursor

    def executescript(self, sql_script, /):  # type: ignore[override]
        template = _sql_template(sql_script)
        try:
            cursor = super().executescript(sql_script)
        except sqlite3.OperationalError as exc:
            if "database is locked" in str(exc).lower():
                _report_database_locked(template)
            raise
        if not self.in_transaction:
            _clear_transaction_owner(self)
        return cursor

    def commit(self):  # type: ignore[override]
        try:
            return super().commit()
        finally:
            if not self.in_transaction:
                _clear_transaction_owner(self)

    def rollback(self):  # type: ignore[override]
        try:
            return super().rollback()
        finally:
            if not self.in_transaction:
                _clear_transaction_owner(self)

    def __exit__(self, *exc):  # type: ignore[override]
        # `with connection:` commits or rolls back in C, past commit() and
        # rollback() above; end the tracked transaction here too.
        try:
            return super().__exit__(*exc)
        finally:
            if not self.in_transaction:
                _clear_transaction_owner(self)

    def close(self):  # type: ignore[override]
        try:
            return super().close()
        finally:
            _clear_transaction_owner(self)


def _open_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB_PATH), timeout=SQLITE_CONNECT_TIMEOUT_SEC,
                          isolation_level=None,
                          check_same_thread=False,
                          factory=_TrackedConnection)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode = WAL")
    con.execute("PRAGMA synchronous = NORMAL")
    con.execute("PRAGMA foreign_keys = ON")
    con.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
    # Private page caches multiply across DB-using threads. Keep a small
    # per-connection target; bulk reads still use the shared OS mmap cache.
    con.execute("PRAGMA cache_size = -8192")
    con.execute("PRAGMA mmap_size = 1073741824")
    return con




def conn() -> sqlite3.Connection:
    """Return this thread's sqlite connection.

    ThreadingHTTPServer runs each request on its own thread. A single shared
    sqlite3.Connection is NOT safe for concurrent use across threads — two
    threads stepping on the same cursor/statement raise "bad parameter or
    other API misuse" and make `UPDATE ... RETURNING` + fetchone() return None
    (→ int(None) crashes in the message store). check_same_thread=False only
    silences the guard; it doesn't make concurrent use safe. So each thread
    gets its own connection.

    Request threads close theirs via `close_local()` at request end (see
    Handler.finish) so short-lived threads don't leak FDs — the bug that made
    a previous thread-local attempt regress. The handful of long-lived worker
    threads keep one connection each for the process lifetime.
    """
    global _MIGRATED
    c = getattr(_LOCAL, "conn", None)
    if c is not None:
        if getattr(_LOCAL, "discard_conn", None) is not c or _in_transaction(c):
            return c
        # A busy-timeout restore failed inside a transaction (see
        # busy_timeout_at_most): now that it ended, never reuse that
        # connection with its lowered timeout; open a fresh one.
        _LOCAL.discard_conn = None
        close_local()
    # Migrate exactly once per process (idempotent + serialized). WAL means
    # every per-thread connection sees the migrated schema on the shared file.
    with _CONN_LOCK:
        c = _open_connection()
        if not _MIGRATED:
            db_migrations._migrate(c)
            _MIGRATED = True
    _LOCAL.conn = c
    return c


def is_locked_error(exc: BaseException) -> bool:
    """True for SQLITE_BUSY / 'database is locked' from this or another process."""
    return isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc).lower()


@contextmanager
def busy_timeout(timeout_ms: int) -> Iterator[sqlite3.Connection]:
    """Temporarily set this thread's SQLite busy timeout, then restore the
    previous one, so a nested use cannot cut an outer, longer wait short."""
    connection = conn()
    previous = int(connection.execute("PRAGMA busy_timeout").fetchone()[0])
    connection.execute(f"PRAGMA busy_timeout = {max(int(timeout_ms), previous)}")
    try:
        yield connection
    finally:
        connection.execute(f"PRAGMA busy_timeout = {previous}")


def _in_transaction(connection: sqlite3.Connection) -> bool:
    try:
        return bool(connection.in_transaction)
    except sqlite3.Error:
        return False          # a closed/broken connection holds no transaction


@contextmanager
def busy_timeout_at_most(timeout_ms: int) -> Iterator[sqlite3.Connection]:
    """Temporarily LOWER this thread's busy timeout to at most *timeout_ms*,
    restoring the previous value on success and on exception. For optional
    writes (F4 delivery hints) that must never hold up a launch behind a long
    writer; a give-up inside is expected and not reported as a lock wait.

    Nesting keeps the outer ``busy_expected`` value. If restoring the timeout
    fails, the connection is never reused with the lowered value: it is
    reported (previous, lowered and observed values) and dropped at once, or,
    when the caller is inside a transaction (dropping it would roll that
    back), the failure is raised and the connection is replaced as soon as
    the transaction has ended (``conn``)."""
    connection = conn()
    previous = int(connection.execute("PRAGMA busy_timeout").fetchone()[0])
    lowered = min(int(timeout_ms), previous)
    prior_expected = getattr(_LOCAL, "busy_expected", False)
    _LOCAL.busy_expected = True
    try:
        connection.execute(f"PRAGMA busy_timeout = {lowered}")
        yield connection
    finally:
        _LOCAL.busy_expected = prior_expected
        try:
            connection.execute(f"PRAGMA busy_timeout = {previous}")
        except Exception as exc:  # noqa: BLE001 - handled explicitly below
            _busy_restore_failed(connection, previous=previous, lowered=lowered, error=exc)


def _busy_restore_failed(connection: sqlite3.Connection, *, previous: int, lowered: int,
                         error: BaseException) -> None:
    try:
        observed: int | str = int(connection.execute("PRAGMA busy_timeout").fetchone()[0])
    except Exception:  # noqa: BLE001 - the connection may be unusable
        observed = "unreadable"
    from .log import log_exception
    log_exception("busyTimeoutRestoreFail", error,
                  detail=f"previous={previous} lowered={lowered} observed={observed} "
                         f"delta_ms={(previous - observed) if isinstance(observed, int) else 'unknown'}")
    if _in_transaction(connection):
        _LOCAL.discard_conn = connection
        raise RuntimeError("busy timeout restore failed inside a transaction; "
                           "the connection is replaced once it ends") from error
    if getattr(_LOCAL, "conn", None) is connection:
        close_local()


def best_effort_write(sql: str, parameters=(), *, timeout_ms: int = 20) -> bool:
    """Run one optional write, giving up after *timeout_ms* if the file is busy.

    For bookkeeping a request must never queue behind a long writer for
    (e.g. a device's last-seen stamp). Returns False when SQLite was busy;
    that expected give-up is not reported as a lock wait.
    """
    connection = conn()
    previous = int(connection.execute("PRAGMA busy_timeout").fetchone()[0])
    _LOCAL.busy_expected = True
    try:
        connection.execute(f"PRAGMA busy_timeout = {int(timeout_ms)}")
        connection.execute(sql, parameters)
        return True
    except sqlite3.OperationalError as exc:
        code = getattr(exc, "sqlite_errorcode", 0) & 0xff
        if code not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            raise
        return False
    finally:
        _LOCAL.busy_expected = False
        connection.execute(f"PRAGMA busy_timeout = {previous}")


_T = TypeVar("_T")


def retry_locked(
    operation: Callable[[], _T],
    *,
    retries: int | None = None,
    sleep_sec: float | None = None,
) -> _T:
    """Run *operation*, retrying SQLITE_BUSY a bounded number of times."""
    attempts = SQLITE_LOCK_RETRIES if retries is None else max(1, int(retries))
    delay = SQLITE_LOCK_RETRY_SLEEP_SEC if sleep_sec is None else max(0.0, float(sleep_sec))
    for attempt in range(attempts):
        try:
            return operation()
        except sqlite3.OperationalError as exc:
            if not is_locked_error(exc) or attempt + 1 >= attempts:
                raise
            if delay:
                time.sleep(delay * (attempt + 1))
    raise RuntimeError("retry_locked exhausted without returning")


def close_local() -> None:
    """Close and drop this thread's connection. Call at request end so a
    short-lived request thread releases its FDs instead of leaking them."""
    c = getattr(_LOCAL, "conn", None)
    if c is None:
        return
    try:
        c.close()
    except sqlite3.Error as exc:
        # Preserve close_local's non-raising contract, but report uncertain
        # release without a recursive eventlog/telemetry connection or payload.
        print(f"dbCloseFail store=state code={type(exc).__name__}",
              file=sys.stderr, flush=True)
    _LOCAL.conn = None


# Schema DDL lives in db_schema and the version-by-version upgrades in
# db_migrations. They are imported here, after the connection primitives, so
# the modules they pull in (the sub-schema providers import `db` back) see the
# same module state they always did.
from . import db_migrations, db_schema  # noqa: E402


def reset_for_tests(path: pathlib.Path | None = None) -> None:
    """Test helper: close cached connection so a new path takes effect."""
    global DB_PATH, _MIGRATED, _LAST_LOCK_REPORT_AT
    # Opening and migrating a connection form one operation. A worker that
    # opens the previous test's file must not mark the next file migrated.
    with _CONN_LOCK:
        if path is not None:
            DB_PATH = path
        _MIGRATED = False
        close_local()
    with _STAMP_LOCK:
        _close_stamp_connection()
    with _TRANSACTION_LOCK:
        _TRANSACTION_OWNERS.clear()
        _LAST_LOCK_REPORT_AT = 0.0


def __getattr__(name: str):
    """Resolve schema and migration internals from their new homes.

    Tests, the rehearsal script and tooling read `db._SCHEMA_SQL`,
    `db._SCHEMA_VERSION`, `db._MIN_UPGRADABLE_VERSION`, `db._migrate`,
    `db._create_schema`, `db._user_version` and `db._migrate_to_vN` on this
    module. A new `_migrate_to_vN` in db_migrations is reachable here without
    touching a re-export list.
    """
    for module in (db_schema, db_migrations):
        try:
            return getattr(module, name)
        except AttributeError:
            continue
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
