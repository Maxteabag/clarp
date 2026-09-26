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


def _record_transaction_begin(connection: sqlite3.Connection, sql: object) -> None:
    owner = _TransactionOwner(
        connection_id=id(connection),
        thread_id=threading.get_ident(),
        thread_name=threading.current_thread().name,
        started_monotonic=time.monotonic(),
        begin_sql=_sql_template(sql),
        last_sql=_sql_template(sql),
        stack=tuple(traceback.format_stack(limit=14)[:-1]),
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


def _clear_transaction_owner(connection: sqlite3.Connection) -> None:
    with _TRANSACTION_LOCK:
        _TRANSACTION_OWNERS.pop(id(connection), None)


def _report_database_locked(waiting_sql: object) -> None:
    """Log transaction owners to stderr without touching SQLite recursively."""
    global _LAST_LOCK_REPORT_AT
    now = time.monotonic()
    waiting = _sql_template(waiting_sql)
    with _TRANSACTION_LOCK:
        owners = tuple(_TRANSACTION_OWNERS.values())
        if now - _LAST_LOCK_REPORT_AT < _LOCK_REPORT_INTERVAL_SEC:
            return
        _LAST_LOCK_REPORT_AT = now
    waiter = threading.current_thread()
    print(
        "sqlite_lock_wait "
        f"waiter_thread={waiter.name!r} waiter_ident={threading.get_ident()} "
        f"sql={waiting!r} owner_count={len(owners)}",
        file=sys.stderr,
        flush=True,
    )
    for owner in owners:
        stack = "".join(owner.stack).replace("\n", "\\n")
        print(
            "sqlite_lock_owner "
            f"connection={owner.connection_id} thread={owner.thread_name!r} "
            f"thread_ident={owner.thread_id} age_sec={now - owner.started_monotonic:.3f} "
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
            _close_stamp_connection()
            data_version = -1
        return data_version, _WRITE_GENERATION


class _TrackedConnection(sqlite3.Connection):
    """Connection that records explicit transaction ownership for diagnostics."""

    def execute(self, sql, parameters=(), /):  # type: ignore[override]
        template = _sql_template(sql)
        measured = getattr(_LOCAL, "request_metrics", None) is not None
        started = time.perf_counter() if measured else None
        try:
            cursor = super().execute(sql, parameters)
        except sqlite3.OperationalError as exc:
            if "database is locked" in str(exc).lower():
                _report_database_locked(template)
            raise
        finally:
            _record_query_metric(template, started)
        verb = template.partition(" ")[0].upper()
        if verb in _WRITE_VERBS:
            _bump_write_generation()
        if verb == "BEGIN" and self.in_transaction:
            _record_transaction_begin(self, template)
        elif verb in {"COMMIT", "ROLLBACK", "END"}:
            if not self.in_transaction:
                _clear_transaction_owner(self)
        elif self.in_transaction:
            _record_transaction_statement(self, template)
        return cursor

    def executemany(self, sql, seq_of_parameters, /):  # type: ignore[override]
        template = _sql_template(sql)
        if template.partition(" ")[0].upper() in _WRITE_VERBS:
            _bump_write_generation()
        measured = getattr(_LOCAL, "request_metrics", None) is not None
        started = time.perf_counter() if measured else None
        try:
            cursor = super().executemany(sql, seq_of_parameters)
        except sqlite3.OperationalError as exc:
            if "database is locked" in str(exc).lower():
                _report_database_locked(template)
            raise
        finally:
            _record_query_metric(template, started)
        if self.in_transaction:
            _record_transaction_statement(self, template)
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

    def close(self):  # type: ignore[override]
        _clear_transaction_owner(self)
        return super().close()


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
    # A 600 MB store with 2 MB of page cache re-reads its hot indexes from
    # disk constantly. 128 MB of cache per connection and a 1 GB mmap window
    # let the OS page cache serve repeat reads without copying.
    con.execute("PRAGMA cache_size = -131072")
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
        return c
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
    """Temporarily raise this thread's SQLite busy timeout, then restore it."""
    connection = conn()
    connection.execute(f"PRAGMA busy_timeout = {int(timeout_ms)}")
    try:
        yield connection
    finally:
        connection.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")


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
    except sqlite3.Error:
        pass
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
