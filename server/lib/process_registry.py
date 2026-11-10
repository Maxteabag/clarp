"""Shared in-flight subprocess registry for CLI-backed turns."""
from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable


# An interrupted turn whose process group is still there this long after its
# SIGTERM is killed (BACKENDS-07): a CLI or a tool child that ignores SIGTERM
# must not hold the agent for ever. The same 10 s as account_failover's stop.
STOP_KILL_DEADLINE_SEC = 10.0
_GROUP_POLL_SEC = 0.2


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
        drain = self.drain_thread
        if drain is not None and drain is not threading.current_thread():
            remaining = (
                None if deadline is None
                else max(0.0, deadline - time.monotonic()))
            drain.join(timeout=remaining)
            if drain.is_alive():
                raise subprocess.TimeoutExpired(self.proc.args, timeout)
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

    def outlived_stop(self) -> bool:
        """A member of the turn's own process group still runs."""
        if self.process_group is None:
            return self.is_alive()
        try:
            os.killpg(self.process_group, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

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
                    self._escalate_later(agent_id, handle, event)
            except Exception as error:
                self._log_exception(event, error, detail=agent_id)
        return count

    def _escalate_later(self, agent_id: str, handle: TurnHandle, event: str) -> None:
        """Kill the turn if SIGTERM has not ended it by the stop deadline."""
        def escalate() -> None:
            # The grace runs from the SIGTERM, for the whole turn: a leader that
            # exits at once does not cut short the grace of a child in its group.
            deadline = time.monotonic() + STOP_KILL_DEADLINE_SEC
            timed_out = False
            try:
                handle.proc.wait(timeout=STOP_KILL_DEADLINE_SEC)
            except subprocess.TimeoutExpired:
                timed_out = True
            if handle.process_group is not None:
                while handle.outlived_stop() and time.monotonic() < deadline:
                    time.sleep(_GROUP_POLL_SEC)
            try:
                # Without its own group, the turn is its leader: kill it only if
                # it outlived the deadline. With a group, any member still
                # there at the deadline (a tool child holding it) is the turn
                # outliving its stop.
                if (handle.outlived_stop() if handle.process_group is not None
                        else timed_out and handle.is_alive()):
                    handle.kill()
                    from .log import log
                    log("turnStopKilled", f"agent={agent_id} pid={handle.proc.pid} "
                        f"after={STOP_KILL_DEADLINE_SEC:g}s")
            except Exception as error:
                self._log_exception(f"{event}Kill", error, detail=agent_id)

        threading.Thread(target=escalate, daemon=True,
                         name=f"turn-stop-escalation-{agent_id}").start()
