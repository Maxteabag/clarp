"""Tail durable background-job changes and publish typed SSE wake-ups."""
from __future__ import annotations

import threading

from . import background_jobs
from .log import log_exception
from .protocol import SSEType
from .timing import SERVER_TIMING


class BackgroundJobWatcher:
    INTERVAL_SEC = SERVER_TIMING.state_watcher_poll_sec

    def __init__(self, stream):
        self.stream = stream
        self._last_id = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._last_id = background_jobs.latest_event_id()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 1.0) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    def _loop(self) -> None:
        while not self._stop.wait(self.INTERVAL_SEC):
            try:
                background_jobs.reconcile_stale()
                self._poll_once()
            except Exception as exc:  # noqa: BLE001
                log_exception("backgroundJobWatcherTickFail", exc)

    def _poll_once(self) -> None:
        events = background_jobs.events_after(self._last_id)
        for event in events:
            job = background_jobs.get(
                event["job_id"], reconcile=False,
                observed_at=int(event["observed_at"]))
            self._last_id = int(event["event_id"])
            if not job:
                continue
            self.stream.broadcast({
                "type": SSEType.BACKGROUND_JOB_UPDATED,
                "change_revision": self._last_id,
                "observed_at": int(event["observed_at"]),
                "job_id": job["job_id"],
                "session": job["session"],
                "agent_id": job["agent_id"],
                "status": job["status"],
                "job": job,
            })
