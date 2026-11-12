"""A recovery pass that cannot get the database's writer is run again.

elitebook's runtime, 2026-11-12 06:40:30 and 07:06:07, each a few seconds
after a Host-only install restarted the Host: the pass that re-admits queued
work began with the receipts' reconciliation, which waits 50 ms for the
writer; the Host held it longer. ``sqlite3.OperationalError: database is
locked`` went up through ``recover_queued`` and ended its timer thread. The
pass schedules its own next run at its end, so that was the last run: the
journal shows it once a second up to the error and then not at all (the next
one at 07:27, twenty-one minutes later, when something else started it).
Nothing was lost, the receipts stayed as they were; they just waited.
"""
from __future__ import annotations

import sqlite3
import uuid
from types import SimpleNamespace

import pytest

from lib import turn_queue
import lib.turn_dispatch as _td
from lib.turn_dispatch import TurnDispatchService
from tests.unit.test_turn_dispatch import _Backends, _Stream


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setattr(_td, "_RUNTIME_CLIENT", None)
    _td.reset_for_tests()
    scheduled = []
    ctx = SimpleNamespace(default_session="agent", agents_path=tmp_path / "unused.json",
                          stream=_Stream())
    made = TurnDispatchService(
        ctx, backend_registry=_Backends(), home=tmp_path, uuid_factory=lambda: str(uuid.uuid4()),
        retry_scheduler=lambda delay, callback: scheduled.append((delay, callback)))
    made.scheduled = scheduled
    yield made
    _td.reset_for_tests()


def test_a_pass_that_meets_a_locked_database_runs_again_in_a_second(service, monkeypatch, capsys):
    real, calls = turn_queue.reconcile_receipts, []

    def locked_once():
        calls.append(1)
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")
        return real()

    monkeypatch.setattr(turn_queue, "reconcile_receipts", locked_once)

    assert service.recover_queued() == 0                  # no exception out of the pass

    assert [delay for delay, _ in service.scheduled] == [1.0]
    assert "queuedRecoveryLocked" in capsys.readouterr().err
    assert _td._RECOVERY_LOCK.acquire(blocking=False)     # and the pass's own lock is free
    _td._RECOVERY_LOCK.release()

    service.scheduled.pop()[1]()                          # the retry is the pass itself
    assert len(calls) == 2


def test_any_other_database_error_still_comes_out(service, monkeypatch):
    def broken():
        raise sqlite3.OperationalError("no such table: queued_turns")

    monkeypatch.setattr(turn_queue, "reconcile_receipts", broken)

    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        service.recover_queued()
    assert service.scheduled == []
