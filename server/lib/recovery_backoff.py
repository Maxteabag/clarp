"""Backoff for durable queued recovery whose launch keeps failing.

Miso (codex), 2026-10-10 18:53-19:01: a broken Codex thread made every
thread/resume fail before the backend started. Queued recovery retried after a
flat second, 441 times in nine minutes. Each agent now waits 1, 2, 4 ... 60 s
between attempts at its queued head, and after ``PARK_AFTER`` identical
failures its recovery parks with a visible reason until a new send, an
explicit send of a queued item or a slot repair clears it. The queued rows are
never touched; the queue and goals are never paused.
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass, replace

BASE_S = 1.0
MAX_S = 60.0
PARK_AFTER = 8


@dataclass
class Backoff:
    queue_id: str
    failures: int = 0
    identical: int = 0
    signature: str = ""
    error: str = ""
    retry_at: float = 0.0
    parked: bool = False


def signature(error: BaseException) -> str:
    """Failures that differ only in ids, counts or times are identical."""
    text = f"{type(error).__name__}: {error}"
    return re.sub(r"[0-9a-f]{6,}|\d+", "#", text)[:300]


class RecoveryBackoff:
    """Per-agent backoff state, owned by the runtime's turn dispatcher."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_agent: dict[str, Backoff] = {}
        # Earliest scheduled backoff wake, so passes that skip a waiting
        # agent do not stack a timer each.
        self._wake_at: float | None = None

    def hold(self, agent_id: str, queue_id: str, now: float) -> Backoff | None:
        """The backoff that keeps ``agent_id``'s head row ``queue_id``
        waiting, or None when it may launch now. A different head row starts
        fresh."""
        with self._lock:
            state = self._by_agent.get(agent_id)
            if state is None:
                return None
            if state.queue_id != queue_id:
                del self._by_agent[agent_id]
                return None
            if state.parked or now < state.retry_at:
                return replace(state)
            return None

    def failed(self, agent_id: str, queue_id: str, error: BaseException,
               now: float) -> Backoff:
        """Record one failed launch; return the new state (a copy)."""
        sig = signature(error)
        with self._lock:
            state = self._by_agent.get(agent_id)
            if state is None or state.queue_id != queue_id:
                state = self._by_agent[agent_id] = Backoff(queue_id=queue_id)
            state.failures += 1
            state.identical = state.identical + 1 if sig == state.signature else 1
            state.signature = sig
            state.error = str(error)[:200]
            state.retry_at = now + min(MAX_S, BASE_S * 2 ** min(state.failures - 1, 16))
            state.parked = state.identical >= PARK_AFTER
            return replace(state)

    def clear(self, agent_id: str) -> Backoff | None:
        """Forget the agent's backoff (a launch succeeded or someone asked
        again). Returns what was cleared."""
        with self._lock:
            return self._by_agent.pop(agent_id, None)

    def claim_wake(self, at: float) -> bool:
        """True if the caller should schedule a wake for ``at``: no wake that
        early is already pending."""
        with self._lock:
            if self._wake_at is not None and self._wake_at <= at:
                return False
            self._wake_at = at
            return True

    def wake_fired(self, at: float) -> None:
        with self._lock:
            if self._wake_at == at:
                self._wake_at = None

    def snapshot(self) -> dict[str, dict]:
        with self._lock:
            return {agent_id: dict(state.__dict__)
                    for agent_id, state in self._by_agent.items()}

    def reset_for_tests(self) -> None:
        with self._lock:
            self._by_agent.clear()
            self._wake_at = None
