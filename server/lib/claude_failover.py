"""Coordinate account recovery for Claude turns owned by one Clarp runtime.

The dispatcher supplies its ownership lock and callbacks. Account credentials
stay in an explicitly configured local command, outside the Host database.
What to do with the parked work is ``policies.failover.FailoverPlan``; this
module gathers the process state, asks, and terminates, kills, waits, resumes.
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
from .policies.failover import (
    FAILED, NOT_CHECKED, PARK_EXPIRED, FailoverPlan, FailoverSettings, Kill,
    Park, PendingWork, Release, Resume)

RECHECK_SECONDS = 60.0
# Owned work is never left parked silently for ever. A turn released into a real
# usage limit fails visibly and the app shows why; a turn parked indefinitely
# just looks like the agent stopped answering, with nothing for the user to act
# on. Verified 2026-09-17: a selector that could not reach its usage API held
# every Claude agent on this Host for three and a half hours.
MAX_PARK_SECONDS = 1800.0


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


def switch_account(command: tuple[str, ...], models: list[str]) -> bool | None:
    """Invoke the local account selector without a shell or logging its output.

    Three answers, which the caller must keep apart:
      True   an account serves every requested model.
      False  the selector checked and none does - wait for a reset.
      None   no verdict: it timed out, exited non-zero, answered in a shape we
             cannot read, or reported its own failure. That says nothing about
             quota, so it must never be read as "no quota".
    """
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
        return None
    if proc.returncode != 0:
        return None
    try:
        result = json.loads(stdout)
    except (ValueError, TypeError):
        return None
    if not isinstance(result, dict):
        return None
    available = result.get("available")
    if available is True:
        return True
    # The selector reports its own crash as {"available": false, "error": ...}.
    # Only a bare false is evidence about quota.
    if available is False and not result.get("error"):
        return False
    return None


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
        self.parked_since = 0.0
        self.checked_models: tuple[str, ...] = ()

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
            self.parked_since = self.now()
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

    def _plan(self) -> FailoverPlan:
        return FailoverPlan(parked_since=self.parked_since, next_check=self.next_check,
                            checked_models=self.checked_models)

    def _decide(self, pending, verdict, settings):
        snapshot = [PendingWork(model=item.model, stopped=item.stopped) for item in pending]
        return self._plan().decide(snapshot, verdict, self.now(), settings)

    @staticmethod
    def _stop_owned(attempt: Attempt) -> None:
        """Terminate one owned process and make sure nothing of it can work."""
        if attempt.stopped:
            return
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

    def recover(self):
        settings = FailoverSettings(recheck_seconds=RECHECK_SECONDS,
                                    max_park_seconds=MAX_PARK_SECONDS)
        try:
            with self.lock:
                pending = self._pending()
            if isinstance(self._decide(pending, NOT_CHECKED, settings), Kill):
                for attempt in pending:
                    self._stop_owned(attempt)
            with self.lock:
                pending = self._pending()
                decision = self._decide(pending, NOT_CHECKED, settings)
                if isinstance(decision, Release):
                    self.recovering = False
                    return
                if isinstance(decision, Park):
                    self.schedule(decision.delay, self.recover)
                    return
                self.next_check = self.now() + settings.recheck_seconds
                self.checked_models = decision.models
            verdict = self.switch(self.command, list(decision.models))
        except Exception as exc:  # Keep the owned work parked for a later check.
            log("claudeAccountRecoveryFail", type(exc).__name__)
            verdict = FAILED
        # Every exit from a failed check lands here, including a raised one, so
        # this is the one place that can guarantee work is not parked for ever.
        with self.lock:
            pending = self._pending()
            decision = self._decide(pending, verdict, settings)
            if isinstance(decision, Resume):
                self.attempts.clear()
                self.recovering = False
                for attempt in pending:
                    if attempt.owned():
                        attempt.resume()
            elif isinstance(decision, Release):
                self.recovering = False
                if decision.reason == PARK_EXPIRED:
                    self.attempts.clear()
        if isinstance(decision, Resume):
            log("claudeAccountRecovered", f"turns={len(pending)}")
            return
        if verdict is None:
            log("claudeAccountCheckInconclusive",
                "selector returned no verdict; quota is unknown")
        elif verdict is not FAILED:
            log("claudeAccountWaiting", "No verified account; turns remain paused")
        if isinstance(decision, Release):
            if decision.reason == PARK_EXPIRED:
                log("claudeAccountParkExpired",
                    f"released={len(pending)} after {MAX_PARK_SECONDS:.0f}s without a "
                    "usable account; the turns run and fail visibly instead")
                for attempt in pending:
                    if attempt.owned():
                        attempt.resume()
            return
        self.schedule(decision.delay, self.recover)

    def status(self):
        with self.lock:
            return {"recovering": self.recovering,
                    "waiting": sorted(item.agent_id for item in self.attempts.values()
                                      if item.state.get("account_recovery") and item.owned())}
