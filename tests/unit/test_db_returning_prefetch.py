"""Autocommit RETURNING must release its writer before wait-log sink I/O."""
import sqlite3
from types import SimpleNamespace

import pytest

from lib import db, log


def test_waited_returning_releases_writer_before_wait_sinks(tmp_path, monkeypatch):
    path = tmp_path / "returning.sqlite"
    writer = sqlite3.connect(path, isolation_level=None, timeout=0.1,
                             factory=db._TrackedConnection)
    blocker = sqlite3.connect(path, isolation_level=None, timeout=0)
    contender = sqlite3.connect(path, isolation_level=None, timeout=0)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE sample(value INTEGER)")
        blocker.execute("BEGIN IMMEDIATE")
        polls = []
        def release(_seconds):
            polls.append(True)
            blocker.rollback()
        ticks = iter([100.0, 100.01, 100.5])
        monkeypatch.setattr(db, "time", SimpleNamespace(monotonic=lambda: next(ticks), sleep=release))
        sink_admissions = []
        def sink(_detail, connection):
            assert not connection.in_transaction
            contender.execute("BEGIN IMMEDIATE")
            sink_admissions.append(True)
            contender.rollback()
        monkeypatch.setattr(log, "sqlite_write_wait", sink)
        cursor = writer.execute("INSERT INTO sample VALUES(7),(8) RETURNING value")
        assert polls == [True]  # the first real admission attempt was BUSY
        assert sink_admissions == [True]
        assert cursor.fetchone() == (7,)
        assert cursor.fetchall() == [(8,)]
        assert cursor.fetchone() is None
    finally:
        writer.close()
        blocker.close()
        contender.close()


@pytest.mark.parametrize("explicit", [False, True])
def test_returning_consumption_and_transaction_boundary(tmp_path, explicit):
    connection = sqlite3.connect(tmp_path / "rows.sqlite", isolation_level=None,
                                 factory=db._TrackedConnection)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("CREATE TABLE sample(id INTEGER PRIMARY KEY, value TEXT)")
        if explicit:
            connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute("INSERT INTO sample(value) VALUES('a'),('b') RETURNING id,value")
        assert connection.in_transaction is explicit
        assert [tuple(row) for row in cursor.fetchall()] == [(1, "a"), (2, "b")]
        assert cursor.fetchone() is None
        assert [entry[0] for entry in cursor.description] == ["id", "value"]
        assert cursor.rowcount == 2
        assert cursor.lastrowid == 2
        cursor.close()
        connection.rollback()
    finally:
        connection.close()
