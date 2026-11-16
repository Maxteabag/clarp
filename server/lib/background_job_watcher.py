"""Tail durable background-job changes and publish typed SSE wake-ups."""
from __future__ import annotations

import threading
import time

from . import background_jobs, events, reconcile, stale_work
from .log import log_exception
from .timing import SERVER_TIMING


class BackgroundJobWatcher:
    INTERVAL_SEC = SERVER_TIMING.state_watcher_poll_sec
    # Helper and status staleness is measured in tens of minutes; once a
    # minute is plenty and keeps the per-agent scan off the fast tick.
    STALE_WORK_INTERVAL_SEC = 60.0
    PROVIDER_INTERVAL_SEC = 5.0
    # A queue receipt the pass could not decide is looked at again after this
    # long, then after twice that, up to the cap. Deciding one reads the
    # agent's whole state history, and one that stays undecidable (a message
    # queued weeks ago for an agent that never ran it) was read again every
    # minute: 170 MB a pass on a Host with 56 such rows (issue 363).
    RECEIPT_RECHECK_SEC = 120.0
    RECEIPT_RECHECK_MAX_SEC = 3600.0

    def __init__(self, stream):
        self._statuses: dict[str, tuple[int, str]] = {}
        self.stream = stream
        self._last_id = 0
        self._stale_work_due = 0.0
        self._provider_due = 0.0
        # (queue_id, queue_seq) -> (not before, the wait that led there),
        # monotonic seconds. A queue id comes again (goal wakes, peer
        # requests); the new row must not inherit the old one's wait.
        self._receipt_waits: dict[tuple[str, int], tuple[float, float]] = {}
        from .provider_background_jobs import ProviderJobObserver
        self._provider_observer = ProviderJobObserver()
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
            self._tick()
            if time.monotonic() >= self._stale_work_due:
                self._stale_work_due = time.monotonic() + self.STALE_WORK_INTERVAL_SEC
                try:
                    stale_work.sweep(self.stream)
                except Exception as exc:  # noqa: BLE001
                    log_exception("staleWorkSweepFail", exc)
                try:
                    # A receipt the ledger never settled must not wait for a
                    # runtime restart to end (turn_queue, idle_agent).
                    reconcile.end_idle_agent_receipts_now(
                        skip=self._receipt_waiting, on_deferred=self._receipt_deferred)
                except Exception as exc:  # noqa: BLE001
                    log_exception("idleReceiptSweepFail", exc)

    def _receipt_waiting(self, queue_id: str, queue_seq: int, *, now: float | None = None) -> bool:
        """This receipt was undecidable a moment ago; not yet time to look again."""
        entry = self._receipt_waits.get((queue_id, queue_seq))
        return entry is not None and (time.monotonic() if now is None else now) < entry[0]

    def _receipt_deferred(self, queue_id: str, queue_seq: int, reason: str = "", *,
                          now: float | None = None) -> None:
        """The pass could not decide this receipt: look again later, each time
        after twice as long, and forget the ones that are no longer deferred."""
        now = time.monotonic() if now is None else now
        previous = self._receipt_waits.get((queue_id, queue_seq))
        wait = self.RECEIPT_RECHECK_SEC if previous is None else min(
            previous[1] * 2, self.RECEIPT_RECHECK_MAX_SEC)
        self._receipt_waits[queue_id, queue_seq] = (now + wait, wait)
        # A receipt that was decided, or is gone, is never deferred again and
        # so never refreshed: drop it once it is well past its time.
        stale = [key for key, (due, _wait) in self._receipt_waits.items()
                 if now - due > 2 * self.RECEIPT_RECHECK_MAX_SEC]
        for key in stale:
            del self._receipt_waits[key]

    def _tick(self) -> None:
        # Publish managed-job progress before any provider backlog work.
        try:
            background_jobs.reconcile_stale()
            self._poll_once()
        except Exception as exc:
            log_exception("backgroundJobWatcherTickFail", exc)
        try:
            if time.monotonic() >= self._provider_due:
                self._provider_due = time.monotonic() + self.PROVIDER_INTERVAL_SEC
                self._provider_observer.poll_once()
                if self._provider_observer.continue_soon:
                    self._provider_due = time.monotonic()
                self._poll_once()
        except Exception as exc:
            log_exception("providerBackgroundWatcherTickFail", exc)

    def _poll_once(self) -> None:
        changes = background_jobs.events_after(self._last_id)
        for event in changes:
            job = background_jobs.get(
                event["job_id"], reconcile=False,
                observed_at=int(event["observed_at"]))
            self._last_id = int(event["event_id"])
            if not job:
                continue
            job = background_jobs.event_projection(job, int(event["observed_at"]))
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
            # Computer-owned jobs normally belong to no agent row, except
            # persona-avatar jobs whose completion swaps in Contact + Agent
            # portraits live.
            state_key = (int(job.get("generation") or 1), str(job["status"]))
            previous_state = self._statuses.get(job["job_id"])
            if previous_state != state_key:
                self._statuses[job["job_id"]] = state_key
                if len(self._statuses) > 1024:
                    for stale_id in list(self._statuses)[:256]:
                        if self._statuses[stale_id][1] not in background_jobs.ACTIVE_STATUSES:
                            self._statuses.pop(stale_id, None)
                if job.get("agent_id"):
                    events.broadcast(self.stream, events.agent_roster("background-job"))
                elif job.get("kind") == "persona-avatar" and (
                    job["status"] in background_jobs.TERMINAL_STATUSES
                    or (
                        previous_state is not None
                        and previous_state[1] in background_jobs.TERMINAL_STATUSES
                        and job["status"] in background_jobs.ACTIVE_STATUSES
                    )
                ):
                    persona_name = ""
                    if isinstance(job.get("metadata"), dict):
                        persona_name = str(job["metadata"].get("persona_name") or "").strip()
                    events.broadcast(
                        self.stream,
                        events.agent_roster(
                            "persona-updated",
                            persona=persona_name if persona_name else events.OMIT,
                        ),
                    )
