"""Background worker that delivers decision answers to their agents.

Native decision cards (approvals, questions, HTML forms) are answered on the
phone and land as rows in SQLite. Nothing wakes the agent at that moment: this
worker polls, lets `artifacts.attention()` materialize expirations and
deliveries, and then hands the pending rows to the delivery callback (see
`lib.dispatch_adapters.DispatchAdapters.deliver_decision_rows`). It used to be
an anonymous thread inside `build_server`; the thread name is unchanged so
py-spy dumps and diagnostics keep reading the same.
"""
from __future__ import annotations

import threading
from typing import Callable

from .log import log_exception
from .timing import SERVER_TIMING

THREAD_NAME = "decision-delivery"


class DecisionDeliveryWorker:
    INTERVAL_SEC = SERVER_TIMING.decision_delivery_interval_sec

    def __init__(self, deliver: Callable[[], None], *,
                 interval_sec: float | None = None):
        self._deliver = deliver
        self.interval_sec = self.INTERVAL_SEC if interval_sec is None else interval_sec
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name=THREAD_NAME)
        self._thread.start()

    def stop(self, timeout: float = 0.0) -> None:
        """Signal the loop to exit. The thread is a daemon; callers that need
        it gone before continuing pass a join timeout."""
        self._stop.set()
        if timeout > 0 and self._thread is not None:
            self._thread.join(timeout=timeout)

    def run_once(self) -> None:
        """One delivery pass; failures are logged, never raised, so one bad
        row cannot stop the loop."""
        try:
            from . import artifacts
            artifacts.attention()  # materializes expirations + deliveries
            self._deliver()
        except Exception as exc:
            log_exception("decisionDeliveryWorkerFail", exc)

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_sec):
            self.run_once()
