"""An admitted explicit transaction must not write its wait log to any sink."""
import json
import sqlite3

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
