"""Central logging helpers.

The user's rule: never swallow exceptions silently. Everywhere we catch
something, we want at least a trace line so failures surface in `journalctl
--user -u clarp-refactor`.
"""
from __future__ import annotations

import sys
import os
import time
import traceback
import threading

# Only the synchronous sqliteWriteWait producer supplies this connection.
# It is restored on every exit and never keeps a connection in the queue.
_WAIT_CONTEXT = threading.local()

# Bounded process-local fault suppression; losing it on restart is intended.
# One slot is reserved for overflow instead of evicting a still-live site.
_FAULT_LOG_THROTTLE: dict[tuple[str, str], tuple[float, int]] = {}
_FAULT_LOG_COUNTS: dict[str, int] = {}
_FAULT_LOG_LOCK = threading.Lock()
_FAULT_LOG_LOCAL = threading.local()
_FAULT_LOG_OVERFLOW = ("faultLogOverflow", "UnknownError")
_FAULT_LOG_WINDOW = 60.0
_FAULT_LOG_LIMIT = 128
_FAULT_LOG_COUNT_LIMIT = 2**63 - 1


def _fault_label(value: object, fallback: str) -> str:
    if (type(value) is str and 0 < len(value) <= 64 and value.isascii()
            and value[0].isalpha() and value.replace("_", "").isalnum()):
        return value
    return fallback


def _fault_count(name: str, amount: int = 1) -> None:
    # Called only under _FAULT_LOG_LOCK, with fixed names and integer counts.
    _FAULT_LOG_COUNTS[name] = min(
        _FAULT_LOG_COUNT_LIMIT, _FAULT_LOG_COUNTS.get(name, 0) + amount)


def log_fault(code: str, exc: BaseException, *, structured: bool = True) -> str:
    """Report a caught fault using only a fixed site code and exception class.

    Internal results describe calls, never delivery: attempted means a selected
    sink returned normally, throttled means no sink was selected this window,
    dropped means none returned (including recursive refusal). Ordinary sink
    faults cannot affect the caller; process-control exceptions still propagate.
    Logging wrappers MUST use structured=False to avoid retrying their sink.
    """
    if getattr(_FAULT_LOG_LOCAL, "active", False):
        return "dropped"
    _FAULT_LOG_LOCAL.active = True
    try:
        code = _fault_label(code, "faultLogInvalidCode")
        error_type = _fault_label(type(exc).__name__, "UnknownError")
        now = time.monotonic()
        slot = (code, error_type)
        with _FAULT_LOG_LOCK:
            if slot not in _FAULT_LOG_THROTTLE and len(_FAULT_LOG_THROTTLE) >= _FAULT_LOG_LIMIT - 1:
                for stale in [key for key, (at, _) in _FAULT_LOG_THROTTLE.items()
                              if key != _FAULT_LOG_OVERFLOW and now - at >= 2 * _FAULT_LOG_WINDOW]:
                    del _FAULT_LOG_THROTTLE[stale]
                if len(_FAULT_LOG_THROTTLE) >= _FAULT_LOG_LIMIT - 1:
                    slot = _FAULT_LOG_OVERFLOW
            last, suppressed = _FAULT_LOG_THROTTLE.get(slot, (-float("inf"), 0))
            if now - last < _FAULT_LOG_WINDOW:
                _FAULT_LOG_THROTTLE[slot] = (last, min(_FAULT_LOG_COUNT_LIMIT, suppressed + 1))
                _fault_count("throttled")
                return "throttled"
            # Even a failed attempt consumes the window, bounding broken sinks.
            _FAULT_LOG_THROTTLE[slot] = (now, 0)
            _fault_count("attempts")
        if slot == _FAULT_LOG_OVERFLOW:
            code, error_type = slot
        line = f"{code} error_type={error_type} suppressed={suppressed}"
        attempted = False
        stderr_errors = raw_errors = structured_errors = 0
        try:
            print(line, file=sys.stderr, flush=True)
            attempted = True
        except Exception:
            stderr_errors = 1
            try:
                # A finite fallback, sharing stderr's descriptor/journal domain.
                os.write(2, (line + "\n").encode("ascii"))
                attempted = True
            except Exception:
                raw_errors = 1
        if structured:
            try:
                from . import eventlog
                eventlog.emit("server", code, level="warning",
                              detail={"error_type": error_type, "suppressed": suppressed})
                attempted = True
            except Exception:
                structured_errors = 1
        with _FAULT_LOG_LOCK:
            _fault_count("stderr_errors", stderr_errors)
            _fault_count("raw_errors", raw_errors)
            _fault_count("structured_errors", structured_errors)
            if not attempted:
                _fault_count("dropped")
        return "attempted" if attempted else "dropped"
    except Exception:
        # For example, a failed clock/label lookup: no sink success is claimed.
        return "dropped"
    finally:
        _FAULT_LOG_LOCAL.active = False


def _eventlog_emit(event: str, level: str, detail: str) -> None:
    """Forward to the structured eventlog. Lazy-imported so the module
    works in isolation (tests etc.) without forcing the eventlog file
    layout into existence."""
    try:
        from . import eventlog as _el
        _el.emit("server", event, level=level,
                 detail={"msg": detail} if detail else None)
    except Exception as exc:
        log_fault("logEventlogFailed", exc, structured=False)


def flush_deferred() -> None:
    """Write the telemetry rows this thread queued inside a state transaction
    (RM-N8b). db.py calls it when a transaction ends; it reaches the eventlog
    through here because logging is the one way down the layers."""
    from . import eventlog as _el
    _el.flush_deferred()


def log(event: str, detail: str = "") -> None:
    """Emit a single-line structured log entry to stderr AND a JSONL row.

    Stderr format: `HH:MM:SS event detail`. Goes to systemd journal.
    JSONL row goes through eventlog.emit() for DuckDB queries.
    """
    line = f"{time.strftime('%H:%M:%S')} {event}"
    if detail:
        line += f" {detail}"
    if event == "sqliteWriteWait":
        connection = getattr(_WAIT_CONTEXT, "connection", None)
        if connection is not None:
            from . import eventlog
            if eventlog.defer_sqlite_write_wait(connection, line, detail):
                return
    print(line, file=sys.stderr, flush=True)
    _eventlog_emit(event, "info", detail)


def sqlite_write_wait(detail: str, connection) -> None:
    """Defer every sink of this line while its explicit state transaction is open."""
    previous = getattr(_WAIT_CONTEXT, "connection", None)
    _WAIT_CONTEXT.connection = connection
    try:
        log("sqliteWriteWait", detail)
    finally:
        _WAIT_CONTEXT.connection = previous


def log_exception(event: str, exc: BaseException, detail: str = "") -> None:
    """Log a caught exception with type, message, and short traceback summary.

    Use this in every except-clause so silent failures leave a trail. The
    structured eventlog row carries the full traceback.
    """
    tb = traceback.format_exception_only(type(exc), exc)
    msg = (tb[-1] if tb else f"{type(exc).__name__}: {exc}").strip()
    extra = f" {detail}" if detail else ""
    print(
        f"{time.strftime('%H:%M:%S')} {event}{extra} :: {msg}",
        file=sys.stderr,
        flush=True,
    )
    try:
        from . import eventlog as _el
        _el.emit_exception("server", event, exc,
                           detail={"msg": detail} if detail else None)
    except Exception as sink_exc:
        log_fault("logExceptionEventlogFailed", sink_exc, structured=False)
