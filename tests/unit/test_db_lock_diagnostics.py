from __future__ import annotations

import sqlite3
import time
from types import SimpleNamespace

import pytest

from lib import db


def test_request_metrics_measure_sql_without_bound_values():
    db.conn()
    db.begin_request_metrics(enabled=True)
    db.conn().execute("SELECT ? AS private_value", ("secret",)).fetchone()
    metrics = db.finish_request_metrics()
    assert metrics["query_count"] == 1
    assert metrics["sqlite_ms"] >= 0
    assert "secret" not in metrics["max_query"]
    assert "SELECT ? AS private_value" in metrics["max_query"]


def test_request_metrics_are_zero_cost_when_disabled():
    db.begin_request_metrics(enabled=False)
    db.conn().execute("SELECT 1").fetchone()
    assert db.finish_request_metrics() == {}


def test_request_metrics_surface_bounded_n_plus_one_templates():
    db.conn()
    db.begin_request_metrics(enabled=True)
    for value in range(9):
        db.conn().execute("SELECT ? AS repeated", (value,)).fetchone()
    metrics = db.finish_request_metrics()
    assert metrics["repeated_queries"] == [
        {"count": 9, "sql": "SELECT ? AS repeated"}
    ]


def _tracked_connection(path, timeout: float = 0.05) -> db._TrackedConnection:
    return sqlite3.connect(
        str(path),
        timeout=timeout,
        isolation_level=None,
        check_same_thread=False,
        factory=db._TrackedConnection,
    )


def test_transaction_owner_is_removed_after_commit_and_close(tmp_path):
    path = tmp_path / "owners.sqlite"
    connection = _tracked_connection(path)
    connection.execute("CREATE TABLE values_table (value TEXT)")

    connection.execute("BEGIN IMMEDIATE")
    assert id(connection) in db._TRANSACTION_OWNERS
    connection.execute("INSERT INTO values_table VALUES (?)", ("secret",))
    assert db._TRANSACTION_OWNERS[id(connection)].last_sql == (
        "INSERT INTO values_table VALUES (?)"
    )

    connection.commit()
    assert id(connection) not in db._TRANSACTION_OWNERS

    connection.execute("BEGIN IMMEDIATE")
    connection.close()
    assert id(connection) not in db._TRANSACTION_OWNERS


def test_lock_report_identifies_owner_without_bound_values(tmp_path, capsys):
    path = tmp_path / "locked.sqlite"
    owner = _tracked_connection(path)
    waiter = _tracked_connection(path)
    owner.execute("CREATE TABLE values_table (value TEXT)")
    owner.execute("BEGIN IMMEDIATE")
    owner.execute("INSERT INTO values_table VALUES (?)", ("owner-secret",))

    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        waiter.execute(
            "INSERT INTO values_table VALUES (?)",
            ("waiter-secret",),
        )

    stderr = capsys.readouterr().err
    assert "sqlite_lock_wait" in stderr
    assert "sqlite_lock_owner" in stderr
    assert "BEGIN IMMEDIATE" in stderr
    assert "INSERT INTO values_table VALUES (?)" in stderr
    assert "test_lock_report_identifies_owner_without_bound_values" in stderr
    assert "owner-secret" not in stderr
    assert "waiter-secret" not in stderr

    owner.rollback()
    owner.close()
    waiter.close()


def test_identical_lock_reports_are_rate_limited(tmp_path, capsys):
    path = tmp_path / "rate-limited.sqlite"
    owner = waiter = None
    try:
        owner = _tracked_connection(path)
        waiter = _tracked_connection(path)
        owner.execute("CREATE TABLE values_table (value TEXT)")
        owner.execute("BEGIN IMMEDIATE")

        for _ in range(2):
            with pytest.raises(sqlite3.OperationalError, match="database is locked"):
                waiter.execute("INSERT INTO values_table VALUES (?)", ("hidden",))

        stderr = capsys.readouterr().err
        lines = stderr.splitlines()
        waits = [line for line in lines if line.startswith("sqlite_lock_wait ")]
        owners = [line for line in lines if line.startswith(
            f"sqlite_lock_owner connection={id(owner)} ")]
        assert len(waits) == 1
        assert "sql='INSERT INTO values_table VALUES (?)' " in waits[0]
        assert len(owners) == 1
        assert "begin_sql='BEGIN IMMEDIATE' " in owners[0]
        assert "last_sql='BEGIN IMMEDIATE' " in owners[0]
        assert "hidden" not in stderr
    finally:
        try:
            if owner is not None:
                owner.close()
        finally:
            if waiter is not None:
                waiter.close()


def test_retry_locked_succeeds_after_a_busy_error():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise sqlite3.OperationalError("database is locked")
        return "ok"

    assert db.retry_locked(flaky, retries=3, sleep_sec=0) == "ok"
    assert calls["n"] == 3


def test_retry_locked_reraises_when_still_busy():
    def always_locked():
        raise sqlite3.OperationalError("database is locked")

    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        db.retry_locked(always_locked, retries=2, sleep_sec=0)


