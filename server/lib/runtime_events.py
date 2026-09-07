"""Durable event relay from the runtime process into the HTTP SSE hub."""
from __future__ import annotations

import threading

from . import agents as agents_db
from .log import log_exception


_RUNTIME_MARKER = "_clarp_runtime_event"


class RuntimeEventStream:
    """Stream-compatible writer used by the runtime, with no local clients."""

    def broadcast(self, event: dict) -> None:
        agents_db.record_sse_event({**dict(event), _RUNTIME_MARKER: True})

    def broadcast_ephemeral(self, event: dict) -> None:
        # Runtime input edges are not useful without a connected HTTP server.
        # Persisting them would make a later reconnect replay an old action.
        return None

    def start(self) -> None:
        return None

    def stop(self, timeout: float = 0.0) -> None:
        return None


class RuntimeEventWatcher:
    """Relay newly persisted runtime events without recording them twice."""

    INTERVAL_SEC = 0.1

    def __init__(self, stream):
        self.stream = stream
        self._last_id = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

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
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    def _loop(self) -> None:
        while not self._stop.wait(self.INTERVAL_SEC):
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
            self.stream.broadcast_ephemeral(row)
