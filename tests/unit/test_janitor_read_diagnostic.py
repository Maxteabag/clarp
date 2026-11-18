"""The temporary read measurement reports only numeric, per-thread aggregates."""
from types import SimpleNamespace

from lib import janitor_autonomy
from tests.unit.test_goal_keeper import owner  # noqa: F401


def test_disabled_meter_does_not_sample_and_enabled_meter_emits_once_per_twenty(monkeypatch):
    from lib import config, eventlog, janitor_read_diagnostic as diag
    worker = janitor_autonomy.AutonomyJanitors(None, None)
    clock = [0]
    samples = []
    def sample():
        samples.append(1)
        return clock[0], 0
    records = []
    monkeypatch.setattr(diag, "thread_rchar", sample)
    monkeypatch.setattr(eventlog, "emit", lambda _source, name, **fields: records.append((name, fields["detail"])))
    switch = SimpleNamespace(janitor_read_diagnostics=False)
    monkeypatch.setattr(config, "load", lambda: switch)
    calls = []
    def lane(name):
        def run():
            calls.append(name)
            clock[0] += 1_000_000
            if name == "keeper" and worker._read_meter:
                worker._read_meter.work()
                worker._read_meter.sweep = True
        return run
    lanes = ["setup", "heartbeat", "quota", "hotseat", "labels", "keeper"]
    monkeypatch.setattr(janitor_autonomy, "setup", lane("setup"))
    for method, name in zip(("heartbeat_once", "quota_once", "hotseat_once", "label_audit_once", "goal_keeper_once"), lanes[1:]):
        monkeypatch.setattr(worker, method, lane(name))
    monkeypatch.setattr(janitor_autonomy, "_SCHEMA_READY_FOR", janitor_autonomy.db.DB_PATH)
    worker.run_once()
    assert calls == lanes and not samples and not records
    switch.janitor_read_diagnostics = True
    for _ in range(19):
        worker.run_once()
    assert not records
    worker.run_once()
    assert calls == lanes * 21
    [(event, detail)] = records
    assert event == "janitorAutonomyReads" and detail["passes"] == 20
    assert detail["lane_mb"] == dict.fromkeys(lanes, 20)
    assert detail["worked"] == ["keeper"] and detail["sweep"] and not detail["self_check"]


def test_keeper_reader_groups_queries_and_marks_sweep_and_self_check(owner):
    from lib import db, goal_keeper
    from lib.janitor_read_diagnostic import ReadMeter
    from tests.unit.test_goal_keeper import _config, _goal

    _goal()
    now = db.now_ms()
    memo = goal_keeper.KeeperMemo()
    memo.meter = ReadMeter()
    memo.passes = 19
    goal_keeper.due(_config(), {"next_sweep_at": now}, now, memo=memo)
    assert memo.meter.self_check and memo.meter.sweep
    assert {"plain_plans", "plan_headers", "helpers", "heads", "messages", "events", "jobs"} <= memo.meter.families.keys()


def test_twentieth_report_reaches_journal_with_capture_off(capfd):
    import json
    from lib import diagnostics_settings, janitor_read_diagnostic as diag, telemetry

    meter = diag.ReadMeter()
    for enabled in (False, True):
        diagnostics_settings.update({"enabled": enabled, "categories": list(diagnostics_settings.CATEGORIES)})
        for _ in range(19):
            meter.finish_pass()
        assert "janitorAutonomyReads" not in capfd.readouterr().err
        meter.lanes["keeper"] = 1_500_000
        meter.finish_pass()
        event = " janitorAutonomyReads "
        lines = [line for line in capfd.readouterr().err.splitlines() if event in line]
        assert len(lines) == 1
        detail = json.loads(lines[0].split(event, 1)[1])
        assert detail == {
            "passes": 20, "lane_mb": {**dict.fromkeys(diag.LANES, 0), "keeper": 1.5},
            "keeper_family_mb": dict.fromkeys(diag.FAMILIES, 0), "keeper_other_mb": 1.5,
            "worked": [], "sweep": False, "self_check": False, "sample_unavailable": False,
        }
        rows = telemetry.conn().execute(
            "SELECT detail FROM diagnostic_events WHERE event=?", ("janitorAutonomyReads",)
        )
        assert [json.loads(row[0]) for row in rows] == ([detail] if enabled else [])
