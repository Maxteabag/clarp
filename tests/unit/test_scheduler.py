"""Unit tests for agent scheduled jobs and cron runner."""
from __future__ import annotations

import pathlib
import pytest
from datetime import datetime, timezone

from lib import agents, db, scheduler


@pytest.fixture(autouse=True)
def _isolated_db(_transcript_importer_quiescent, tmp_path: pathlib.Path):
    db.reset_for_tests(tmp_path / "test_state.sqlite")
    yield
    # This override shadows conftest's, so it drains the importer itself.
    from lib import transcript_import_cache
    assert transcript_import_cache.reset_for_tests(), "transcript importer not quiescent"
    db.reset_for_tests(None)


def test_cron_parsing_and_shortcuts():
    # Valid 5-field cron
    mins, hours, doms, months, dows = scheduler.parse_cron("*/15 9-17 * * 1-5")
    assert mins == {0, 15, 30, 45}
    assert hours == set(range(9, 18))
    assert doms == set(range(1, 32))
    assert months == set(range(1, 13))
    assert dows == {1, 2, 3, 4, 5}

    # Shortcuts
    mins, hours, _, _, _ = scheduler.parse_cron("@hourly")
    assert mins == {0}
    assert hours == set(range(0, 24))

    # Invalid
    with pytest.raises(ValueError):
        scheduler.parse_cron("invalid cron")
    with pytest.raises(ValueError):
        scheduler.parse_cron("65 * * * *")


def test_compute_next_run():
    # Reference: 2026-09-04 10:05:30 UTC
    ref_dt = datetime(2026, 9, 4, 10, 5, 30, tzinfo=timezone.utc)
    ref_ms = int(ref_dt.timestamp() * 1000)

    # Next run for "15 10 * * *" should be 10:15:00 today
    next_ms = scheduler.compute_next_run("15 10 * * *", ref_ms)
    assert next_ms is not None
    next_dt = datetime.fromtimestamp(next_ms / 1000.0, tz=timezone.utc)
    assert next_dt.hour == 10
    assert next_dt.minute == 15
    assert next_dt.day == 4

    # Next run for "0 9 * * *" should be tomorrow at 09:00:00
    next_ms2 = scheduler.compute_next_run("0 9 * * *", ref_ms)
    assert next_ms2 is not None
    next_dt2 = datetime.fromtimestamp(next_ms2 / 1000.0, tz=timezone.utc)
    assert next_dt2.hour == 9
    assert next_dt2.minute == 0
    assert next_dt2.day == 5


def test_schedule_crud_and_due(tmp_path: pathlib.Path):
    from lib.agent_store import save_agents, load_agents
    save_agents({"mike": {"name": "Mike", "voice_id": "V1", "cwd": str(tmp_path), "backend": "claude"}})
    agent = load_agents()["mike"]
    session = "mike"

    # 1. Create
    sched = scheduler.create_schedule(
        session=session,
        name="Morning Standup",
        cron_expression="0 9 * * *",
        prompt="Prepare standup summary",
    )
    assert sched["name"] == "Morning Standup"
    assert sched["enabled"] is True
    assert sched["next_run_at"] is not None
    sid = sched["schedule_id"]

    # 2. Get & List
    fetched = scheduler.get_schedule(sid)
    assert fetched == sched

    s_list = scheduler.list_schedules(session=session)
    assert len(s_list) == 1
    assert s_list[0]["schedule_id"] == sid

    # 3. Update
    updated = scheduler.update_schedule(sid, enabled=False, name="Renamed Standup")
    assert updated["enabled"] is False
    assert updated["name"] == "Renamed Standup"
    assert updated["next_run_at"] is None

    # Re-enable
    re_enabled = scheduler.update_schedule(sid, enabled=True)
    assert re_enabled["enabled"] is True
    assert re_enabled["next_run_at"] is not None

    # 4. Due schedules
    far_future_ms = re_enabled["next_run_at"] + 1000
    due = scheduler.due_schedules(far_future_ms)
    assert len(due) == 1
    assert due[0]["schedule_id"] == sid

    # 5. Advance
    scheduler.advance_schedule(sid, far_future_ms)
    after_advance = scheduler.get_schedule(sid)
    assert after_advance["last_run_at"] == far_future_ms
    assert after_advance["next_run_at"] > far_future_ms

    # 6. Delete
    assert scheduler.delete_schedule(sid) is True
    assert scheduler.get_schedule(sid) is None


