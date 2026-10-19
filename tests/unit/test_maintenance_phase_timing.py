"""Diagnostic phase metadata of MaintenanceWorker.run_once (DB-lock attribution, lane 1).

One best-effort `maintenancePhases` log line per pass: for each fixed phase a monotonic EXECUTION duration (it includes
waits and is NOT lock-held time), a row count and the outcome. No SQL, parameters or content. Results, exceptions and the
existing failure handling are unchanged, and phases already timed are still logged when a later phase raises.
"""
from __future__ import annotations

import pytest

from lib import db
from lib import maintenance

PRUNE_PHASES = ["db.sse_events", "db.tts_queue", "db.state_log", "db.background_job_events", "db.clips",
                "db.turn_usage", "db.judgment_decisions", "db.tool_explanation_learning",
                "db.tool_explanation_programs"]


@pytest.fixture
def logged(monkeypatch):
    lines: list[tuple[str, str]] = []
    real = maintenance.log
    monkeypatch.setattr(maintenance, "log", lambda name, detail="": lines.append((name, detail)) or real(name, detail))
    return lines


def _phases(lines):
    [detail] = [detail for name, detail in lines if name == "maintenancePhases"]
    head, _, body = detail.partition(" | ")
    return head, [entry.split(":") for entry in body.split()]


def test_every_fixed_phase_is_logged_in_order_with_rows_and_outcome(tmp_path, logged):
    db.conn().execute("INSERT INTO sse_events (ts, type, payload) VALUES (1, 'x', '{}')")
    worker = maintenance.MaintenanceWorker(audio_dir=tmp_path)
    counts = worker.run_once(checkpoint=True)
    head, entries = _phases(logged)
    assert head == "execution_ms includes waits; NOT lock-held"
    names = [e[0] for e in entries]
    assert names == ["hls_artifacts", *PRUNE_PHASES, "event_logs", "helpers_archived", "telemetry", "wal_checkpoint"]
    for name, ms, rows, outcome in entries:
        assert ms.endswith("ms") and int(ms[:-2]) >= 0
        assert outcome == "ok"
    by_name = {e[0]: e for e in entries}
    assert by_name["db.sse_events"][2] == f"rows={counts['sse_events']}" == "rows=1"
    assert by_name["wal_checkpoint"][2] == "rows=-"
    detail = next(d for n, d in logged if n == "maintenancePhases")
    for forbidden in ("DELETE", "SELECT", "WHERE", "PRAGMA", "sse_events WHERE", "{}"):
        assert forbidden not in detail                      # no SQL, parameters or content
    names_logged = [n for n, _ in logged]
    assert names_logged.index("maintenancePruned") < names_logged.index("maintenancePhases")


def test_without_checkpoint_there_is_no_checkpoint_phase(tmp_path, logged):
    maintenance.MaintenanceWorker(audio_dir=tmp_path).run_once(checkpoint=False)
    _, entries = _phases(logged)
    assert "wal_checkpoint" not in [e[0] for e in entries]


def test_a_later_phase_that_raises_keeps_its_exception_and_the_earlier_phases(tmp_path, logged, monkeypatch):
    boom = RuntimeError("event log dir unreadable")

    def fail(*args, **kwargs):
        raise boom
    monkeypatch.setattr(maintenance, "prune_event_logs", fail)
    with pytest.raises(RuntimeError) as excinfo:
        maintenance.MaintenanceWorker(audio_dir=tmp_path).run_once(checkpoint=True)
    assert excinfo.value is boom                            # the original exception object, unchanged
    _, entries = _phases(logged)
    assert [e[0] for e in entries] == ["hls_artifacts", *PRUNE_PHASES, "event_logs"]
    assert all(e[3] == "ok" for e in entries[:-1])
    assert entries[-1][2:] == ["rows=-", "RuntimeError"]


