"""Tail durable background-job changes and publish typed SSE wake-ups."""
from __future__ import annotations

import threading

from . import background_jobs, events
from .log import log_exception
from .timing import SERVER_TIMING


class BackgroundJobWatcher:
    INTERVAL_SEC = SERVER_TIMING.state_watcher_poll_sec

    def __init__(self, stream):
        self._statuses: dict[str, str] = {}
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
        changes = background_jobs.events_after(self._last_id)
        for event in changes:
            job = background_jobs.get(
                event["job_id"], reconcile=False,
                observed_at=int(event["observed_at"]))
            self._last_id = int(event["event_id"])
            if not job:
                continue
            events.broadcast(self.stream, events.background_job_updated(
                change_revision=self._last_id,
                observed_at=int(event["observed_at"]),
                job_id=job["job_id"],
                session=job["session"],
                agent_id=job["agent_id"],
                status=job["status"],
                job=job,
            ))
            # The agent row derives its background state from active jobs, so
            # a start or finish must reach the list without waiting for a poll.
            # Heartbeats also produce events; only a status change nudges.
            # Computer-owned jobs (updates, model installs) belong to no agent row.
            if job.get("agent_id") and self._statuses.get(job["job_id"]) != job["status"]:
                self._statuses[job["job_id"]] = job["status"]
                if job["status"] not in background_jobs.ACTIVE_STATUSES:
                    self._statuses.pop(job["job_id"], None)
                events.broadcast(self.stream, events.agent_roster("background-job"))
