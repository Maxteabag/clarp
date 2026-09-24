"""Wall-clock helpers shared across the server library.

Leaf module: no lib imports, so anything may depend on it without cycles.
"""
from __future__ import annotations

import datetime as _dt
import time


def now_ms() -> int:
    """Integer epoch milliseconds from the wall clock (`time.time()`)."""
    return int(time.time() * 1000)


def iso_from_ms(ts_ms: int) -> str:
    """Epoch milliseconds -> ISO-8601 UTC with millisecond precision and a Z suffix."""
    stamp = _dt.datetime.fromtimestamp(ts_ms / 1000, tz=_dt.timezone.utc)
    return stamp.strftime("%Y-%m-%dT%H:%M:%S.") + f"{stamp.microsecond // 1000:03d}Z"
