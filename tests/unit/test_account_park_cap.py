"""BACKENDS-05: after the 30-minute park cap, parked turns are released so
they 'run and fail visibly'. The resumed turn re-registered as fresh work, so
its next usage-limit failure started a new 30-minute park, and the user never
saw 'Usage limit reached' until the accounts reset. Work whose park expired
is not parked again; new work still is."""
from __future__ import annotations

import threading

from lib.account_failover import MAX_PARK_SECONDS, RECHECK_SECONDS, AccountFailover, Attempt


def _coordinator():
    clock, scheduled = [100.0], []
    coordinator = AccountFailover(threading.RLock(), switch=lambda *_a: False,
                                  schedule=lambda _delay, callback: scheduled.append(callback),
                                  now=lambda: clock[0])
    return coordinator, clock, scheduled


def _attempt(trace, resumed):
    item = Attempt("synthetic", trace, "sonnet", {}, lambda: True, lambda: None,
                   lambda: resumed.append(trace), handle=None)
    item.spawned.set()
    return item


def _run_park_to_expiry(coordinator, clock, scheduled):
    for _ in range(int(MAX_PARK_SECONDS / RECHECK_SECONDS) + 3):
        if not scheduled:
            break
        scheduled.pop(0)()
        clock[0] += RECHECK_SECONDS


def test_work_whose_park_expired_is_not_parked_again():
    coordinator, clock, scheduled = _coordinator()
    resumed = []
    coordinator.register(_attempt("admitted-trace", resumed))
    assert coordinator.request("synthetic", "admitted-trace", ("selector",))
    _run_park_to_expiry(coordinator, clock, scheduled)
    assert resumed == ["admitted-trace"] and not coordinator.recovering
    # The dispatcher re-admits the same spec with a fresh Attempt; it fails on
    # the usage limit again.
    coordinator.register(_attempt("admitted-trace", resumed))
    assert coordinator.request("synthetic", "admitted-trace", ("selector",)) is False


def test_new_work_after_an_expired_park_still_parks():
    coordinator, clock, scheduled = _coordinator()
    resumed = []
    coordinator.register(_attempt("admitted-trace", resumed))
    coordinator.request("synthetic", "admitted-trace", ("selector",))
    _run_park_to_expiry(coordinator, clock, scheduled)
    coordinator.register(_attempt("next-trace", resumed))
    assert coordinator.request("synthetic", "next-trace", ("selector",)) is True
