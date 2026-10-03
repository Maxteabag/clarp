"""Durable event relay from the runtime process into the HTTP SSE hub."""
from __future__ import annotations

import threading
import time

from . import agents as agents_db
from . import events
from .live_pacing import TrailingThrottle
from .log import log_exception
from .protocol import SSEType


_RUNTIME_MARKER = "_clarp_runtime_event"


class RuntimeEventStream:
    """Stream-compatible writer used by the runtime, with no local clients.

    ``transcript-updated`` is throttled here, before it is stored: a streaming
    reply would otherwise write one relay row per live-row update. The first
    wake-up of a burst goes at once and the last when the window closes.
    """

    TRANSCRIPT_MIN_INTERVAL_SEC = 0.25

    def __init__(self, *, clock=time.monotonic, schedule=None):
        self._transcript_throttle = TrailingThrottle(
            self.TRANSCRIPT_MIN_INTERVAL_SEC, clock=clock, schedule=schedule)

    def broadcast(self, event: dict) -> None:
        if dict(event).get("type") == SSEType.TRANSCRIPT_UPDATED:
            key = str(event.get("session") or event.get("agent_id") or "")
            self._transcript_throttle.submit(key, dict(event), self._record)
            return
        self._record(event)

    @staticmethod
    def _record(event: dict) -> None:
        agents_db.record_sse_event({**dict(event), _RUNTIME_MARKER: True})
        from . import live_hub
        live_hub.nudge()

    def broadcast_ephemeral(self, event: dict) -> None:
        # Runtime input edges are not useful without a connected HTTP server.
        # Persisting them would make a later reconnect replay an old action.
        return None

    def start(self) -> None:
        return None

    def stop(self, timeout: float = 0.0) -> None:
        return None


class RuntimeEventWatcher:
    """Relay newly persisted runtime events without recording them twice.

    The runtime nudges this watcher over the live stream (``poll_now``) right
    after it stores an event, so relaying no longer waits for the poll; the
    poll stays as the fallback (fast until the first nudge arrives).
    """

    INTERVAL_SEC = 0.1
    NUDGED_INTERVAL_SEC = 1.0

    def __init__(self, stream):
        self.stream = stream
        self._last_id = 0
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._nudged = False
        self._thread: threading.Thread | None = None

    def poll_now(self) -> None:
        self._nudged = True
        self._wake.set()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        from . import db
        row = db.conn().execute(
            "SELECT COALESCE(MAX(event_id),0) AS event_id FROM sse_events"
        ).fetchone()
        self._last_id = int(row["event_id"] if row else 0)
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="runtime-event-watcher")
        self._thread.start()

    def stop(self, timeout: float = 1.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(self.NUDGED_INTERVAL_SEC if self._nudged else self.INTERVAL_SEC)
            self._wake.clear()
            if self._stop.is_set():
                return
            try:
                self._poll_once()
            except Exception as exc:  # noqa: BLE001 - watcher must self-heal
                log_exception("runtimeEventWatcherFail", exc)

    def _poll_once(self) -> None:
        rows = agents_db.events_after(self._last_id)
        for row in rows:
            self._last_id = max(self._last_id, int(row.get("event_id") or 0))
            if not row.pop(_RUNTIME_MARKER, False):
                continue
            events.broadcast_ephemeral(self.stream, events.as_event(row))
