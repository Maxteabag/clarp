"""Coordinate account recovery for Claude turns owned by one Clarp runtime.

The dispatcher supplies its ownership lock and callbacks. Account credentials
stay in an explicitly configured local command, outside the Host database.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
import signal
import subprocess
import threading
import time
from typing import Callable

from .log import log

RECHECK_SECONDS = 60.0


def finish_owned_group(handle) -> None:
    """Reap the parent separately; ensure no non-zombie group member can work."""
    group = getattr(handle, "process_group", None)
    if not isinstance(group, int) or group <= 0:
        return
    # A tool can redirect stdout and ignore SIGTERM, so a drained parent is
    # insufficient proof. Kill remaining members even when the pipe is closed.
    handle.kill()
    deadline = time.monotonic() + 10
    while True:
        try:
            os.killpg(group, 0)
        except ProcessLookupError:
            return
        result = subprocess.run(
            ["ps", "-eo", "pgid=,stat="], capture_output=True, text=True,
            check=True, timeout=5)
        running = False
        for line in result.stdout.splitlines():
            fields = line.split()
            if (len(fields) >= 2 and fields[0] == str(group)
                    and not fields[1].startswith(("Z", "X"))):
                running = True
                break
        if not running:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError("Owned Claude process group has not stopped")
        time.sleep(0.05)


def switch_account(command: tuple[str, ...], models: list[str]) -> bool:
    """Invoke the local account selector without a shell or logging its output."""
    proc = subprocess.Popen(
        command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, start_new_session=os.name == "posix")
    try:
        stdout, _ = proc.communicate(json.dumps({"models": models}), timeout=300)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
        proc.communicate()
        return False
    if proc.returncode != 0:
        return False
    try:
        result = json.loads(stdout)
    except (ValueError, TypeError):
        return False
    return isinstance(result, dict) and result.get("available") is True


@dataclass
class Attempt:
    agent_id: str
    trace_id: str
    model: str
    state: dict
    owned: Callable[[], bool]
    pause: Callable[[], None]
    resume: Callable[[], None]
    handle: object = None
    spawned: threading.Event = field(default_factory=threading.Event)
    stopped: bool = False


class ClaudeFailover:
    def __init__(self, lock, *, switch=switch_account, schedule=None,
                 now=time.monotonic):
        self.lock = lock
        self.switch = switch
        self.schedule = schedule or self._schedule
        self.now = now
        self.attempts: dict[str, Attempt] = {}
        self.recovering = False
        self.command: tuple[str, ...] = ()
        self.next_check = 0.0

    @staticmethod
    def _schedule(delay, callback):
        timer = threading.Timer(delay, callback)
        timer.daemon = True
        timer.start()

    def register(self, attempt: Attempt) -> bool:
        """Return true if the new attempt must wait for account recovery."""
        with self.lock:
            self.attempts[attempt.agent_id] = attempt
            if self.recovering:
                attempt.state["account_recovery"] = True
                attempt.pause()
                attempt.spawned.set()
                return True
            return False

    def discard(self, agent_id, trace_id):
        with self.lock:
            attempt = self.attempts.get(agent_id)
            if attempt and attempt.trace_id == trace_id:
                self.attempts.pop(agent_id, None)

    def parked(self, agent_id, trace_id):
        """Whether cancellation owns work that has no remaining process."""
        with self.lock:
            attempt = self.attempts.get(agent_id)
            return bool(attempt and attempt.trace_id == trace_id
                        and attempt.state.get("account_recovery")
                        and (attempt.stopped or (
                            attempt.spawned.is_set() and attempt.handle is None)))

    def request(self, agent_id, trace_id, command) -> bool:
        with self.lock:
            trigger = self.attempts.get(agent_id)
            if not command or not trigger or trigger.trace_id != trace_id:
                return False
            if self.recovering:
                return True
            self.recovering = True
            self.command = tuple(command)
            for attempt in self.attempts.values():
                if attempt.owned():
                    attempt.state["account_recovery"] = True
                    attempt.pause()
        self.schedule(0.0, self.recover)
        return True

    def _pending(self):
        # Called with the dispatch lock held. A user Stop, deletion, or explicit
        # replacement takes ownership away and therefore cancels recovery.
        self.attempts = {key: value for key, value in self.attempts.items()
                         if value.owned()}
        return list(self.attempts.values())

    def recover(self):
        try:
            with self.lock:
                pending = self._pending()
            for attempt in pending:
                if attempt.stopped:
                    continue
                # A quota callback can arrive before spawn_turn has returned.
                # Wait for its exact handle, then finish draining before resume.
                if not attempt.spawned.wait(timeout=10):
                    raise RuntimeError("Claude spawn has not settled")
                handle = attempt.handle
                if handle is not None:
                    handle.terminate()
                    try:
                        handle.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        handle.kill()
                        handle.wait(timeout=10)
                    drain = getattr(handle, "drain_thread", None)
                    if drain is not None and drain.is_alive():
                        # wait() bounds the drainer join but does not raise
                        # when a descendant keeps stdout open after parent exit.
                        handle.kill()
                        handle.wait(timeout=10)
                        if drain.is_alive():
                            raise RuntimeError("Claude transcript is still draining")
                    finish_owned_group(handle)
                attempt.stopped = True
            with self.lock:
                pending = self._pending()
                if not pending:
                    self.recovering = False
                    return
                delay = self.next_check - self.now()
                if delay > 0:
                    self.schedule(delay, self.recover)
                    return
                models = sorted({attempt.model for attempt in pending})
                self.next_check = self.now() + RECHECK_SECONDS
            available = self.switch(self.command, models)
            if available:
                with self.lock:
                    pending = self._pending()
                    # New arrivals can introduce a model that was not checked.
                    # Keep them parked for another complete account check.
                    if any(item.model not in models for item in pending):
                        available = False
                    else:
                        self.attempts.clear()
                        self.recovering = False
                        for attempt in pending:
                            if attempt.owned():
                                attempt.resume()
                if available:
                    log("claudeAccountRecovered", f"turns={len(pending)}")
                    return
            log("claudeAccountWaiting", "No verified account; turns remain paused")
        except Exception as exc:  # Keep the owned work parked for a later check.
            log("claudeAccountRecoveryFail", type(exc).__name__)
        with self.lock:
            if not self._pending():
                self.recovering = False
                return
        self.schedule(RECHECK_SECONDS, self.recover)

    def status(self):
        with self.lock:
            return {"recovering": self.recovering,
                    "waiting": sorted(item.agent_id for item in self.attempts.values()
                                      if item.state.get("account_recovery") and item.owned())}
