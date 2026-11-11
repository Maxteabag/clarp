"""Coordinate account recovery for the turns of one account pool (Claude,
Codex) owned by one Clarp runtime.

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
# A recovery carried over a runtime handoff waits this long at most for the
# work it carried to be re-admitted before an empty pass may end it.
CARRIED_WORK_WAIT_SECONDS = 120.0
CARRIED_WORK_POLL_SECONDS = 1.0
# A turn that fails on its account this soon after a recovery resumed it has
# not left its park: what happens next continues it.
# - A plan failure ends the turn with the provider's own sentence. The
#   selector said an account offers the model, and it does not; asking again
#   is a loop (2026-11-11: 146 rounds in three and a half hours, every running
#   turn of the pool stopped and resumed in each).
# - A usage limit or a failed sign-in goes back into the park. The selector
#   can pick an account with a sliver left (2026-11-11 14:28 and 14:32 on
#   laptopstudio: 20%, then 6%), and waiting does help: those turns resumed at
#   15:01 and worked. But the park's clock is not started again, so the
#   MAX_PARK_SECONDS cap counts from the turn's first park and stays the one
#   place that gives up.
RESUME_GRACE_SECONDS = 120.0
# A turn parked because the account does not offer its model waits for this
# many selector answers of "no saved account does" and then ends. A plan is
# not a usage window: nothing resets, so it does not get the half hour.
PLAN_CHECKS = 3


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


def switch_account(command: tuple[str, ...], models: list[str]) -> bool | frozenset | None:
    """Invoke the local account selector without a shell or logging its output.

    Four answers, which the caller must keep apart:
      True       an account serves every requested model.
      frozenset  an account is active that serves only these of the requested
                 models (the selector's ``models`` list); no saved account
                 serves the rest. Never empty.
      False      the selector checked and none does - wait for a reset.
      None       no verdict: it timed out, exited non-zero, answered in a shape
                 we cannot read, or reported its own failure. That says
                 nothing about quota, so it must never be read as "no quota".
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
        if "models" not in result:
            return True                 # a selector that does not say: all of them
        served = result["models"]
        if not isinstance(served, list) or not all(isinstance(m, str) for m in served):
            return None
        covered = frozenset(served) & frozenset(models)
        if covered >= frozenset(models):
            return True
        # "Available" for none of what was asked is no readable answer.
        return covered or None
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
    # The dispatcher's own record of the parked work (its spec and attempt
    # number), read only by the release drain's transfer (turn_dispatch).
    work: object = None
    # Ends the turn visibly, saying that no saved account offers its model;
    # without it such work is resumed to fail by itself, as it always was.
    fail: Callable[[], None] | None = None
    # Parked because its own model is not offered by the account (a plan
    # failure), and how often the selector has since answered that no saved
    # account offers it. It ends after PLAN_CHECKS of those.
    plan: bool = False
    unserved_checks: int = 0


class AccountFailover:
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
        # Recovery passes running (deciding, stopping processes, or asking
        # the selector); a timer and a new request can overlap. Parked work
        # is never handed over while any runs.
        self.checking = 0
        # Agents whose parked work the previous runtime handed over and that
        # have not registered here yet, and until when to wait for them.
        self.awaiting: set[str] = set()
        self.awaiting_until = 0.0
        # Work released when its park reached MAX_PARK_SECONDS: (agent, trace).
        # It runs and fails visibly; it is never parked again (BACKENDS-05).
        self.park_expired: set[tuple[str, str]] = set()
        # When a recovery last resumed each turn: (agent, trace) -> clock.
        self.resumed_at: dict[tuple[str, str], float] = {}
        # When each turn's park began. A park a recovery resumed and that the
        # turn fell back into within RESUME_GRACE_SECONDS is the same park.
        self.first_parked: dict[tuple[str, str], float] = {}

    @staticmethod
    def _schedule(delay, callback):
        timer = threading.Timer(delay, callback)
        timer.daemon = True
        timer.start()

    def register(self, attempt: Attempt) -> bool:
        """Return true if the new attempt must wait for account recovery."""
        with self.lock:
            # A new trace for the agent ends the old one's expired mark.
            self.park_expired = {key for key in self.park_expired
                                 if key[0] != attempt.agent_id or key[1] == attempt.trace_id}
            self.resumed_at = {key: at for key, at in self.resumed_at.items()
                               if key[0] != attempt.agent_id or key[1] == attempt.trace_id}
            self.first_parked = {key: at for key, at in self.first_parked.items()
                                 if key[0] != attempt.agent_id or key[1] == attempt.trace_id}
            self.attempts[attempt.agent_id] = attempt
            self.awaiting.discard(attempt.agent_id)
            if self.recovering and not self._expired(attempt):
                attempt.state["account_recovery"] = True
                self._note_park(attempt)
                attempt.pause()
                attempt.spawned.set()
                return True
            return False

    def discard(self, agent_id, trace_id):
        with self.lock:
            self.park_expired.discard((agent_id, trace_id))
            self.resumed_at.pop((agent_id, trace_id), None)
            self.first_parked.pop((agent_id, trace_id), None)
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

    def _same_park(self, key: tuple[str, str]) -> bool:
        """A recovery resumed this turn so recently that it has not left its
        park. Called with the lock held."""
        resumed = self.resumed_at.get(key)
        return resumed is not None and self.now() - resumed < RESUME_GRACE_SECONDS

    def _note_park(self, attempt: Attempt) -> None:
        """Start the turn's park clock, unless this is its park going on."""
        key = (attempt.agent_id, attempt.trace_id)
        if not (key in self.first_parked and self._same_park(key)):
            self.first_parked[key] = self.now()

    def request(self, agent_id, trace_id, command, *, plan=False) -> bool:
        """Start (or join) the pool's recovery for a turn that failed on its
        account; False when that turn has to fail visibly instead.

        ``plan`` is for a plan failure ("this account does not offer this
        model"): a fact about one turn's model, not about the account's
        quota. Recovery stops every running turn of the pool, because the
        selector switches the machine's account while it probes. That is
        right when the account is out for all of them and wrong when the
        others run fine, so such a failure starts a recovery only when no
        other turn of the pool is running, and never again right after a
        recovery resumed the turn (see RESUME_GRACE_SECONDS)."""
        with self.lock:
            trigger = self.attempts.get(agent_id)
            if not command or not trigger or trigger.trace_id != trace_id:
                return False
            key = (agent_id, trace_id)
            if key in self.park_expired:
                return False   # its park already expired: fail visibly now
            if self._same_park(key):
                if plan:
                    return False   # resumed onto an account said to offer it; it does not
                if self.now() - self.first_parked.get(key, self.now()) >= MAX_PARK_SECONDS:
                    return False   # back in the same park, and that park's time is up
            if self.recovering:
                return True
            if plan:
                if any(other is not trigger and other.owned()
                       and not other.state.get("account_recovery")
                       for other in self.attempts.values()):
                    return False
                trigger.plan = True
            self.recovering = True
            self.parked_since = self.now()
            self.command = tuple(command)
            for attempt in self.attempts.values():
                if attempt.owned() and not self._expired(attempt):
                    attempt.state["account_recovery"] = True
                    self._note_park(attempt)
                    attempt.pause()
        self.schedule(0.0, self.recover)
        return True

    def _pending(self):
        # Called with the dispatch lock held. A user Stop, deletion, or explicit
        # replacement takes ownership away and therefore cancels recovery.
        self.attempts = {key: value for key, value in self.attempts.items()
                         if value.owned()}
        # Work whose park expired runs and fails visibly; no recovery,
        # whoever started it, parks or holds it again (BACKENDS-05).
        return [value for value in self.attempts.values() if not self._expired(value)]

    def _expired(self, attempt: Attempt) -> bool:
        return (attempt.agent_id, attempt.trace_id) in self.park_expired

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
        with self.lock:
            self.checking += 1
        try:
            self._recover()
        finally:
            with self.lock:
                self.checking -= 1

    def _recover(self):
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
                if (not pending and self.awaiting
                        and self.now() < self.awaiting_until):
                    # A carried recovery whose work is not re-admitted yet
                    # (boot recovery runs after this timer, or before it):
                    # ending it now would run that work unchecked.
                    self.schedule(CARRIED_WORK_POLL_SECONDS, self.recover)
                    return
                self.awaiting.clear()
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
            # Work no saved account serves ends now, with a sentence that says
            # so. Resumed, it would fail again and start the next recovery;
            # parked, it would hold the pool in recovery, which stops every
            # turn again at the next check. After a partial answer that is
            # every model the answer leaves out, at once: the pool's recovery
            # is over. After a plain "no", the turns parked for their plan
            # once they have heard it PLAN_CHECKS times (a usage window is
            # waited out to the park cap).
            served = verdict if isinstance(verdict, frozenset) else None
            if verdict is False:
                for attempt in pending:
                    attempt.unserved_checks += bool(attempt.plan)
            unserved = [attempt for attempt in pending if attempt.fail is not None and (
                (served is not None and attempt.model not in served)
                or (verdict is False and attempt.plan
                    and attempt.unserved_checks >= PLAN_CHECKS))]
            for attempt in unserved:
                self.attempts.pop(attempt.agent_id, None)
            pending = [attempt for attempt in pending if attempt not in unserved]
            if served is not None:
                # What is left is covered, or cannot be ended (no ``fail``)
                # and is resumed to fail by itself, as before.
                verdict = True
            # The cap is per turn, from its first park: a turn that fell back
            # into its park is released when that park is MAX_PARK_SECONDS
            # old, though this recovery began later.
            overdue = []
            if verdict is not True:
                at = self.now()
                overdue = [attempt for attempt in pending if at - self.first_parked.get(
                    (attempt.agent_id, attempt.trace_id), at) >= MAX_PARK_SECONDS]
                for attempt in overdue:
                    self.attempts.pop(attempt.agent_id, None)
                    self.park_expired.add((attempt.agent_id, attempt.trace_id))
                pending = [attempt for attempt in pending if attempt not in overdue]
            decision = self._decide(pending, verdict, settings)
            if isinstance(decision, Resume):
                self.attempts.clear()
                self.recovering = False
                at = self.now()
                for attempt in pending:
                    if attempt.owned():
                        self.resumed_at[(attempt.agent_id, attempt.trace_id)] = at
                        attempt.resume()
            elif isinstance(decision, Release):
                self.recovering = False
                if decision.reason == PARK_EXPIRED:
                    self.park_expired.update(
                        (attempt.agent_id, attempt.trace_id) for attempt in pending)
                    self.attempts.clear()
            for attempt in unserved:
                if attempt.owned():
                    attempt.fail()
        if unserved:
            log("claudeAccountUnserved",
                f"ended={len(unserved)} models={sorted({a.model for a in unserved})}")
        if overdue:
            log("claudeAccountParkExpired",
                f"released={len(overdue)} whose own park is {MAX_PARK_SECONDS:.0f}s old; "
                "the turns run and fail visibly instead")
            for attempt in overdue:
                if attempt.owned():
                    attempt.resume()
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
            return {"recovering": self.recovering, "checking": bool(self.checking),
                    "waiting": sorted(item.agent_id for item in self.attempts.values()
                                      if item.state.get("account_recovery") and item.owned())}

    # --- the release drain's hand-over (turn_dispatch.transfer_recovery_parks)

    def take_parked(self, eligible: Callable[[Attempt], bool]) -> list[Attempt]:
        """Remove and return parked work no process ever ran for, which
        ``eligible`` accepts, so no recovery pass can resume it. Nothing while
        a recovery pass is running. Called with the dispatch lock held."""
        with self.lock:
            if self.checking:
                return []
            taken = [attempt for attempt in self.attempts.values()
                     if attempt.state.get("account_recovery")
                     and attempt.spawned.is_set() and attempt.handle is None
                     and not attempt.state.get("spawn_started")
                     and attempt.owned() and eligible(attempt)]
            for attempt in taken:
                self.attempts.pop(attempt.agent_id, None)
            return taken

    def restore(self, attempts: list[Attempt]) -> list[Attempt]:
        """Give back work ``take_parked`` removed whose hand-over failed. If
        the recovery ended meanwhile it resumes now, like the rest did.
        Returns the attempts parked again (only those still wait)."""
        resume, parked = [], []
        with self.lock:
            for attempt in attempts:
                if not attempt.owned():
                    continue
                if self.recovering:
                    self.attempts.setdefault(attempt.agent_id, attempt)
                    parked.append(attempt)
                else:
                    resume.append(attempt)
            for attempt in resume:
                attempt.resume()
        return parked

    def carry_over(self, wall: float, agents=()) -> dict | None:
        """What the next runtime needs to keep this recovery's bound: when
        the work was first parked (wall-clock ms) and whose parked work was
        handed over. None when not recovering."""
        with self.lock:
            if not self.recovering:
                return None
            return {"parked_since_ms": int(
                (wall - max(0.0, self.now() - self.parked_since)) * 1000),
                "agents": sorted(set(agents))}

    def resume_recovery(self, command, parked_since_ms: int, wall: float,
                        agents=()) -> bool:
        """At boot after a handoff: continue the previous runtime's recovery
        with its original start, so a handoff never extends the park bound.
        Credentials stay with the selector; only the timing carries over."""
        if not command:
            return False
        with self.lock:
            if self.recovering:
                return False
            elapsed = max(0.0, wall - parked_since_ms / 1000.0)
            self.recovering = True
            self.parked_since = self.now() - elapsed
            self.command = tuple(command)
            self.next_check = 0.0
            # Hold the recovery for the carried work, whichever runs first:
            # this pass or the boot's queue recovery that re-admits it.
            self.awaiting = {str(agent) for agent in agents} - set(self.attempts)
            self.awaiting_until = self.now() + CARRIED_WORK_WAIT_SECONDS
        self.schedule(0.0, self.recover)
        return True
