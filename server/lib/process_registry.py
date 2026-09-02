"""Shared in-flight subprocess registry for CLI-backed turns."""
from __future__ import annotations

import subprocess
import threading
from dataclasses import dataclass
from typing import Callable


@dataclass
class TurnHandle:
    proc: subprocess.Popen
    drain_thread: threading.Thread | None

    @property
    def pid(self) -> int:
        return self.proc.pid

    def wait(self, timeout: float | None = None) -> int:
        return self.proc.wait(timeout=timeout)

    def is_alive(self) -> bool:
        return self.proc.poll() is None


class ProcessRegistry:
    def __init__(self, *, log_exception: Callable):
        self._active: dict[str, list[TurnHandle]] = {}
        self._lock = threading.Lock()
        self._log_exception = log_exception

    def active_handles(self, agent_id: str) -> list[TurnHandle]:
        with self._lock:
            return list(self._active.get(agent_id, []))

    def register(self, agent_id: str, handle: TurnHandle) -> None:
        with self._lock:
            self._active.setdefault(agent_id, []).append(handle)

    def unregister(self, agent_id: str, handle: TurnHandle) -> None:
        with self._lock:
            handles = self._active.get(agent_id)
            if handles and handle in handles:
                handles.remove(handle)
            if handles is not None and not handles:
                self._active.pop(agent_id, None)

    def interrupt(self, agent_id: str, *, event: str) -> int:
        count = 0
        for handle in self.active_handles(agent_id):
            try:
                if handle.is_alive():
                    handle.proc.terminate()
                    count += 1
            except Exception as error:
                self._log_exception(event, error, detail=agent_id)
        return count
