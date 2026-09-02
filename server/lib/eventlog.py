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

import datetime as _dt
import fcntl
import json
import os
import pathlib
import hashlib
import sys
import threading
import traceback
from dataclasses import dataclass


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


def _write_sqlite(record: dict, now: _dt.datetime) -> None:
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
        print(f"eventlog sqlite write failed: {error}", file=sys.stderr)


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
        except Exception:
            pass

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
    try:
        _write_jsonl(record, now_dt)
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
