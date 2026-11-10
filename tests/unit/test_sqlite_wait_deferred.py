"""An admitted explicit transaction must not write its wait log to any sink."""
import json
import sqlite3
import gc
import threading
import time
from types import SimpleNamespace

import pytest

from lib import db, diagnostics_settings, eventlog, log, telemetry


@pytest.fixture(autouse=True)
def _capture_enabled(_isolated_db):
    diagnostics_settings.update({"enabled": True, "categories": list(diagnostics_settings.CATEGORIES)})


def _lines(path):
    return [json.loads(line) for file in path.glob("*.jsonl") for line in file.read_text().splitlines()]


@pytest.mark.parametrize("ending", ["COMMIT", "ROLLBACK", "commit", "rollback", "with_commit", "with_rollback", "close"])
def test_wait_sinks_run_after_transaction_end_and_other_jsonl_stays_immediate(tmp_path, monkeypatch, capsys, ending):
    connection = db.conn()
    telemetry.conn()
    eventlog.reset_for_tests(tmp_path)
    capsys.readouterr()
    attempts = []
    write_jsonl, insert = eventlog._write_jsonl, eventlog._insert_sqlite
    stderr = log.sys.stderr

    def in_transaction():
        try:
            return connection.in_transaction
        except sqlite3.ProgrammingError:  # the close ending released its lock
            return False

    def jsonl(record, now):
        if record["event"] == "sqliteWriteWait":
            attempts.append(("jsonl", in_transaction()))
        write_jsonl(record, now)

    def sqlite(record, now):
        if record["event"] == "sqliteWriteWait":
            attempts.append(("sqlite", in_transaction()))
        insert(record, now)

    class Stderr:
        def write(self, value):
            if "sqliteWriteWait" in value:
                attempts.append(("stderr", in_transaction()))
            return stderr.write(value)

        def flush(self):
            return stderr.flush()

    monkeypatch.setattr(eventlog, "_write_jsonl", jsonl)
    monkeypatch.setattr(eventlog, "_insert_sqlite", sqlite)
    monkeypatch.setattr(log.sys, "stderr", Stderr())
    monkeypatch.setattr(db, "_LONG_WRITE_WAIT_SECONDS", -1)

    def begin():
        connection.execute("BEGIN IMMEDIATE")
        assert id(connection) in db._TRANSACTION_OWNERS
        assert attempts == []
        eventlog.emit("test", "ordinaryInside")
        assert [r["event"] for r in _lines(tmp_path)] == ["ordinaryInside"]
        assert telemetry.conn().execute("SELECT count(*) FROM diagnostic_events").fetchone()[0] == 0
        eventlog.flush_deferred()
        eventlog._flush_all_deferred()
        assert attempts == []  # backstops cannot bypass the admitted transaction

    try:
        if ending.startswith("with_"):
            try:
                with connection:
                    begin()
                    if ending == "with_rollback":
                        raise ValueError("synthetic rollback")
            except ValueError:
                pass
        else:
            begin()
            if ending in ("COMMIT", "ROLLBACK"):
                connection.execute(ending)
            else:
                getattr(connection, ending)()
        assert sorted(attempts) == [("jsonl", False), ("sqlite", False), ("stderr", False)]
        rows = telemetry.conn().execute("SELECT event,ts_iso FROM diagnostic_events ORDER BY event_id").fetchall()
        assert [r["event"] for r in rows] == ["sqliteWriteWait", "ordinaryInside"]
        lines = _lines(tmp_path)
        assert [r["event"] for r in lines] == ["ordinaryInside", "sqliteWriteWait"]
        assert lines[-1]["ts"] == rows[0]["ts_iso"]
    finally:
        db.close_local()


def test_wait_outside_transaction_is_immediate_and_disabled_capture_still_logs_stderr(tmp_path, capsys):
    eventlog.reset_for_tests(tmp_path)
    log.log("sqliteWriteWait", "synthetic immediate")
    assert "sqliteWriteWait" in capsys.readouterr().err
    assert [r["event"] for r in _lines(tmp_path)] == ["sqliteWriteWait"]
    diagnostics_settings.update({"enabled": False, "categories": []})
    connection = db.conn()
    connection.execute("BEGIN IMMEDIATE")
    # This uses the production wait-specific entry point; no mirror is enabled,
    # but the stderr line must survive until the transaction actually ends.
    log.sqlite_write_wait("synthetic disabled", connection)
    assert capsys.readouterr().err == ""
    connection.rollback()
    assert "synthetic disabled" in capsys.readouterr().err
    assert len(_lines(tmp_path)) == 1


def test_wait_queue_does_not_keep_an_abandoned_writer_connection_alive(tmp_path, monkeypatch):
    path = db.conn().execute("PRAGMA database_list").fetchone()[2]
    telemetry.conn()
    eventlog.reset_for_tests(tmp_path)
    monkeypatch.setattr(db, "_LONG_WRITE_WAIT_SECONDS", -1)
    errors = []

    def abandoned():
        try:
            connection = db.conn()
            connection.execute("BEGIN IMMEDIATE")
            # Deliberately leave the transaction for connection destruction.
            # The logger must not retain it beyond the thread-local lifetime.
        except Exception as error:
            errors.append(type(error).__name__)

    worker = threading.Thread(target=abandoned)
    worker.start()
    worker.join(5)
    assert not worker.is_alive()
    assert errors == []
    gc.collect()
    with sqlite3.connect(path, isolation_level=None, timeout=0.1) as other:
        other.execute("BEGIN IMMEDIATE")
        other.rollback()
    eventlog.flush_deferred()
    assert [r["event"] for r in _lines(tmp_path)] == ["sqliteWriteWait"]


def test_failed_wait_producer_does_not_defer_later_unrelated_logging(tmp_path, monkeypatch, capsys):
    connection = db.conn()
    eventlog.reset_for_tests(tmp_path)
    connection.execute("BEGIN IMMEDIATE")

    def failing_log(event, detail):
        raise RuntimeError("synthetic sink failure")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(log, "log", failing_log)
            with pytest.raises(RuntimeError, match="synthetic sink failure"):
                log.sqlite_write_wait("synthetic failed producer", connection)
        capsys.readouterr()
        log.log("sqliteWriteWait", "synthetic unrelated producer")
        assert "synthetic unrelated producer" in capsys.readouterr().err
        assert [row["event"] for row in _lines(tmp_path)] == ["sqliteWriteWait"]
    finally:
        connection.rollback()


def test_admission_precedes_wait_deferral_and_its_timestamp_is_not_reset(monkeypatch):
    connection = db.conn()
    telemetry.conn()
    tick = [100.0]
    original = eventlog.defer_sqlite_write_wait
    seen = []

    def observe(connection, line, detail):
        seen.append(db._TRANSACTION_OWNERS.get(id(connection)))
        result = original(connection, line, detail)
        tick[0] = 200.0
        return result

    monkeypatch.setattr(db, "time", SimpleNamespace(
        monotonic=lambda: tick[0], perf_counter=time.perf_counter, sleep=time.sleep))
    monkeypatch.setattr(db, "_LONG_WRITE_WAIT_SECONDS", -1)
    monkeypatch.setattr(eventlog, "defer_sqlite_write_wait", observe)
    try:
        connection.execute("BEGIN IMMEDIATE")
        assert len(seen) == 1 and seen[0] is not None
        assert seen[0].started_monotonic == 100.0
        assert db._TRANSACTION_OWNERS[id(connection)].started_monotonic == 100.0
    finally:
        connection.rollback()