def test_busy_timeout_restores_the_request_path_budget():
    db.conn()
    with db.busy_timeout(30_000):
        assert db.conn().execute("PRAGMA busy_timeout").fetchone()[0] == 30_000
    assert db.conn().execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_lock_report_separates_read_snapshots_from_confirmed_writers(tmp_path, capsys):
    path = tmp_path / "snapshot.sqlite"
    reader, writer, waiter = [_tracked_connection(path) for _ in range(3)]
    reader.execute("PRAGMA journal_mode=WAL")
    reader.execute("CREATE TABLE sample(value TEXT)")
    reader.execute("INSERT INTO sample VALUES ('synthetic')")
    try:
        reader.execute("BEGIN")
        reader.execute("SELECT value FROM sample").fetchall()
        writer.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.OperationalError):
            waiter.execute("INSERT INTO sample VALUES (?)", ("private-value",))
        stderr = capsys.readouterr().err
        owners = [line for line in stderr.splitlines() if line.startswith("sqlite_lock_owner ")]
        snapshots = [line for line in stderr.splitlines() if line.startswith("sqlite_lock_snapshot ")]
        assert len(owners) == len(snapshots) == 1
        assert f"connection={id(writer)} " in owners[0]
        assert f"connection={id(reader)} " in snapshots[0]
        assert "owner_count=1 snapshot_count=1" in stderr
        assert "private-value" not in stderr
    finally:
        reader.close()
        writer.close()
        waiter.close()


def test_deferred_write_confirmation_has_its_own_age(tmp_path, monkeypatch, capsys):
    connection = _tracked_connection(tmp_path / "upgrade.sqlite")
    connection.execute("CREATE TABLE sample(value INTEGER)")
    tick = [10.0]
    monkeypatch.setattr(db, "time", SimpleNamespace(
        monotonic=lambda: tick[0], perf_counter=time.perf_counter, sleep=time.sleep))
    try:
        connection.execute("BEGIN")
        connection.execute("SELECT value FROM sample").fetchall()
        assert db._TRANSACTION_OWNERS[id(connection)].write_started_monotonic is None
        tick[0] = 100.0
        connection.execute("INSERT INTO sample VALUES (1)")
        owner = db._TRANSACTION_OWNERS[id(connection)]
        assert owner.started_monotonic == 10.0
        assert owner.write_started_monotonic == 100.0
        tick[0] = 102.0
        db._report_database_locked("BEGIN IMMEDIATE")
        stderr = capsys.readouterr().err
        assert "age_sec=92.000" in stderr
        assert "write_age_sec=2.000" in stderr
    finally:
        connection.close()


def test_write_admission_is_registered_before_wait_logging_and_not_reset(tmp_path, monkeypatch):
    from lib import log
    connection = _tracked_connection(tmp_path / "admission.sqlite")
    tick = [100.0]
    observations = []
    monkeypatch.setattr(db, "time", SimpleNamespace(
        monotonic=lambda: tick[0], perf_counter=time.perf_counter, sleep=time.sleep))
    monkeypatch.setattr(db, "_LONG_WRITE_WAIT_SECONDS", -1)

    def observe(event, detail):
        owner = db._TRANSACTION_OWNERS.get(id(connection))
        observations.append((event, owner))
        tick[0] = 200.0  # stand-in for a slow logging sink

    monkeypatch.setattr(log, "log", observe)
    try:
        connection.execute("BEGIN IMMEDIATE")
        assert len(observations) == 1
        event, owner = observations[0]
        assert event == "sqliteWriteWait"
        assert owner is not None
        assert owner.write_started_monotonic == 100.0
        assert db._TRANSACTION_OWNERS[id(connection)].write_started_monotonic == 100.0
    finally:
        connection.close()


def test_owner_names_current_sql_while_it_is_executing(tmp_path):
    connection = _tracked_connection(tmp_path / "current.sqlite")
    observed = []
    connection.create_function("observe_current", 1, lambda value: observed.append(
        db._TRANSACTION_OWNERS[id(connection)].last_sql) or 0)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("SELECT observe_current(?)", ("private-value",)).fetchall()
        assert observed == ["SELECT observe_current(?)"]
    finally:
        connection.close()


def test_failed_snapshot_upgrade_is_not_a_confirmed_writer(tmp_path, capsys):
    path = tmp_path / "stale.sqlite"
    reader, other = [_tracked_connection(path) for _ in range(2)]
    reader.execute("PRAGMA journal_mode=WAL")
    reader.execute("CREATE TABLE sample(value INTEGER)")
    reader.execute("INSERT INTO sample VALUES (1)")
    try:
        reader.execute("BEGIN")
        reader.execute("SELECT value FROM sample").fetchall()
        other.execute("INSERT INTO sample VALUES (2)")
        with pytest.raises(sqlite3.OperationalError) as error:
            reader.execute("UPDATE sample SET value=3")
        assert error.value.sqlite_errorcode == sqlite3.SQLITE_BUSY_SNAPSHOT
        assert db._TRANSACTION_OWNERS[id(reader)].write_started_monotonic is None
        stderr = capsys.readouterr().err
        assert "owner_count=0 snapshot_count=1" in stderr
        assert "sqlite_lock_owner " not in stderr
    finally:
        reader.close()
        other.close()


@pytest.mark.parametrize("raises", [False, True])
def test_context_exit_removes_confirmed_owner_on_commit_and_rollback(tmp_path, raises):
    connection = _tracked_connection(tmp_path / "context.sqlite")
    try:
        try:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                if raises:
                    raise ValueError("synthetic rollback")
        except ValueError:
            pass
        assert not connection.in_transaction
        assert id(connection) not in db._TRANSACTION_OWNERS
    finally:
        connection.close()


def test_sqlite_implicit_rollback_removes_confirmed_owner(tmp_path):
    connection = _tracked_connection(tmp_path / "implicit.sqlite")
    connection.execute("CREATE TABLE sample(value INTEGER UNIQUE)")
    connection.execute("INSERT INTO sample VALUES (1)")
    try:
        connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("INSERT OR ROLLBACK INTO sample VALUES (1)")
        assert not connection.in_transaction
        assert id(connection) not in db._TRANSACTION_OWNERS
    finally:
        connection.close()