def test_schedule_runner_execution(tmp_path: pathlib.Path):
    from lib.agent_store import save_agents, load_agents
    save_agents({"worker": {"name": "Worker", "voice_id": "V2", "cwd": str(tmp_path), "backend": "claude"}})
    agent = load_agents()["worker"]

    dispatched = []

    def mock_dispatch(session: str, prompt: str):
        dispatched.append((session, prompt))

    sched = scheduler.create_schedule(
        session=agent["session"],
        name="Tick Task",
        cron_expression="* * * * *",
        prompt="Run periodic check",
    )

    runner = scheduler.AgentScheduleRunner(dispatch_turn=mock_dispatch)

    # Force schedule next_run_at to now so tick fires it
    now = db.now_ms()
    with db.conn() as c:
        c.execute(
            "UPDATE agent_schedules SET next_run_at = ? WHERE schedule_id = ?",
            (now - 1000, sched["schedule_id"]),
        )

    count = runner.tick()
    assert count == 1
    assert len(dispatched) == 1
    assert dispatched[0] == (agent["session"], "Run periodic check")

    # Second immediate tick should find 0 due because schedule advanced
    count2 = runner.tick()
    assert count2 == 0

def test_snapshot_includes_schedules(tmp_path: pathlib.Path):
    from lib.agent_store import save_agents, load_agents
    from lib.snapshot import build_agent_snapshot
    from unittest.mock import MagicMock

    save_agents({"alice": {"name": "Alice", "voice_id": "V1", "cwd": str(tmp_path), "backend": "claude"}})
    agent = load_agents()["alice"]

    sched = scheduler.create_schedule(
        session="alice",
        name="Nightly Audit",
        cron_expression="0 2 * * *",
        prompt="Audit security logs",
    )

    ctx = MagicMock()
    ctx.static = tmp_path
    snap = build_agent_snapshot(ctx)

    alice_snap = next(a for a in snap["agents"] if a["session"] == "alice")
    assert "schedules" in alice_snap
    assert len(alice_snap["schedules"]) == 1
    assert alice_snap["schedules"][0]["name"] == "Nightly Audit"
    assert alice_snap["schedules"][0]["schedule_id"] == sched["schedule_id"]


def test_archiving_maintenance_contact_preserves_scheduled_work(tmp_path):
    from lib import agents
    aid = agents.create_agent(persona='Maintenance', voice_id='', cwd=str(tmp_path),
                              session='maintenance', backend='codex')
    task = scheduler.create_schedule(session='maintenance', name='Maintenance pass',
                                     cron_expression='* * * * *', prompt='Check quietly')
    agents.set_archived(aid, True)
    db.conn().execute('UPDATE agent_schedules SET next_run_at=? WHERE schedule_id=?',
                      (db.now_ms()-1000, task['schedule_id']))
    calls = []
    assert scheduler.AgentScheduleRunner(dispatch_turn=lambda *args: calls.append(args)).tick() == 1
    assert calls == [('maintenance', 'Check quietly')]
    assert scheduler.get_schedule(task['schedule_id'])['enabled']


def _due_schedule(tmp_path):
    from lib.agent_store import load_agents, save_agents
    save_agents({"worker": {"name": "Worker", "voice_id": "V2", "cwd": str(tmp_path), "backend": "claude"}})
    agent = load_agents()["worker"]
    sched = scheduler.create_schedule(
        session=agent["session"], name="Tick Task", cron_expression="* * * * *",
        prompt="Run periodic check")
    with db.conn() as c:
        c.execute("UPDATE agent_schedules SET next_run_at = ? WHERE schedule_id = ?",
                  (db.now_ms() - 1000, sched["schedule_id"]))
    return agent


def test_a_raising_goal_tick_does_not_stop_the_cron_schedules(tmp_path, monkeypatch):
    """AUT-09: one bad goal must not starve every agent's cron schedules."""
    from lib import task_goal_recovery
    agent = _due_schedule(tmp_path)

    def broken(*_args, **_kwargs):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(task_goal_recovery, "tick", broken)
    dispatched = []
    runner = scheduler.AgentScheduleRunner(
        dispatch_turn=lambda session, prompt: dispatched.append((session, prompt)),
        dispatch_goal=lambda *_args: None)
    assert runner.tick() == 1
    assert dispatched == [(agent["session"], "Run periodic check")]


