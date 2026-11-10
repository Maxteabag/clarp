"""Structured-event writer.

Every interesting thing that happens in the system gets one row in the
`diagnostic_events` table of telemetry.sqlite. A JSONL mirror in
~/.cache/clarp/logs/YYYY-MM-DD.jsonl feeds the DuckDB tooling in
scripts/query.sh; SQLite is the live debugging source of truth.

Concurrency: every producer (server thread, Stop hook subprocess,
UserPromptSubmit hook subprocess) calls `emit()`. We open the file in append
mode and take an `flock(LOCK_EX)` around the single write. Append-mode
writes are atomic for short records on Linux, but the lock keeps us honest
when two processes happen to land in the same OS write call.

Schema (top-level promoted columns; everything else goes in `detail`):
  ts          ISO-8601 UTC string (millisecond precision)
  source      "server" | "client" | "stop_hook" | "userprompt_hook"
              | "tts" | "stt" | "scheduler" | "router"
  event       short verb, e.g. "heraldEmitted", "httpRequest", "playOk"
  level       "info" | "warn" | "error" (default info)
  trace_id    end-to-end turn trace id
  request_id  per-HTTP-request id
  client_id   per-client/browser id when known
  session     app session name (when known)
  agent_id    app agent id (when known)
  backend_session_id backend conversation id (when known)
  persona     "Mike" | "Rachel" | ...
  clip_id     SQLite clip id involved (when relevant)
  clip_url    audio clip URL/path involved (when relevant)
  sse_event_id durable SSE replay cursor id (when relevant)
  path        HTTP path or other promoted route/path
  status      HTTP status or lifecycle status code
  duration_ms float / int — anything timing
  detail      arbitrary JSON object — free-form payload
"""
from __future__ import annotations

import atexit
import datetime as _dt
import fcntl
import json
import os
import pathlib
import hashlib
import sys
import sqlite3
import weakref
import threading
import time
import traceback
from dataclasses import dataclass

from .log import log_fault


LOG_DIR = pathlib.Path(os.environ.get(
    "CLAUDE_PWA_LOG_DIR",
    str(pathlib.Path.home() / ".cache" / "clarp" / "logs"),
))
LOG_DIR.mkdir(parents=True, exist_ok=True)


# In-process serialisation. The flock handles cross-process; this avoids
# interleaving when many threads in the same server emit at once.
_LOCK = threading.Lock()
_PRIVATE_DETAIL_KEYS = frozenset({
    "text", "prompt", "transcript", "content", "body", "authorization",
    "auth_token", "token", "api_key", "password",
})


@dataclass(frozen=True)
class EventContext:
    trace_id: str | None = None
    agent_id: str | None = None
    session: str | None = None
    backend_session_id: str | None = None


def _path_for(now: _dt.datetime | None = None) -> pathlib.Path:
    now = now or _dt.datetime.now(_dt.timezone.utc)
    return LOG_DIR / f"{now.date().isoformat()}.jsonl"


def reset_for_tests(path: pathlib.Path | None = None) -> None:
    global LOG_DIR
    LOG_DIR = pathlib.Path(path) if path is not None else pathlib.Path(os.environ.get(
        "CLAUDE_PWA_LOG_DIR",
        str(pathlib.Path.home() / ".cache" / "clarp" / "logs"),
    ))
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with _DROPPED_LOCK:
        _DROPPED.update(pid=os.getpid(), total=0, reported=0, last_report=None)


def _iso(now: _dt.datetime | None = None) -> str:
    now = now or _dt.datetime.now(_dt.timezone.utc)
    # millisecond precision, Z suffix — DuckDB parses this as TIMESTAMP.
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _ts_ms(now: _dt.datetime) -> int:
    if now.tzinfo is None:
        now = now.replace(tzinfo=_dt.timezone.utc)
    return int(now.timestamp() * 1000)


def _redacted_value(value: object) -> str:
    raw = str(value)
    digest = hashlib.sha256(raw.encode()).hexdigest()[:12]
    return f"<redacted length={len(raw)} sha256={digest}>"


def _sanitize_detail(value: object, key: str = "") -> object:
    if key.strip().lower() in _PRIVATE_DETAIL_KEYS:
        return _redacted_value(value)
    if isinstance(value, dict):
        return {str(name): _sanitize_detail(item, str(name))
                for name, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_detail(item) for item in value]
    return value


# Ordinary emits queue only their SQLite row; JSONL stays synchronous. The
# sqliteWriteWait record queues both sinks (and stderr) until its particular
# explicit/open state transaction ends (RM-N8b addition B).
@dataclass(frozen=True)
class _DeferredRecord:
    record: dict | None
    now: _dt.datetime
    jsonl: bool = False
    stderr_line: str | None = None
    connection: weakref.ReferenceType | None = None


_DEFERRED: dict[int, list[_DeferredRecord]] = {}
_DEFERRED_LOCK = threading.Lock()

