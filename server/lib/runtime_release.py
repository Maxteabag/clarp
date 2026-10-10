"""Hand an old runtime over to a newly installed runtime release.

When the installed release differs from the running one, the monitor raises
the admission fence (turn_dispatch: no turn starts, every arrival is admitted
to the durable queue, in-flight turns finish normally) and hands over the
moment the runtime owns nothing a restart would cut short. Nothing is ever
interrupted. The wait is bounded: when ``budget_sec`` passes first the fence is
lifted (held work starts, nothing lost or repeated) and the next attempt waits
an exponentially growing, capped backoff. A fully idle runtime hands over at
once, as it always did.

The budget comes from the live Host's turn history (state_log/turns, 7 days to
2026-10-10, docs/runtime-restarts.md "Graceful release drain"): with the fence
raised at a random minute, every in-flight turn had finished within 15 minutes
60% of the time (p50 6 min); 15 minutes bounds how long a held message waits,
and with a 5-minute backoff doubling to 60 the handoff landed within 1 hour
78% and within 6 hours 96% of the time in replay. A longer budget gained
little (30 min: 81% within 1 hour) while doubling the worst wait.
"""
from __future__ import annotations

import pathlib
import os
import threading
import time
from collections.abc import Callable
from typing import Any

from .log import log, log_exception

DRAIN_BUDGET_SEC = 900.0
DRAIN_BACKOFF_BASE_SEC = 300.0
DRAIN_BACKOFF_CAP_SEC = 3600.0


def read_runtime_release_id(root: pathlib.Path | str) -> str:
    root = pathlib.Path(root)
    if not (root / "RUNTIME_READY").is_file():
        return ""
    try:
        return (root / "RUNTIME_RELEASE_ID").read_text().strip()
    except OSError:
        return ""


def read_installed_release_id(root: pathlib.Path | str) -> str:
    """The release id a drain may target: its runtime is ready and the whole
    install finished (``INSTALL_OK``, written after the health check). A
    release still installing, or one an install is rolling back, is not."""
    if not (pathlib.Path(root) / "INSTALL_OK").is_file():
        return ""
    return read_runtime_release_id(root)


def mark_clean_handoff(path: pathlib.Path | str) -> None:
    target = pathlib.Path(path)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.next")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(descriptor, b"clean\n")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    temporary.replace(target)


def consume_clean_handoff(path: pathlib.Path | str) -> bool:
    target = pathlib.Path(path)
    try:
        target.unlink()
        return True
    except FileNotFoundError:
        return False


