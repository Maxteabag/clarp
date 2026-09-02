"""Central logging helpers.

The user's rule: never swallow exceptions silently. Everywhere we catch
something, we want at least a trace line so failures surface in `journalctl
--user -u clarp-refactor`.
"""
from __future__ import annotations

import sys
import time
import traceback


def _eventlog_emit(event: str, level: str, detail: str) -> None:
    """Forward to the structured eventlog. Lazy-imported so the module
    works in isolation (tests etc.) without forcing the eventlog file
    layout into existence."""
    try:
        from . import eventlog as _el
        _el.emit("server", event, level=level,
                 detail={"msg": detail} if detail else None)
    except Exception:
        pass


def log(event: str, detail: str = "") -> None:
    """Emit a single-line structured log entry to stderr AND a JSONL row.

    Stderr format: `HH:MM:SS event detail`. Goes to systemd journal.
    JSONL row goes through eventlog.emit() for DuckDB queries.
    """
    line = f"{time.strftime('%H:%M:%S')} {event}"
    if detail:
        line += f" {detail}"
    print(line, file=sys.stderr, flush=True)
    _eventlog_emit(event, "info", detail)


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
    except Exception:
        pass
