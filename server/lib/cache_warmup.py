"""Warm the Host's heavy read caches once after boot.

The sidebar's first requests after a restart used to pay for cold page cache
and empty result caches at once: the pair-room list took 18-37 s, the
snapshot 7 s, the model catalogue 5 s of CLI probing, and every trivial
request queued behind them. This worker runs those computations once in the
background, a moment after the listeners are up, so the first client finds
warm caches. Failures are logged and never fatal.
"""
from __future__ import annotations

import threading
import time

from .log import log, log_exception

START_DELAY_SEC = 1.5


def _steps():
    from . import agent_conversations, artifacts, message_previews, provider_capabilities
    return (
        ("pair-rooms", lambda: agent_conversations.list_conversations()),
        ("dashboard-previews", lambda: message_previews.dashboard_messages()),
        ("model-catalog", lambda: provider_capabilities.capability_catalog()),
        ("artifacts", lambda: artifacts.list_artifacts(limit=50, order="updated")),
    )


class CacheWarmupWorker:
    def __init__(self, *, delay_sec: float = START_DELAY_SEC):
        self._delay = delay_sec
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="cache-warmup", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        if self._stop.wait(self._delay):
            return
        for name, step in _steps():
            if self._stop.is_set():
                return
            started = time.perf_counter()
            try:
                step()
                log("cacheWarmup", f"step={name} ms={int((time.perf_counter() - started) * 1000)}")
            except Exception as exc:  # noqa: BLE001 - warm-up must never take the server down
                log_exception("cacheWarmupFailed", exc, detail=f"step={name}")