def test_a_handled_phase_failure_is_still_handled_as_before(tmp_path, logged, monkeypatch):
    handled = []
    monkeypatch.setattr(maintenance, "archive_done_helpers",
                        lambda **kw: (_ for _ in ()).throw(ValueError("helper store")))
    monkeypatch.setattr(maintenance, "log_exception", lambda name, exc, *a, **k: handled.append((name, type(exc))))
    counts = maintenance.MaintenanceWorker(audio_dir=tmp_path).run_once(checkpoint=False)
    assert "helpers_archived" not in counts                 # the same result as before the diagnostic
    assert ("helperArchiveFail", ValueError) in handled
    _, entries = _phases(logged)
    assert {e[0]: e[3] for e in entries}["helpers_archived"] == "ValueError"


def test_a_failing_diagnostic_log_never_changes_results_or_exceptions(tmp_path, monkeypatch):
    real = maintenance.log

    def log(name, detail=""):
        if name == "maintenancePhases":
            raise OSError("log sink down")
        return real(name, detail)
    monkeypatch.setattr(maintenance, "log", log)
    worker = maintenance.MaintenanceWorker(audio_dir=tmp_path)
    assert isinstance(worker.run_once(checkpoint=False), dict)
    boom = RuntimeError("later phase")
    monkeypatch.setattr(maintenance, "prune_event_logs", lambda *a, **k: (_ for _ in ()).throw(boom))
    with pytest.raises(RuntimeError) as excinfo:
        worker.run_once(checkpoint=False)
    assert excinfo.value is boom                            # not the log sink's OSError


def test_prune_database_returns_the_same_result_with_or_without_a_recorder():
    policy = maintenance.Policy(sse_max_age_ms=100)
    c = db.conn()
    c.execute("INSERT INTO sse_events (ts, type, payload) VALUES (1, 'x', '{}')")
    phases = maintenance._Phases()
    recorded = maintenance.prune_database(now_ms=10_000, policy=policy, phases=phases)
    assert [r[0] for r in phases.records] == PRUNE_PHASES
    assert {r[0]: r[2] for r in phases.records}["db.sse_events"] == recorded["sse_events"] == 1
    c.execute("INSERT INTO sse_events (ts, type, payload) VALUES (1, 'x', '{}')")
    plain = maintenance.prune_database(now_ms=10_000, policy=policy)
    assert plain == recorded                                # identical counts on identical data, no recorder


def test_the_recorder_times_with_its_clock_counts_rows_and_is_bounded():
    ticks = iter([10.0, 10.25, 20.0, 20.5, 30.0, 30.001])
    phases = maintenance._Phases(clock=lambda: next(ticks))
    assert phases.run("a", lambda: 7) == 7
    assert phases.run("b", lambda: {"x": 2, "y": 3, "flag": True, "s": "z"}) == {"x": 2, "y": 3, "flag": True, "s": "z"}
    assert phases.run("c", lambda: True) is True
    assert phases.records == [("a", 250, 7, "ok"), ("b", 500, 5, "ok"), ("c", 1, None, "ok")]
    many = maintenance._Phases(clock=lambda: 0.0)
    for i in range(maintenance._Phases.MAX_PHASES + 5):
        many.run(f"p{i}", lambda: 0)
    assert len(many.records) == maintenance._Phases.MAX_PHASES


def test_a_failing_clock_never_stops_repeats_or_replaces_the_phase():
    class ClockDown(Exception):
        pass

    def broken_clock():
        raise ClockDown("clock unavailable")

    def clock_failing_on_second_read():
        reads = iter([1.0])
        return lambda: next(reads)                      # the second (final) read raises StopIteration, an ordinary Exception

    for label, clock in (("initial read", broken_clock), ("final read", clock_failing_on_second_read())):
        calls = []

        def work():
            calls.append(1)
            return 42
        phases = maintenance._Phases(clock=clock)
        assert phases.run("p", work) == 42, label         # original result
        assert calls == [1], label                         # fn ran exactly once, no retry

        failure = LookupError("work failed")
        calls.clear()

        def failing_work():
            calls.append(1)
            raise failure
        phases = maintenance._Phases(clock=clock if label == "initial read" else clock_failing_on_second_read())
        with pytest.raises(LookupError) as excinfo:
            phases.run("p", failing_work)
        assert excinfo.value is failure, label             # the ORIGINAL exception object, not the clock's
        assert excinfo.value.__context__ is None, label    # unchanged: a fresh failure, no clock error chained onto it
        assert calls == [1], label
