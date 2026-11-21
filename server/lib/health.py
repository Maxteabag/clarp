"""In-process subsystem health registry for diagnostics and tests."""
from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass


@dataclass
class SubsystemHealth:
    last_success_at: float | None = None
    last_error_at: float | None = None
    last_error: str | None = None


_LOCK = threading.Lock()
_STATE: dict[str, SubsystemHealth] = {}


def mark_success(subsystem: str, *, now: float | None = None) -> None:
    with _LOCK:
        item = _STATE.setdefault(subsystem, SubsystemHealth())
        item.last_success_at = now if now is not None else time.time()


def mark_error(subsystem: str, error: BaseException | str,
               *, now: float | None = None) -> None:
    """Record that `subsystem` failed. Every paired device reads this state (GET
    /diagnostics/health, limited scope too), and an exception's text can name local
    paths or carry a provider's reply, so only the exception's type name is kept, or,
    for a string, the fixed word the caller passes on purpose. The full error belongs
    in the caller's own log line."""
    word = type(error).__name__ if isinstance(error, BaseException) else str(error)
    with _LOCK:
        item = _STATE.setdefault(subsystem, SubsystemHealth())
        item.last_error_at = now if now is not None else time.time()
        item.last_error = word


def snapshot() -> dict[str, dict]:
    with _LOCK:
        return {name: asdict(item) for name, item in sorted(_STATE.items())}


def reset_for_tests() -> None:
    with _LOCK:
        _STATE.clear()
