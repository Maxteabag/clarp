"""Process-local terminal presence shared by PTY transport and turn ownership."""
from __future__ import annotations

import threading

# Count, rather than a boolean, preserves overlapping terminal teardown.
_live: dict[str, int] = {}
_live_lock = threading.Lock()


def has_live_terminal(agent_id: str) -> bool:
    with _live_lock:
        return _live.get(agent_id, 0) > 0


def _mark(agent_id: str, delta: int) -> None:
    with _live_lock:
        n = max(0, _live.get(agent_id, 0) + delta)
        if n:
            _live[agent_id] = n
        else:
            _live.pop(agent_id, None)

