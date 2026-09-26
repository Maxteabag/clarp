"""What to do with work parked behind an account limit.

``account_failover.AccountFailover.recover`` owns the lock, the ownership checks
and every subprocess action. It snapshots the pending attempts, asks this plan
what to do, and acts. The plan never reads a clock: ``now`` comes in.

One recovery pass asks twice. First with ``verdict=NOT_CHECKED``: is owned
work still running (``Kill``), is the next check not due yet (``Park``), or
should the selector run now (``Check``)? Then with the selector's verdict:
``Resume`` the parked turns, ``Park`` them for another check, or ``Release``
them so they run and fail visibly instead of waiting for ever.

``Release`` also answers when nothing is owned any more (a user Stop, a
deletion or a replacement took the work away): recovery simply ends.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


class _Sentinel:
    __slots__ = ("name",)

    def __init__(self, name: str):
        self.name = name

    def __repr__(self) -> str:
        return self.name


# The selector has not run in this pass.
NOT_CHECKED = _Sentinel("NOT_CHECKED")
# The pass raised before a verdict was reached; quota is unknown.
FAILED = _Sentinel("FAILED")


@dataclass(frozen=True)
class PendingWork:
    """One owned attempt as the coordinator sees it under its lock."""
    model: str
    stopped: bool = False


@dataclass(frozen=True)
class FailoverSettings:
    recheck_seconds: float
    max_park_seconds: float


@dataclass(frozen=True)
class Kill:
    reason: str


@dataclass(frozen=True)
class Check:
    models: tuple[str, ...]


@dataclass(frozen=True)
class Resume:
    reason: str


@dataclass(frozen=True)
class Park:
    reason: str
    delay: float


@dataclass(frozen=True)
class Release:
    reason: str


Decision = Kill | Check | Resume | Park | Release

NO_OWNED_WORK = "no-owned-work"
PARK_EXPIRED = "park-expired"


@dataclass(frozen=True)
class FailoverPlan:
    """The durable facts of one recovery: when it started, when it may check
    again, and which models the last verdict covered."""
    parked_since: float
    next_check: float = 0.0
    checked_models: tuple[str, ...] = ()

    def decide(self, pending: Sequence[PendingWork], verdict, now: float,
               settings: FailoverSettings) -> Decision:
        if not pending:
            return Release(NO_OWNED_WORK)
        if verdict is NOT_CHECKED:
            if any(not item.stopped for item in pending):
                return Kill("owned-work-still-running")
            delay = self.next_check - now
            if delay > 0:
                return Park("recheck-not-due", delay)
            return Check(tuple(sorted({item.model for item in pending})))
        if verdict is True:
            # New arrivals can introduce a model that was not checked. Keep
            # them parked for another complete account check.
            if all(item.model in self.checked_models for item in pending):
                return Resume("account-verified")
            reason = "unchecked-model"
        elif verdict is False:
            reason = "no-account"
        elif verdict is None:
            reason = "inconclusive"
        else:
            reason = "recovery-failed"
        if now - self.parked_since >= settings.max_park_seconds:
            return Release(PARK_EXPIRED)
        return Park(reason, settings.recheck_seconds)