# Process-local loss accounting, independent of the contended telemetry DB.
# Report the first loss immediately, then at most once per minute on an insert
# attempt (including recovery); orderly exit flushes any remaining count.
_DROPPED = {"pid": os.getpid(), "total": 0, "reported": 0, "last_report": None}
_DROPPED_LOCK = threading.Lock()


def _in_state_transaction() -> bool:
    from . import db
    return db.thread_holds_transaction()


def flush_deferred() -> None:
    """Write this thread's queued rows and those of threads that have ended."""
    if not _DEFERRED:
        return
    me = threading.get_ident()
    alive = {thread.ident for thread in threading.enumerate()}
    with _DEFERRED_LOCK:
        owners = [ident for ident in _DEFERRED if ident == me or ident not in alive]
        batches = [(ident, _DEFERRED.pop(ident)) for ident in owners]
    for owner, batch in batches:
        _flush_batch(owner, batch)


def _flush_batch(owner: int, batch: list[_DeferredRecord]) -> None:
    for index, item in enumerate(batch):
        connection = item.connection() if item.connection is not None else None
        if connection is not None:
            try:
                blocked = connection.in_transaction
            except sqlite3.ProgrammingError:  # a closed connection released its lock
                blocked = False
            if blocked:
                # Manual/exit flushes must not bypass this wait's transaction.
                # Prepend before later emits to preserve this thread's order.
                with _DEFERRED_LOCK:
                    _DEFERRED[owner] = batch[index:] + _DEFERRED.get(owner, [])
                return
        if item.stderr_line is not None:
            print(item.stderr_line, file=sys.stderr, flush=True)
        if item.record is not None:
            _insert_sqlite(item.record, item.now)
            if item.jsonl:
                _append_jsonl(item.record, item.now)


def _flush_all_deferred() -> None:
    with _DEFERRED_LOCK:
        batches = list(_DEFERRED.items())
        _DEFERRED.clear()
    for owner, batch in batches:
        _flush_batch(owner, batch)
    _report_dropped(_dt.datetime.now(_dt.timezone.utc), force=True)


def defer_sqlite_write_wait(connection, stderr_line: str, detail: str) -> bool:
    """Queue the wait line without sink I/O while its explicit transaction is open.

    Autocommit RETURNING cursors are deliberately outside this predicate (RM-N13).
    Weak ownership never keeps an abandoned connection/its writer lock alive.
    """
    if not connection.in_transaction:
        return False
    from . import diagnostics_settings
    now = _dt.datetime.now(_dt.timezone.utc)
    record = None
    if diagnostics_settings.allows_event(source="server", event="sqliteWriteWait"):
        record = {"ts": _iso(now), "source": "server", "event": "sqliteWriteWait",
                  "level": "info", "detail": {"msg": detail}}
    item = _DeferredRecord(record, now, jsonl=True, stderr_line=stderr_line,
                           connection=weakref.ref(connection))
    with _DEFERRED_LOCK:
        _DEFERRED.setdefault(threading.get_ident(), []).append(item)
    return True


atexit.register(_flush_all_deferred)


def _write_sqlite(record: dict, now: _dt.datetime) -> None:
    if _in_state_transaction():
        with _DEFERRED_LOCK:
            _DEFERRED.setdefault(threading.get_ident(), []).append(_DeferredRecord(record, now))
        return
    flush_deferred()
    _insert_sqlite(record, now)


