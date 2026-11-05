"""RM-N8b Part 2: an emit inside a state.sqlite transaction never waits on
telemetry.sqlite; its row is written when the transaction ends.

Before, every emit inserted into telemetry.sqlite with a 5 s busy timeout. An
emit inside a state transaction (db.py's own sqliteWriteWait line after an
admitted BEGIN IMMEDIATE among them) held the state write lock for as long as
telemetry was busy. Now the row is queued and written at the end of the
transaction, by whatever path it ends, and backstops make sure no queue is
left behind.
"""
from __future__ import annotations

import sqlite3
import threading
import time

import pytest

from lib import db, diagnostics_settings, eventlog, telemetry


@pytest.fixture(autouse=True)
def _persist_enabled_diagnostics(_isolated_db):
    # A cache-only seed expires after 2 s or a cache clear. These tests exercise
    # deferred delivery, so capture must stay enabled for the whole test.
    diagnostics_settings.update({"enabled": True, "categories": list(diagnostics_settings.CATEGORIES)})


def _rows(event: str) -> int:
    return telemetry.conn().execute(
        "SELECT count(*) FROM diagnostic_events WHERE event = ?", (event,)).fetchone()[0]


def _emit(event: str) -> None:
    eventlog.emit("test", event)


def test_an_emit_inside_a_state_transaction_does_not_wait_on_a_locked_telemetry():
    path = telemetry.conn().execute("PRAGMA database_list").fetchone()[2]
    locker = sqlite3.connect(path, isolation_level=None)
    c = db.conn()
    c.execute("BEGIN IMMEDIATE")
    locker.execute("BEGIN IMMEDIATE")          # telemetry.sqlite is busy
    try:
        started = time.monotonic()
        _emit("insideBusy")
        assert time.monotonic() - started < 0.5   # not its 5 s busy wait
    finally:
        locker.execute("COMMIT")
        locker.close()
    assert _rows("insideBusy") == 0
    c.execute("COMMIT")
    assert _rows("insideBusy") == 1


ENDINGS = {
    "COMMIT": lambda c: c.execute("COMMIT"),
    "ROLLBACK": lambda c: c.execute("ROLLBACK"),
    "END": lambda c: c.execute("END"),
    "commit()": lambda c: c.commit(),
    "rollback()": lambda c: c.rollback(),
    "executescript": lambda c: c.executescript("SELECT 1;"),   # it commits an open transaction first
}


@pytest.mark.parametrize("ending", sorted(ENDINGS))
def test_every_tracked_way_a_transaction_ends_writes_the_queued_rows(ending):
    c = db.conn()
    c.execute("BEGIN IMMEDIATE")
    _emit(f"end-{ending}")
    assert _rows(f"end-{ending}") == 0
    ENDINGS[ending](c)
    assert not c.in_transaction
    assert _rows(f"end-{ending}") == 1


@pytest.mark.parametrize("rolled_back", [False, True])
def test_releasing_an_outermost_savepoint_writes_the_queued_rows_and_a_nested_one_waits(rolled_back):
    """Reviewer A2's probe: sse_store, attention_index, prompt_history and
    agents.set_manual_custom_status open an outermost SAVEPOINT, which a
    RELEASE ends."""
    c = db.conn()
    event = f"savepoint-{rolled_back}"
    c.execute("SAVEPOINT outer_sp")
    _emit(event)
    c.execute("SAVEPOINT inner_sp")
    c.execute("RELEASE inner_sp")                  # nested: the transaction goes on
    assert c.in_transaction and _rows(event) == 0
    if rolled_back:
        c.execute("ROLLBACK TO outer_sp")          # still inside the transaction
        assert c.in_transaction and _rows(event) == 0
    c.execute("RELEASE outer_sp")
    assert not c.in_transaction
    assert _rows(event) == 1


@pytest.mark.parametrize("raises", [False, True])
def test_a_with_block_writes_the_queued_rows_when_it_commits_or_rolls_back(raises):
    c = db.conn()
    try:
        with c:
            c.execute("BEGIN IMMEDIATE")
            _emit(f"with-{raises}")
            assert _rows(f"with-{raises}") == 0
            if raises:
                raise RuntimeError("roll back")
    except RuntimeError:
        pass
    assert not c.in_transaction
    assert _rows(f"with-{raises}") == 1


def test_an_untracked_end_is_flushed_by_the_next_emit_in_order():
    c = db.conn()
    c.execute("BEGIN IMMEDIATE")
    _emit("untracked-1")
    sqlite3.Connection.execute(c, "COMMIT")     # past the tracked path, like an implicit rollback
    assert _rows("untracked-1") == 0
    _emit("untracked-2")
    order = [r[0] for r in telemetry.conn().execute(
        "SELECT event FROM diagnostic_events WHERE event LIKE 'untracked-%' ORDER BY event_id")]
    assert order == ["untracked-1", "untracked-2"]


def test_a_thread_that_ends_with_a_queue_is_flushed_by_another_thread():
    def work():
        c = db.conn()
        c.execute("BEGIN IMMEDIATE")
        _emit("orphan")
        sqlite3.Connection.execute(c, "COMMIT")  # untracked, then the thread ends

    worker = threading.Thread(target=work)
    worker.start()
    worker.join()
    assert _rows("orphan") == 0
    eventlog.flush_deferred()
    assert _rows("orphan") == 1


def test_process_exit_writes_every_queue():
    c = db.conn()
    c.execute("BEGIN IMMEDIATE")
    _emit("atexit")
    try:
        eventlog._flush_all_deferred()
        assert _rows("atexit") == 1
    finally:
        c.execute("ROLLBACK")
    assert _rows("atexit") == 1
