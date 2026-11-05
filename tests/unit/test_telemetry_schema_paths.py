"""TEL-N1: the telemetry schema is made per database file. reset_for_tests switches
the file between tests; a thread that opened the previous file before the switch
(a worker left over from an earlier test) must not mark the new file ready."""
from __future__ import annotations

import sqlite3
import threading

import pytest

from lib import telemetry


def test_a_thread_holding_the_old_file_cannot_leave_the_new_one_without_tables(tmp_path, monkeypatch):
    old, new = tmp_path / "old.sqlite", tmp_path / "new.sqlite"
    telemetry.reset_for_tests(old)
    opened, release = threading.Event(), threading.Event()
    real_open = telemetry._open

    def held_open(path):
        con = real_open(path)
        if threading.current_thread().name == "leftover-worker":
            opened.set()
            assert release.wait(10), "the test releases the worker"
        return con
    monkeypatch.setattr(telemetry, "_open", held_open)

    errors = []

    def leftover_worker():
        try:
            telemetry.conn()
        except Exception as exc:  # noqa: BLE001 - reported below
            errors.append(exc)
        finally:
            telemetry.close_local()
    worker = threading.Thread(target=leftover_worker, name="leftover-worker")
    worker.start()
    assert opened.wait(10), "the worker opened the old file"
    telemetry.reset_for_tests(new)          # the next test switches files meanwhile
    release.set()
    worker.join(10)
    assert not errors

    # The new file has its schema, whatever the worker did with the old one.
    try:
        assert telemetry.conn().execute("SELECT count(*) FROM diagnostic_events").fetchone()[0] == 0
        assert telemetry.conn().execute("SELECT count(*) FROM hourly_metrics").fetchone()[0] == 0
    except sqlite3.OperationalError as exc:
        pytest.fail(f"the new telemetry file has no schema: {exc}")
    finally:
        telemetry.close_local()


def test_each_file_gets_its_schema_once_and_a_reset_makes_it_again(tmp_path):
    first = tmp_path / "first.sqlite"
    telemetry.reset_for_tests(first)
    telemetry.conn().execute("SELECT count(*) FROM diagnostic_events").fetchone()
    first.unlink()                          # the same path, a fresh file
    telemetry.reset_for_tests(first)
    try:
        assert telemetry.conn().execute("SELECT count(*) FROM diagnostic_events").fetchone()[0] == 0
    finally:
        telemetry.close_local()
