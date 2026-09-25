"""Registry for the server's background workers.

`build_server` used to start some twenty threads inline, each followed by its
own `srv.on_close(...)`. A `WorkerSet` holds the same starts as `Worker`
records in one ordered list, starts them, registers every stop with the
server, logs one `workerStart` line per worker and keeps the running handles
for diagnostics (`/status` style dumps, tests, py-spy correlation).

Two stages exist because restart recovery has to run after the boot workers
(it needs the heartbeat scheduler) and before any listener that accepts
outside requests on its own thread. `BOOT` workers start first; `SERVING`
workers start after recovery. Within a stage the list order is the start
order and, since `ContextHTTPServer.server_close` runs callbacks in
registration order, also the stop order.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from .log import log

BOOT = "boot"
SERVING = "serving"
STAGES = (BOOT, SERVING)


def _always() -> bool:
    return True


def _stop_handle(handle: Any) -> None:
    handle.stop()


@dataclass(frozen=True)
class Worker:
    """One background worker.

    `start` builds the worker, starts it and returns the running handle. It
    returns None when the worker decided not to come up (Bonjour without a
    LAN address, a listener that failed to bind); no stop is registered then.
    `stop` receives that handle at server close. `enabled` is evaluated at
    start time, so config and ctx can be read when the server is built.
    """
    name: str
    start: Callable[[], Any]
    stop: Callable[[Any], None] = _stop_handle
    enabled: Callable[[], bool] = _always
    stage: str = BOOT
    # Why this worker sits where it does in the order, for readers.
    note: str = ""

    def __post_init__(self):
        if self.stage not in STAGES:
            raise ValueError(f"unknown worker stage {self.stage!r}")


@dataclass
class WorkerSet:
    workers: Sequence[Worker]
    running: dict[str, Any] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)
    _started_stages: set[str] = field(default_factory=set)

    def __post_init__(self):
        names = [worker.name for worker in self.workers]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"duplicate worker names: {duplicates}")

    def names(self, stage: str | None = None) -> list[str]:
        return [worker.name for worker in self.workers
                if stage is None or worker.stage == stage]

    def start(self, srv, stage: str = BOOT) -> list[str]:
        """Start every enabled worker of `stage` in registry order and
        register its stop with `srv.on_close`. Returns the names started.

        A start that raises propagates: the server must not come up half
        wired, and `srv.server_close()` still stops what already started.
        """
        if stage in self._started_stages:
            raise RuntimeError(f"worker stage {stage!r} already started")
        self._started_stages.add(stage)
        started: list[str] = []
        for worker in self.workers:
            if worker.stage != stage:
                continue
            if not worker.enabled():
                self.skipped.append(worker.name)
                continue
            handle = worker.start()
            if handle is None:
                self.skipped.append(worker.name)
                continue
            self.running[worker.name] = handle
            srv.on_close(_Stop(worker, handle))
            log("workerStart", f"name={worker.name}")
            started.append(worker.name)
        return started

    def get(self, name: str) -> Any:
        """The running handle for `name`, or None if it was skipped."""
        return self.running.get(name)

    def diagnostics(self) -> dict:
        return {"running": list(self.running), "skipped": list(self.skipped),
                "order": self.names()}


class _Stop:
    """Bound stop callback; a class so the callback list is readable when
    debugging `server_close`."""

    def __init__(self, worker: Worker, handle: Any):
        self.worker = worker
        self.handle = handle

    def __call__(self) -> None:
        self.worker.stop(self.handle)

    def __repr__(self) -> str:
        return f"<stop {self.worker.name}>"


def started(handle: Any) -> Any:
    """`start()` a freshly built worker and return it; the common `start`
    callable body for classes with a no-argument `start()`."""
    handle.start()
    return handle

