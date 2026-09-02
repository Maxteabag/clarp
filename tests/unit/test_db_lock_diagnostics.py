from __future__ import annotations

import sqlite3

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
    owner = _tracked_connection(path)
    waiter = _tracked_connection(path)
    owner.execute("CREATE TABLE values_table (value TEXT)")
    owner.execute("BEGIN IMMEDIATE")

    for _ in range(2):
        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            waiter.execute("INSERT INTO values_table VALUES (?)", ("hidden",))

    stderr = capsys.readouterr().err
    assert stderr.count("sqlite_lock_wait") == 1
    assert stderr.count("sqlite_lock_owner") == 1

    owner.rollback()
    owner.close()
    waiter.close()