class RuntimeReleaseMonitor:
    """Drive one runtime's graceful handoff. ``runtime`` is the
    RuntimeRPCServer (begin_drain_if_idle, begin_admission_fence,
    seal_if_drained, release_admission_fence, cancel_handover, shutdown)."""

    def __init__(
        self,
        runtime,
        *,
        running_release_id: str,
        desired_release_id: Callable[[], str],
        before_shutdown: Callable[[], None] | None = None,
        interval_sec: float = 1.0,
        budget_sec: float = DRAIN_BUDGET_SEC,
        backoff_base_sec: float = DRAIN_BACKOFF_BASE_SEC,
        backoff_cap_sec: float = DRAIN_BACKOFF_CAP_SEC,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
    ):
        self.runtime = runtime
        self.running_release_id = str(running_release_id or "")
        self.desired_release_id = desired_release_id
        self.before_shutdown = before_shutdown or (lambda: None)
        self.interval_sec = interval_sec
        self.budget_sec = float(budget_sec)
        self.backoff_base_sec = float(backoff_base_sec)
        self.backoff_cap_sec = float(backoff_cap_sec)
        self.clock = clock
        self.wall = wall
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        # idle -> draining -> (handed_over | backoff -> draining ...)
        self._phase = "idle"
        self._target = ""
        self._attempt = 0
        self._fenced_at = 0.0
        self._deadline = 0.0
        self._next_attempt = 0.0
        self._blockers: dict[str, list[str]] = {}
        self._last_outcome = ""

    # --- status --------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """The drain as runtime status shows it. Times are wall-clock ms."""
        with self._lock:
            now, wall = self.clock(), self.wall()

            def at(moment: float) -> int | None:
                return int((wall + moment - now) * 1000) if moment else None

            return {
                "phase": self._phase,
                "running_release": self.running_release_id,
                "target_release": self._target,
                "attempt": self._attempt,
                "fenced_since": at(self._fenced_at) if self._phase == "draining" else None,
                "deadline": at(self._deadline) if self._phase == "draining" else None,
                "next_attempt_at": at(self._next_attempt) if self._phase == "backoff" else None,
                "blockers": {key: list(value) for key, value in self._blockers.items()},
                "last_outcome": self._last_outcome,
            }

    def _set(self, **fields: Any) -> None:
        with self._lock:
            for key, value in fields.items():
                setattr(self, f"_{key}", value)

    # --- one poll ------------------------------------------------------------

    def check_once(self) -> bool:
        """Advance the drain; True once the runtime has been shut down."""
        desired = str(self.desired_release_id() or "")
        if not desired or desired == self.running_release_id:
            self._withdraw()
            return False
        now = self.clock()
        if self._phase != "draining":
            if self.runtime.begin_drain_if_idle():
                return self._hand_over(desired)
            if self._phase == "backoff" and now < self._next_attempt:
                self._set(target=desired)
                return False
            self._fence(desired, now)
        elif desired != self._target:
            # A newer release superseded the target: keep the one fence and
            # its deadline, aim at the newest. One handoff, never two.
            log("runtimeDrainRetarget", f"from={self._target} to={desired}")
            self._set(target=desired)
        blockers = self.runtime.seal_if_drained()
        self._set(blockers=blockers)
        if not blockers:
            return self._hand_over(desired)
        if self.clock() >= self._deadline:
            self._revert("budget expired", now=self.clock())
        return False

    def _fence(self, target: str, now: float) -> None:
        self.runtime.begin_admission_fence()
        attempt = self._attempt + 1
        self._set(phase="draining", target=target, attempt=attempt,
                  fenced_at=now, deadline=now + self.budget_sec, blockers={})
        log("runtimeDrainStarted",
            f"running={self.running_release_id} target={target} "
            f"attempt={attempt} budget={self.budget_sec:g}s")

    def _revert(self, reason: str, *, now: float, backoff: bool = True) -> None:
        """Lift the fence; held work starts. With ``backoff`` the next attempt
        waits base * 2**(attempt-1), capped."""
        self.runtime.release_admission_fence()
        if backoff:
            delay = min(self.backoff_cap_sec,
                        self.backoff_base_sec * 2 ** max(0, self._attempt - 1))
            self._set(phase="backoff", next_attempt=now + delay,
                      last_outcome=reason)
        else:
            delay = 0.0
            self._set(phase="idle", attempt=0, target="", next_attempt=0.0,
                      blockers={}, last_outcome=reason)
        log("runtimeDrainReverted",
            f"target={self._target} attempt={self._attempt} reason={reason} "
            f"blockers={ {key: len(value) for key, value in self._blockers.items()} } "
            f"next_attempt_in={delay:g}s")

    def _withdraw(self) -> None:
        """The installed release is the running one again (a rollback) or is
        not ready: abort a drain cleanly and forget the backoff."""
        if self._phase == "draining":
            self._revert("target withdrawn", now=self.clock(), backoff=False)
        elif self._phase == "backoff":
            self._set(phase="idle", attempt=0, target="", next_attempt=0.0,
                      blockers={})

    def _hand_over(self, target: str) -> bool:
        """The runtime is sealed (hard fence). Re-read the installed release:
        when it was rolled back or lost readiness meanwhile, unseal and stay."""
        desired = str(self.desired_release_id() or "")
        if not desired or desired == self.running_release_id:
            self.runtime.cancel_handover()
            self._withdraw()
            return False
        self._set(phase="handed_over", target=desired, blockers={},
                  last_outcome="handed over")
        log("runtimeDrainHandover",
            f"running={self.running_release_id} target={desired} "
            f"attempt={self._attempt}")
        self.before_shutdown()
        self.runtime.shutdown()
        return True

    # --- thread --------------------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="runtime-release-monitor")
        self._thread.start()

    def stop(self, timeout: float = 1.0) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_sec):
            try:
                if self.check_once():
                    return
            except Exception as exc:  # noqa: BLE001 - retry on next poll
                log_exception("runtimeReleaseMonitorFail", exc)
