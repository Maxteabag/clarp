"""Unit tests for agent scheduled jobs and cron runner."""
from __future__ import annotations

import pathlib
import pytest
from datetime import datetime, timezone

from lib import agents, db, scheduler


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: pathlib.Path):
    db.reset_for_tests(tmp_path / "test_state.sqlite")
    yield
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