def test_a_plan_that_cannot_be_claimed_does_not_stop_the_others(tmp_path, monkeypatch):
    """AUT-09: a plan whose claim raises is skipped; the next plan still wakes."""
    from lib import task_goal_recovery
    agent_id = agents.create_agent(persona="Mike", voice_id="V", cwd=str(tmp_path), session="mike")
    now = db.now_ms()
    with db.conn() as c:
        for plan_id in ("plan-a", "plan-b"):
            c.execute(
                "INSERT INTO task_plans (plan_id, agent_id, session, title, recovery_enabled,"
                " status, created_at, updated_at) VALUES (?, ?, 'mike', ?, 1, 'active', ?, ?)",
                (plan_id, agent_id, plan_id, now, now))

    def claim(plan_id, _now):
        if plan_id == "plan-a":
            raise RuntimeError("database is locked")
        return {"session": "mike"}, {"continuation": {"request_id": "goal-wake-b"}}
    monkeypatch.setattr(task_goal_recovery, "_claim", claim)
    monkeypatch.setattr(task_goal_recovery, "_prompt", lambda plan, goal: "wake")
    results = []
    monkeypatch.setattr(task_goal_recovery, "_result",
                        lambda plan_id, request, _now, status, _reason: results.append((plan_id, status)))
    woken = []
    count = task_goal_recovery.tick(
        lambda session, prompt, request: woken.append(request) or {"queued": False}, now=now)
    assert count == 1 and woken == ["goal-wake-b"] and results == [("plan-b", "admitted")]


def test_a_plan_that_keeps_failing_is_logged_once_then_hourly(monkeypatch):
    """AUT-09: never swallowed, never spammed: the first failure per plan and
    error type with its traceback, then at most one line an hour."""
    from lib import task_goal_recovery
    monkeypatch.setattr(task_goal_recovery, "_FAILURE_LOGGED", {}, raising=False)
    traced, lines = [], []
    monkeypatch.setattr(task_goal_recovery, "log_exception",
                        lambda event, exc, detail="": traced.append((event, detail)))
    monkeypatch.setattr(task_goal_recovery, "log",
                        lambda event, detail="": lines.append(event), raising=False)
    error = RuntimeError("database is locked")
    start = 1_000_000_000
    for minutes in (0, 1, 2, 59):
        task_goal_recovery.log_failure_throttled(
            "goalRecoveryClaimFail", ("plan-a",), error, start + minutes * 60_000, detail="plan-a")
    assert traced == [("goalRecoveryClaimFail", "plan-a")] and lines == []
    task_goal_recovery.log_failure_throttled(
        "goalRecoveryClaimFail", ("plan-a",), error, start + 61 * 60_000, detail="plan-a")
    assert len(traced) == 1 and lines == ["goalRecoveryClaimFail"]
    task_goal_recovery.log_failure_throttled(
        "goalRecoveryClaimFail", ("plan-a",), ValueError("bad goal"), start + 62 * 60_000,
        detail="plan-a")
    assert len(traced) == 2                     # a new error type is traced again


def test_many_repeating_failures_are_not_traced_again_when_the_memory_is_full(monkeypatch):
    """AUT-09 (reviewer C's control on 41258db9): with more failing keys than
    the bounded memory holds, a key still failing is never traced again within
    the hour; the overflow is one aggregate line an hour, not a traceback each."""
    from lib import task_goal_recovery
    monkeypatch.setattr(task_goal_recovery, "_FAILURE_LOGGED", {}, raising=False)
    monkeypatch.setattr(task_goal_recovery, "_FAILURE_LOG_KEYS", 3, raising=False)
    traced, lines = [], []
    monkeypatch.setattr(task_goal_recovery, "log_exception",
                        lambda event, exc, detail="": traced.append(detail))
    monkeypatch.setattr(task_goal_recovery, "log",
                        lambda event, detail="": lines.append(detail), raising=False)
    error = RuntimeError("database is locked")
    for minute in (0, 3):
        for index in range(5):
            task_goal_recovery.log_failure_throttled(
                "goalRecoveryClaimFail", (f"plan-{index}",), error,
                1_000_000_000 + minute * 60_000, detail=f"plan-{index}")
    # Three slots: two failures tracked, one kept for the overflow marker.
    assert traced == ["plan-0", "plan-1"]
    assert len(lines) == 1 and "not tracked" in lines[0]
    assert len(task_goal_recovery._FAILURE_LOGGED) <= 3
