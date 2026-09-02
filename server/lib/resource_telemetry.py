"""Low-frequency, opt-in host/process pressure samples."""
from __future__ import annotations

import os
import pathlib
import resource
import threading

from . import db, diagnostics_settings, eventlog, telemetry


def _pressure(name: str) -> dict[str, float]:
    path = pathlib.Path("/proc/pressure") / name
    try:
        first = path.read_text().splitlines()[0].split()
        values = dict(item.split("=", 1) for item in first[1:])
        return {f"{name}_{key}": float(values[key])
                for key in ("avg10", "avg60") if key in values}
    except (OSError, ValueError, IndexError):
        return {}


def sample() -> dict:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    detail: dict[str, object] = {
        "process_rss_kb": int(usage.ru_maxrss),
        "process_threads": threading.active_count(),
        "load_1m": round(os.getloadavg()[0], 3),
        "state_bytes": db.DB_PATH.stat().st_size if db.DB_PATH.exists() else 0,
        "telemetry_bytes": (
            telemetry.TELEMETRY_PATH.stat().st_size
            if telemetry.TELEMETRY_PATH.exists() else 0),
    }
    for name in ("cpu", "memory", "io"):
        detail.update(_pressure(name))
    return detail


class ResourceTelemetryWorker:
    def __init__(self, interval_sec: float = 15.0):
        self.interval_sec = interval_sec
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="resource-telemetry")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_sec):
            if diagnostics_settings.allows("resources"):
                eventlog.emit("resources", "sample", detail=sample())
