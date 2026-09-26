"""Per-agent turn slots: who is in flight, who waits, who is still spawning.

One object owns the three maps the dispatcher used to keep as module globals
(``_INFLIGHT``, ``_QUEUED``, ``_CLAIMED_AT``) and the two sentinel markers,
behind one lock. Memory is a written-through view; the durable authority is:

* in flight   -> the newest open ``turns`` row of the agent (written by
                 ``turn_lifecycle.open_turn`` when the dispatcher claims);
* queued      -> ``queued_turns`` rows in status ``queued`` (``turn_queue``);
* Stop-parked -> ``queued_turns`` rows in status ``parked`` for sends that
                 have no durable queue row of their own (a normal send
                 admitted while the Stop barrier was up used to live only
                 here, in memory, and vanished with the process).

``rehydrate()`` rebuilds the view from those rows after a restart; the
dispatcher's ``recover_queued`` then re-admits what was queued or parked.

``live_work()`` is the one query for "is anything happening for this agent",
folding in the two facts other modules own: an attached interactive terminal
(``terminal_ws``) and a running compaction (``compaction``).
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable

# Placeholder trace owning the in-flight slot while an interactive terminal is
# attached to an agent. A normal turn routed to that agent queues behind it
# (two processes resuming one session would corrupt the transcript); the queue
# drains via ``turn_dispatch.drain_after_terminal()`` when the terminal closes.
TERMINAL_SENTINEL = "terminal"
# Placeholder installed by Stop between "interrupt requested" and "interrupt
# confirmed"; sends admitted meanwhile are parked behind it.
STOPPING_SENTINEL = "stopping"
SENTINELS = frozenset({TERMINAL_SENTINEL, STOPPING_SENTINEL})


class SlotsExhausted(RuntimeError):
    """The bounded slot table refused to grow."""


@dataclass(frozen=True)
class LiveWork:
    agent_id: str
    inflight: str          # trace id, a sentinel, or ""
    spawning: bool
    queued: int
    terminal: bool
    compacting: bool

    @property
    def stopping(self) -> bool:
        return self.inflight == STOPPING_SENTINEL

    @property
    def active_trace(self) -> str:
        return "" if self.inflight in SENTINELS else self.inflight

    @property
    def busy(self) -> bool:
        return bool(self.inflight) or self.terminal or self.compacting


@dataclass(frozen=True)
class StopSnapshot:
    trace_id: str
    claimed_at: float | None
    queued: tuple

    def as_dict(self) -> dict[str, Any]:
        return {"trace_id": self.trace_id, "claimed_at": self.claimed_at,
                "queued": list(self.queued)}


class TurnSlots:
    MAX_AGENTS = 4096
    MAX_QUEUE_PER_AGENT = 256

    def __init__(self, *, lock: Any | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self.lock = lock if lock is not None else threading.RLock()
        self._clock = clock
        # The dicts are exposed under the dispatcher's historical names so
        # existing tests and the few remaining inline sites see one truth.
        self.inflight: dict[str, str] = {}
        self.queued: dict[str, list] = {}
        self.claimed_at: dict[str, float] = {}

    # --- reads -------------------------------------------------------------

    def get(self, agent_id: str) -> str:
        with self.lock:
            return self.inflight.get(agent_id) or ""

    def owns(self, agent_id: str, trace_id: str) -> bool:
        with self.lock:
            return bool(trace_id) and self.inflight.get(agent_id) == trace_id

    def is_spawning(self, agent_id: str) -> bool:
        with self.lock:
            return agent_id in self.claimed_at

    def queue_depth(self, agent_id: str) -> int:
        with self.lock:
            return len(self.queued.get(agent_id) or ())

    def has_queued(self, agent_id: str, predicate: Callable[[Any], bool]) -> bool:
        with self.lock:
            return any(predicate(item) for item in self.queued.get(agent_id, ()))

    def snapshot(self) -> dict[str, Any]:
        """Serializable ownership view (the runtime ``status`` payload)."""
        with self.lock:
            return {
                "active": {
                    agent_id: trace_id
                    for agent_id, trace_id in self.inflight.items()
                    if trace_id not in SENTINELS
                },
                "terminals": sorted(
                    agent_id for agent_id, trace_id in self.inflight.items()
                    if trace_id == TERMINAL_SENTINEL),
                "spawning": sorted(self.claimed_at),
                "queued": {
                    agent_id: len(items)
                    for agent_id, items in self.queued.items() if items
                },
            }

    def live_work(self, agent_id: str, *, session: str = "") -> LiveWork:
        """Everything that counts as work for the agent, from every owner."""
        with self.lock:
            inflight = self.inflight.get(agent_id) or ""
            spawning = agent_id in self.claimed_at
            queued = len(self.queued.get(agent_id) or ())
        return LiveWork(
            agent_id=agent_id, inflight=inflight, spawning=spawning,
            queued=queued, terminal=_terminal_live(agent_id),
            compacting=_compacting(session) if session else False)

    # --- claims ------------------------------------------------------------

    def claim(self, agent_id: str, trace_id: str) -> None:
        """Take the slot for ``trace_id`` and mark it spawning."""
        with self.lock:
            if (agent_id not in self.inflight
                    and len(self.inflight) >= self.MAX_AGENTS):
                raise SlotsExhausted(
                    f"{self.MAX_AGENTS} agents already hold turn slots")
            self.inflight[agent_id] = trace_id
            self.claimed_at[agent_id] = self._clock()

    def mark_spawned(self, agent_id: str, trace_id: str) -> bool:
        """The backend accepted the turn: it is no longer spawning."""
        with self.lock:
            if self.inflight.get(agent_id) != trace_id:
                return False
            self.claimed_at.pop(agent_id, None)
            return True

    def touch_claim(self, agent_id: str) -> None:
        with self.lock:
            self.claimed_at[agent_id] = self._clock()

    def release(self, agent_id: str, trace_id: str) -> bool:
        """Give the slot back if ``trace_id`` still owns it."""
        with self.lock:
            if self.inflight.get(agent_id) != trace_id:
                return False
            self.inflight.pop(agent_id, None)
            self.claimed_at.pop(agent_id, None)
            return True

    def free_stale(self, agent_id: str) -> str | None:
        """INV3 (lib.reconcile): free a slot that has no live turn and nothing
        queued behind it. Returns the dead trace id, or None if nothing was
        freed. Spawning slots and terminal sentinels are left alone."""
        with self.lock:
            if agent_id in self.claimed_at or agent_id not in self.inflight:
                return None
            if self.queued.get(agent_id):
                return None  # the next send / finish drains these
            trace = self.inflight.get(agent_id)
            if trace == TERMINAL_SENTINEL:
                return None
            self.inflight.pop(agent_id, None)
            return trace or ""

    # --- queue -------------------------------------------------------------

    def enqueue(self, agent_id: str, spec: Any) -> int:
        """Append behind the current owner; returns the new depth."""
        with self.lock:
            items = self.queued.setdefault(agent_id, [])
            if len(items) >= self.MAX_QUEUE_PER_AGENT:
                raise SlotsExhausted(
                    f"agent {agent_id} already has "
                    f"{self.MAX_QUEUE_PER_AGENT} turns queued")
            items.append(spec)
            return len(items)

    def hold_for_terminal(self, agent_id: str, spec: Any) -> int:
        with self.lock:
            depth = self.enqueue(agent_id, spec)
            self.inflight.setdefault(agent_id, TERMINAL_SENTINEL)
            return depth

    def pop_next(self, agent_id: str, *, expected: str | None = None) -> Any:
        """Hand the slot to the next queued spec, or free it.

        With ``expected`` the handover happens only while that trace (or
        sentinel) still owns the slot. Returns the spec now owning the slot,
        or None when the slot was freed (or ownership had moved)."""
        with self.lock:
            if expected is not None and self.inflight.get(agent_id) != expected:
                return None
            queue = self.queued.get(agent_id)
            next_spec = queue.pop(0) if queue else None
            if next_spec is None:
                self.inflight.pop(agent_id, None)
                self.claimed_at.pop(agent_id, None)
                self.queued.pop(agent_id, None)
                return None
            self.inflight[agent_id] = next_spec.trace_id
            self.claimed_at[agent_id] = self._clock()
            return next_spec

    def drop_queued(self, agent_id: str, predicate: Callable[[Any], bool]) -> int:
        with self.lock:
            items = self.queued.get(agent_id, [])
            kept = [item for item in items if not predicate(item)]
            if kept:
                self.queued[agent_id] = kept
            else:
                self.queued.pop(agent_id, None)
            return len(items) - len(kept)

    def clear(self, agent_id: str, *, stopping: bool = False) -> int:
        """Drop the slot (or replace it with the Stop barrier) and the memory
        queue. Returns the number of queued specs dropped."""
        with self.lock:
            if stopping:
                self.inflight[agent_id] = STOPPING_SENTINEL
            else:
                self.inflight.pop(agent_id, None)
            self.claimed_at.pop(agent_id, None)
            return len(self.queued.pop(agent_id, []) or [])

    # --- Stop barrier ------------------------------------------------------

    def begin_stop(self, agent_id: str) -> tuple[StopSnapshot, int]:
        """Install the Stop barrier atomically; returns what it replaced."""
        with self.lock:
            value = self.inflight.get(agent_id)
            snapshot = StopSnapshot(
                trace_id=value if value not in {None, STOPPING_SENTINEL} else "",
                claimed_at=self.claimed_at.get(agent_id),
                queued=tuple(self.queued.get(agent_id) or ()))
            self.inflight[agent_id] = STOPPING_SENTINEL
            self.claimed_at.pop(agent_id, None)
            dropped = len(self.queued.pop(agent_id, []) or [])
            return snapshot, dropped

    def restore_stop(self, agent_id: str, snapshot: dict[str, Any]) -> bool:
        """Roll back the barrier after a failed interrupt."""
        with self.lock:
            if self.inflight.get(agent_id) != STOPPING_SENTINEL:
                return False
            trace_id = str(snapshot.get("trace_id") or "")
            if trace_id:
                self.inflight[agent_id] = trace_id
            else:
                self.inflight.pop(agent_id, None)
            claimed_at = snapshot.get("claimed_at")
            if claimed_at is None:
                self.claimed_at.pop(agent_id, None)
            else:
                self.claimed_at[agent_id] = float(claimed_at)
            # Sends admitted while the barrier was up were appended after the
            # snapshot. Preserve them behind the restored pre-Stop queue.
            queued = list(snapshot.get("queued") or []) + list(
                self.queued.get(agent_id) or [])
            if queued:
                self.queued[agent_id] = queued
            else:
                self.queued.pop(agent_id, None)
            return True

    def restore_queue(self, agent_id: str, snapshot: dict[str, Any],
                      cancelled_trace_ids: Iterable[str]) -> None:
        """Put preserved and newly parked specs back, minus cancelled ones."""
        cancelled = set(cancelled_trace_ids)
        with self.lock:
            queue = list(snapshot.get("queued") or []) + list(
                self.queued.get(agent_id) or [])
            remaining = [spec for spec in queue
                         if spec.trace_id not in cancelled]
            if remaining:
                self.queued[agent_id] = remaining
            else:
                self.queued.pop(agent_id, None)

    # --- durability --------------------------------------------------------

    def rehydrate(self, *, live: Callable[[str, str], bool],
                  requeue_parked: bool = True) -> dict[str, Any]:
        """Rebuild the view from the durable rows after a restart.

        An open ``turns`` row whose process ``live(agent_id, trace_id)`` can
        still see becomes the in-flight owner again; one without a process is
        left for ``interrupted_turns``/``reconcile`` to settle. Parked rows go
        back to ``queued`` so ``recover_queued`` re-admits them in order.
        """
        from . import turn_lifecycle, turn_queue
        adopted: dict[str, str] = {}
        for row in turn_lifecycle.open_turns():
            agent_id = str(row["agent_id"])
            trace_id = str(row["trace_id"] or "")
            if not trace_id:
                continue
            with self.lock:
                if agent_id in self.inflight:
                    continue
            if live(agent_id, trace_id):
                with self.lock:
                    self.inflight[agent_id] = trace_id
                adopted[agent_id] = trace_id
        requeued = turn_queue.requeue_parked() if requeue_parked else 0
        return {"adopted": adopted, "requeued_parked": requeued}

    def reset_for_tests(self) -> None:
        with self.lock:
            self.inflight.clear()
            self.queued.clear()
            self.claimed_at.clear()


# --- facts other modules own ----------------------------------------------------
# TODO(integration): terminal_ws and compaction are outside this stream. They
# are read through their public module functions here so that live_work() is
# the single query; once they expose typed services on the ServerContext the
# two helpers below take those instead of importing the modules.

def _terminal_live(agent_id: str) -> bool:
    try:
        from . import terminal_ws
        return terminal_ws.has_live_terminal(agent_id)
    except Exception:  # noqa: BLE001
        return False


def _compacting(session: str) -> bool:
    try:
        from . import compaction
        return session in set(compaction.active_sessions())
    except Exception:  # noqa: BLE001
        return False