def _insert_sqlite(record: dict, now: _dt.datetime) -> None:
    dropped = False
    try:
        from . import telemetry

        detail = record.get("detail")
        detail_json = None
        if detail is not None:
            detail_json = json.dumps(detail, separators=(",", ":"))
        # The telemetry connection is deliberately separate from state.sqlite.
        # A process-wide Python lock here creates a lock inversion: a logger
        # can hold that lock while waiting for SQLite, while the current SQLite
        # writer logs before committing and waits for the Python lock forever.
        telemetry.conn().execute(
            """
            INSERT INTO diagnostic_events (
                ts, ts_iso, source, event, level, trace_id, request_id,
                client_id, session, agent_id, backend_session_id, persona,
                clip_id, clip_url, sse_event_id, path, status, duration_ms,
                detail
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _ts_ms(now),
                record["ts"],
                record["source"],
                record["event"],
                record["level"],
                record.get("trace_id"),
                record.get("request_id"),
                record.get("client_id"),
                record.get("session"),
                record.get("agent_id"),
                record.get("backend_session_id"),
                record.get("persona"),
                record.get("clip_id"),
                record.get("clip_url"),
                record.get("sse_event_id"),
                record.get("path"),
                record.get("status"),
                record.get("duration_ms"),
                detail_json,
            ),
        )
    except Exception as error:
        dropped = True
        print(f"eventlog sqlite write failed: {error}", file=sys.stderr)
    _report_dropped(_dt.datetime.now(_dt.timezone.utc), dropped=dropped)


def _write_jsonl(record: dict, now: _dt.datetime) -> None:
    line = json.dumps(record, separators=(",", ":")) + "\n"
    path = _path_for(now)
    with _LOCK:
        with open(path, "a", encoding="utf-8") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                f.write(line)
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def _report_dropped(now: _dt.datetime, *, dropped: bool = False, force: bool = False) -> None:
    # Never call emit() here: a summary must not attempt SQLite or recursively
    # count itself. Hold this lock only across accounting and the JSONL append,
    # never across an insert, state transaction or deferred-queue lock.
    with _DROPPED_LOCK:
        pid = os.getpid()
        if _DROPPED["pid"] != pid:  # a fork starts a new process counter
            _DROPPED.update(pid=pid, total=0, reported=0, last_report=None)
        if dropped:
            _DROPPED["total"] += 1
        pending = _DROPPED["total"] - _DROPPED["reported"]
        if not pending:
            return
        tick = time.monotonic()
        last = _DROPPED["last_report"]
        if not force and last is not None and tick - last < 60:
            return
        record = {
            "ts": _iso(now), "source": "server", "event": "eventlogDropped", "level": "warn",
            "detail": {"pid": pid, "dropped": pending, "total_dropped": _DROPPED["total"]},
        }
        try:
            _write_jsonl(record, now)
        except OSError as error:
            # Retain the count for the next attempt; logging cannot break callers.
            print(f"eventlog drop report failed: {error}", file=sys.stderr)
            return
        _DROPPED["reported"] = _DROPPED["total"]
        _DROPPED["last_report"] = tick


def emit(source: str, event: str, *,
         level: str = "info",
         session: str | None = None,
         agent_id: str | None = None,
         backend_session_id: str | None = None,
         persona: str | None = None,
         clip_id: int | None = None,
         clip_url: str | None = None,
         sse_event_id: int | None = None,
         request_id: str | None = None,
         client_id: str | None = None,
         path: str | None = None,
         status: int | None = None,
         duration_ms: float | int | None = None,
         trace_id: str | None = None,
         context: EventContext | None = None,
         detail: dict | None = None,
         now: _dt.datetime | None = None) -> None:
    """Write a single event row. Never raises — logging must not break the
    caller."""
    if context is not None:
        trace_id = trace_id or context.trace_id
        session = session or context.session
        agent_id = agent_id or context.agent_id
        backend_session_id = backend_session_id or context.backend_session_id
        if context.agent_id:
            detail = {"agent_id": context.agent_id, **(detail or {})}
    from . import diagnostics_settings
    if not diagnostics_settings.allows_event(
            source=source, event=event, path=path):
        return
    now_dt = now or _dt.datetime.now(_dt.timezone.utc)
    record: dict = {
        "ts": _iso(now_dt),
        "source": source,
        "event": event,
        "level": level,
    }
    # Auto-pick up the per-session trace marker if the caller didn't supply one.
    if not trace_id and session:
        try:
            from . import agents as _agents
            agent = _agents.get_by_session(session)
            if agent:
                trace_id = _agents.get_trace(agent["agent_id"]) or None
        except Exception as exc:
            log_fault("eventlogTraceLookupFailed", exc, structured=False)

    if session:     record["session"] = session
    if agent_id:    record["agent_id"] = agent_id
    if backend_session_id: record["backend_session_id"] = backend_session_id
    if persona:     record["persona"] = persona
    if clip_id is not None: record["clip_id"] = clip_id
    if clip_url:    record["clip_url"] = clip_url
    if sse_event_id is not None: record["sse_event_id"] = sse_event_id
    if request_id:  record["request_id"] = request_id
    if client_id:   record["client_id"] = client_id
    if path:        record["path"] = path
    if status is not None: record["status"] = status
    if duration_ms is not None: record["duration_ms"] = duration_ms
    if trace_id:    record["trace_id"] = trace_id
    if detail:
        record["detail"] = _sanitize_detail(detail)
    try:
        json.dumps(record, separators=(",", ":"))
    except (TypeError, ValueError):
        # Detail wasn't serialisable — coerce to repr and retry.
        try:
            record["detail"] = {"_repr": repr(detail)}
            json.dumps(record, separators=(",", ":"))
        except Exception as error:
            print(f"eventlog serialization failed: {error}", file=sys.stderr)
            return
    _write_sqlite(record, now_dt)
    _append_jsonl(record, now_dt)


def _append_jsonl(record: dict, now: _dt.datetime) -> None:
    try:
        _write_jsonl(record, now)
    except OSError as error:
        # Never let logging hurt the caller, but leave a journal trail.
        print(f"eventlog write failed: {error}", file=sys.stderr)


def emit_exception(source: str, event: str, exc: BaseException, **kw) -> None:
    """Same as emit() but unpacks a caught exception into the detail blob."""
    tb_text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    detail = kw.pop("detail", None) or {}
    detail.setdefault("error_type", type(exc).__name__)
    detail.setdefault("error", str(exc))
    detail.setdefault("traceback", tb_text)
    emit(source, event, level="error", detail=detail, **kw)
