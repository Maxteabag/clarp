"""A pinned WAL reader must not make maintenance starve another writer."""
import sqlite3
import threading
import time

import pytest

from lib import db, maintenance, telemetry


@pytest.fixture
def checkpoint_only(tmp_path, monkeypatch):
    # Retention and telemetry have independent tests; exercise the real state
    # checkpoint through the maintenance entry point without their other I/O.
    monkeypatch.setattr(maintenance, "prune_hls_artifacts", lambda *a, **kw: 0)
    monkeypatch.setattr(maintenance, "prune_database", lambda **kw: {})
    monkeypatch.setattr(maintenance, "prune_event_logs", lambda *a, **kw: 0)
    monkeypatch.setattr(maintenance, "archive_done_helpers", lambda **kw: 0)
    monkeypatch.setattr(telemetry, "rollup_and_prune", lambda **kw: {})
    reports = []
    monkeypatch.setattr(maintenance, "log", lambda event, detail: reports.append((event, detail)))
    return maintenance.MaintenanceWorker(audio_dir=tmp_path), reports


@pytest.mark.parametrize("timeout_ms", [5000, 25])
def test_checkpoint_allows_writer_while_reader_stays_open_and_later_truncates(checkpoint_only, timeout_ms):
    worker, reports = checkpoint_only
    connection = db.conn()
    connection.execute(f"PRAGMA busy_timeout={timeout_ms}")
    connection.execute("CREATE TABLE checkpoint_admission_probe(value INTEGER)")
    connection.execute("INSERT INTO checkpoint_admission_probe VALUES (1)")
    path = connection.execute("PRAGMA database_list").fetchone()[2]
    reader = sqlite3.connect(path, isolation_level=None)
    reader.execute("BEGIN")
    assert reader.execute("SELECT value FROM checkpoint_admission_probe").fetchall() == [(1,)]
    connection.execute("INSERT INTO checkpoint_admission_probe VALUES (2)")
    checkpoint_started = threading.Event()
    outcome = []

    def trace(sql):
        if sql.upper() == "PRAGMA WAL_CHECKPOINT(TRUNCATE)":
            checkpoint_started.set()

    def write_during_checkpoint():
        other = sqlite3.connect(path, isolation_level=None, timeout=0.5)
        try:
            if not checkpoint_started.wait(5):
                outcome.append("checkpoint_not_started")
                return
            # The trace fires before SQLite acquires the checkpoint lock.
            # Let it enter its busy wait; keep the reader pinned throughout.
            time.sleep(0.03)
            other.execute("INSERT INTO checkpoint_admission_probe VALUES (3)")
            outcome.append("admitted")
        except sqlite3.OperationalError as exc:
            outcome.append(exc.sqlite_errorname)
        finally:
            other.close()

    writer = threading.Thread(target=write_during_checkpoint)
    connection.set_trace_callback(trace)
    writer.start()
    try:
        worker.run_once()
        writer.join(5)
        assert not writer.is_alive()
        assert outcome == ["admitted"]
        assert reader.in_transaction  # admission did not depend on releasing the reader
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == timeout_ms
        results = [dict(item.split("=", 1) for item in detail.split())
                   for event, detail in reports if event == "maintenanceCheckpoint"]
        assert [(r["mode"], r["busy"]) for r in results] == [("TRUNCATE", "1"), ("PASSIVE", "0")]
        assert all(int(r["log_frames"]) >= int(r["checkpointed_frames"]) >= 0 for r in results)
        reader.execute("ROLLBACK")
        reports.clear()
        worker.run_once()
        results = [dict(item.split("=", 1) for item in detail.split())
                   for event, detail in reports if event == "maintenanceCheckpoint"]
        assert [(r["mode"], r["busy"]) for r in results] == [("TRUNCATE", "0")]
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == timeout_ms
        assert [tuple(row) for row in connection.execute(
            "SELECT value FROM checkpoint_admission_probe ORDER BY value").fetchall()] == [
            (1,), (2,), (3,)]
    finally:
        connection.set_trace_callback(None)
        if reader.in_transaction:
            reader.execute("ROLLBACK")
        reader.close()
        writer.join(5)
