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
#   15:01 and worked. But the recovery is not started again: the park's
#   clock keeps counting, so the MAX_PARK_SECONDS cap counts from the turn's
#   first park and stays the one place that gives up; the selector is asked
#   at the park's next recheck, not at once; and the turn is not told a
#   second time that it was recovered (``state["same_park"]``).
# The shortest real gap between a resume and the next limit that afternoon
# was 145 s of work, which is a new park.
RESUME_GRACE_SECONDS = 120.0
# A turn parked because the provider said its account does not offer its model
# waits for this many selector answers that do not serve that model and then
# ends, with the provider's sentence. A plan is not a usage window: nothing
# resets, so it does not get the half hour. No other turn ends early: a model
# missing from an answer is not served by the chosen account now (its weekly
# quota is spent, its probe failed), which says nothing about the plan.
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
      frozenset  the account now active serves only these of the requested
                 models (the selector's ``models`` list). It does not serve
                 the rest now: that model's quota is spent there, its plan
                 lacks it, or its probe failed. That says nothing about the
                 other saved accounts. May be empty.
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
        return True if covered >= frozenset(models) else covered
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
    # Ends the turn visibly with the provider's sentence from its own plan
    # failure. Called only for a turn parked for that failure (``plan``).
    fail: Callable[[], None] | None = None
    # Tells the turn's owner that it waits for an account that serves its
    # model while the rest of the pool works on.
    hold: Callable[[], None] | None = None
    # Parked because the provider said the account does not offer its model
    # (a plan failure), and how many selector answers since did not serve
    # that model. It ends after PLAN_CHECKS of those.
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
        # The latest timer armed for work that stays parked while the pool
        # works (see ``_begin``); an older one does nothing.
        self.hold_timer = 0

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
            # Behind a recovery, or behind the work already parked for this
            # model: run now, it would meet the same limit and stop the pool.
            unserved = attempt.model in self._unserved_models()
            if (self.recovering or unserved) and not self._park_over(attempt):
                attempt.state["account_recovery"] = True
                self._note_park(attempt)
                (attempt.pause if self.recovering or attempt.hold is None else attempt.hold)()
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

    def _parked(self) -> list[Attempt]:
        """Owned work that waits for an account. Called with the lock held."""
        return [attempt for attempt in self.attempts.values()
                if attempt.state.get("account_recovery") and attempt.owned()
                and not self._expired(attempt)]

    def _unserved_models(self) -> set[str]:
        """While the pool works: the models the active account does not serve
        now and work is parked for. Called with the lock held."""
        return set() if self.recovering else {attempt.model for attempt in self._parked()}

    def _same_park(self, key: tuple[str, str]) -> bool:
        """A recovery resumed this turn so recently that it has not left its
        park. Called with the lock held."""
        resumed = self.resumed_at.get(key)
        return resumed is not None and self.now() - resumed < RESUME_GRACE_SECONDS

    def _park_over(self, attempt: Attempt) -> bool:
        """Whether this turn may not be parked (again): its park expired, or
        it would go back into a park that is MAX_PARK_SECONDS old, which
        expires it here. It runs on and fails visibly, whoever's recovery
        this is. Called with the lock held, before the turn is parked."""
        key = (attempt.agent_id, attempt.trace_id)
        if (key not in self.park_expired and self._same_park(key)
                and self.now() - self.first_parked.get(key, self.now()) >= MAX_PARK_SECONDS):
            self.park_expired.add(key)
        return key in self.park_expired

    def _note_park(self, attempt: Attempt) -> None:
        """Start the turn's park clock, unless this is its park going on; then
        say so to whoever resumes it."""
        key = (attempt.agent_id, attempt.trace_id)
        if key in self.first_parked and self._same_park(key):
            attempt.state["same_park"] = True
        else:
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
            if self._park_over(trigger):
                return False   # its park's time is up: fail visibly now
            if self._same_park(key):
                if plan:
                    return False   # resumed onto an account said to offer it; it does not
                # The park goes on, and so does its pace: the selector's last
                # answer was wrong, which is no reason to ask it again at once.
                self.next_check = max(self.next_check, self.now() + RECHECK_SECONDS)
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
                if attempt.state.get("account_recovery"):
                    continue   # parked already; its clock runs from that park
                if attempt.owned() and not self._park_over(attempt):
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

    def _take_overdue(self, attempts: list[Attempt]) -> list[Attempt]:
        """Take out the work whose own park is MAX_PARK_SECONDS old; it is
        never parked again. Called with the lock held; ``_release`` follows."""
        at = self.now()
        overdue = [attempt for attempt in attempts if at - self.first_parked.get(
            (attempt.agent_id, attempt.trace_id), at) >= MAX_PARK_SECONDS]
        for attempt in overdue:
            self.attempts.pop(attempt.agent_id, None)
            self.park_expired.add((attempt.agent_id, attempt.trace_id))
        return overdue

    @staticmethod
    def _release(overdue: list[Attempt]) -> None:
        if not overdue:
            return
        log("claudeAccountParkExpired",
            f"released={len(overdue)} whose own park is {MAX_PARK_SECONDS:.0f}s old; "
            "the turns run and fail visibly instead")
        for attempt in overdue:
            if attempt.owned():
                attempt.resume()

    def _arm_hold(self) -> None:
        """Look again in a recheck at the work that stays parked while the
        pool works. Called with the lock held."""
        self.hold_timer += 1
        timer = self.hold_timer
        self.schedule(RECHECK_SECONDS, lambda: self.recover_parked(timer))

    def _begin(self, hold=None) -> bool:
        """Whether this pass is a recovery pass. ``hold`` is the timer of a
        pass armed for parked work; a recovery's own passes have none.

        Outside a recovery, work can stay parked for a model the active
        account does not serve now while the turns it does serve work. For
        that work nothing is done to the others: no turn is stopped, and the
        selector, which switches the machine's account while it probes, is
        not run. Each such turn is released at its own cap. Only when no turn
        of the pool is running does a recovery begin for what is parked."""
        with self.lock:
            if self.recovering:
                return hold is None    # the recovery has its own passes
            if hold is not None and hold != self.hold_timer:
                return False
            parked = self._parked()
            overdue = self._take_overdue(parked)
            parked = [attempt for attempt in parked if attempt not in overdue]
            running = any(attempt.owned() and not attempt.state.get("account_recovery")
                          for attempt in self.attempts.values())
            begin = bool(parked) and not running
            if begin:
                self.recovering = True
                self.parked_since = self.now()
            elif parked:
                self._arm_hold()
        self._release(overdue)
        return begin

    def recover(self):
        with self.lock:
            self.checking += 1
        try:
            self._recover()
        finally:
            with self.lock:
                self.checking -= 1

    def recover_parked(self, timer: int) -> None:
        """The timer armed for work that stays parked while the pool works."""
        with self.lock:
            self.checking += 1
        try:
            if self._begin(timer):
                self._recover()
        finally:
            with self.lock:
                self.checking -= 1

    def _recover(self):
        if not self._begin():
            return
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
            # Turns are restarted only by an answer, and the selector is not
            # asked again until a recheck after this one. So no answers,
            # however wrong, restart turns more often than the recheck.
            self.next_check = self.now() + settings.recheck_seconds
            pending = self._pending()
            served = verdict if isinstance(verdict, frozenset) else None
            # A turn parked for its plan has heard one more answer that does
            # not serve its model. After PLAN_CHECKS of them it ends, with the
            # provider's sentence. Nothing else ends here: a usage window is
            # waited out to the park cap.
            ended = []
            if verdict is False or served is not None:
                for attempt in pending:
                    if attempt.plan and (served is None or attempt.model not in served):
                        attempt.unserved_checks += 1
                        if attempt.fail is not None and attempt.unserved_checks >= PLAN_CHECKS:
                            ended.append(attempt)
            for attempt in ended:
                self.attempts.pop(attempt.agent_id, None)
            pending = [attempt for attempt in pending if attempt not in ended]
            # An answer for some of the models: the turns it covers are
            # resumed, the others stay parked in their own park. The active
            # account does not serve them now; nothing is known beyond that.
            waiting = []
            if served is not None:
                covered = [attempt for attempt in pending if attempt.model in served]
                if covered:
                    waiting = [attempt for attempt in pending if attempt.model not in served]
                    pending, verdict = covered, True
                else:
                    verdict = False     # it serves none of them now: keep asking
            # The cap is per turn, from its first park: a turn that fell back
            # into its park, or that waits while others work, is released when
            # that park is MAX_PARK_SECONDS old, though this recovery began later.
            overdue = self._take_overdue(waiting + (pending if verdict is not True else []))
            pending = [attempt for attempt in pending if attempt not in overdue]
            waiting = [attempt for attempt in waiting if attempt not in overdue]
            decision = self._decide(pending, verdict, settings)
            if isinstance(decision, Resume):
                self.recovering = False
                at = self.now()
                # All of them out first: a resumed turn registers its new
                # process, which must not find the others still parked.
                for attempt in pending:
                    self.attempts.pop(attempt.agent_id, None)
                for attempt in pending:
                    if attempt.owned():
                        self.resumed_at[(attempt.agent_id, attempt.trace_id)] = at
                        attempt.resume()
                if waiting:
                    for attempt in waiting:
                        if attempt.hold is not None and attempt.owned():
                            attempt.hold()
                    self._arm_hold()
            elif isinstance(decision, Release):
                self.recovering = False
                if decision.reason == PARK_EXPIRED:
                    self.park_expired.update(
                        (attempt.agent_id, attempt.trace_id) for attempt in pending)
                    self.attempts.clear()
            for attempt in ended:
                if attempt.owned():
                    attempt.fail()
        if ended:
            log("claudeAccountPlanEnded",
                f"ended={len(ended)} after {PLAN_CHECKS} answers without "
                f"{sorted({attempt.model for attempt in ended})}")
        self._release(overdue)
        if isinstance(decision, Resume):
            log("claudeAccountRecovered", f"turns={len(pending)}" + (
                f" parked={len(waiting)} for {sorted({attempt.model for attempt in waiting})}, "
                "which the active account does not serve now" if waiting else ""))
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
                if self.recovering or attempt.model in self._unserved_models():
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
