"""Shared in-flight subprocess registry for CLI-backed turns."""
from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable


@dataclass
class TurnHandle:
    proc: subprocess.Popen
    drain_thread: threading.Thread | None
    # Set only by runners that used start_new_session=True. Never infer ownership
    # from getpgid(): a caller-supplied process may share the runtime's group.
    process_group: int | None = None

    @property
    def pid(self) -> int:
        return self.proc.pid

    def wait(self, timeout: float | None = None) -> int:
        """Wait for the turn, including the transcript the turn writes.

        The subprocess exiting is not the end of a turn: the drainer thread
        parses its output and persists the conversation, and it is still
        finishing when the pipe closes. A caller waiting on a turn means "it
        is recorded", so join that too — otherwise a read straight after this
        can catch a half-written transcript.

        `timeout` bounds the whole wait, not each half of it.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        returncode = self.proc.wait(timeout=timeout)
        if self.drain_thread is not None:
            remaining = (
                None if deadline is None
                else max(0.0, deadline - time.monotonic()))
            self.drain_thread.join(timeout=remaining)
        return returncode

    def is_alive(self) -> bool:
        return self.proc.poll() is None

    def terminate(self) -> bool:
        """Send the stop signal to the owned turn group, including descendants."""
        if self.process_group is not None:
            try:
                os.killpg(self.process_group, signal.SIGTERM)
            except ProcessLookupError:
                return False
            return True
        if self.is_alive():
            self.proc.terminate()
            return True
        return False

    def kill(self) -> None:
        if self.process_group is not None:
            try:
                os.killpg(self.process_group, signal.SIGKILL)
            except ProcessLookupError:
                pass
        elif self.is_alive():
            self.proc.kill()


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
                if handle.terminate():
                    count += 1
            except Exception as error:
                self._log_exception(event, error, detail=agent_id)
        return count
