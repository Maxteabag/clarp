"""Lost telemetry rows remain measurable in the independent JSONL mirror."""
import json
import os
import sqlite3
from types import SimpleNamespace

import pytest

from lib import db, eventlog, telemetry


def _lines(path):
    return [json.loads(line) for file in path.glob("*.jsonl")
            for line in file.read_text().splitlines()]


@pytest.mark.parametrize("deferred", [False, True])
def test_dropped_rows_are_counted_without_recursive_sqlite_writes(tmp_path, monkeypatch, deferred):
    ticks = [0.0]
    monkeypatch.setattr(eventlog, "time", SimpleNamespace(monotonic=lambda: ticks[0]), raising=False)
    eventlog.reset_for_tests(tmp_path)
    conn = telemetry.conn()
    conn.execute("PRAGMA busy_timeout=0")
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    locker = sqlite3.connect(path, isolation_level=None)
    locker.execute("BEGIN IMMEDIATE")
    try:
        for tick in (0, 10, 59):
            ticks[0] = tick
            if deferred:
                db.conn().execute("BEGIN IMMEDIATE")
            eventlog.emit("test", "dropProbe")
            if deferred:
                db.conn().execute("COMMIT")
        reports = [r for r in _lines(tmp_path) if r["event"] == "eventlogDropped"]
        assert len(reports) == 1
        assert reports[0]["detail"] == {"pid": os.getpid(), "dropped": 1, "total_dropped": 1}
    finally:
        locker.execute("ROLLBACK")
        locker.close()
    ticks[0] = 60
    eventlog.emit("test", "recovered")
    ticks[0] = 120
    eventlog.emit("test", "stillHealthy")
    lines = _lines(tmp_path)
    reports = [r for r in lines if r["event"] == "eventlogDropped"]
    assert [r["detail"]["dropped"] for r in reports] == [1, 2]
    assert [r["detail"]["total_dropped"] for r in reports] == [1, 3]
    assert all(r["level"] == "warn" for r in reports)
    assert len([r for r in lines if r["event"] == "dropProbe"]) == 3
    assert [r[0] for r in conn.execute("SELECT event FROM diagnostic_events ORDER BY event_id")] == [
        "recovered", "stillHealthy"]


def test_failed_jsonl_drop_report_is_retried_and_exit_reports_the_remainder(tmp_path, monkeypatch):
    ticks = [0.0]
    monkeypatch.setattr(eventlog, "time", SimpleNamespace(monotonic=lambda: ticks[0]), raising=False)
    eventlog.reset_for_tests(tmp_path)
    write_jsonl = eventlog._write_jsonl
    fail_report = [True]

    def write(record, now):
        if record["event"] == "eventlogDropped" and fail_report[0]:
            raise OSError("synthetic unavailable mirror")
        write_jsonl(record, now)

    monkeypatch.setattr(eventlog, "_write_jsonl", write)
    conn = telemetry.conn()
    conn.execute("PRAGMA busy_timeout=0")
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    locker = sqlite3.connect(path, isolation_level=None)
    locker.execute("BEGIN IMMEDIATE")
    try:
        eventlog.emit("test", "firstDrop")
        fail_report[0] = False
        ticks[0] = 1
        eventlog.emit("test", "secondDrop")
        ticks[0] = 2
        eventlog.emit("test", "lastDrop")
    finally:
        locker.execute("ROLLBACK")
        locker.close()
    eventlog._flush_all_deferred()  # the registered orderly-exit callback
    eventlog._flush_all_deferred()  # no duplicate or zero-count report
    reports = [r for r in _lines(tmp_path) if r["event"] == "eventlogDropped"]
    assert [r["detail"]["dropped"] for r in reports] == [2, 1]
    assert [r["detail"]["total_dropped"] for r in reports] == [2, 3]
    assert conn.execute("SELECT count(*) FROM diagnostic_events").fetchone()[0] == 0
