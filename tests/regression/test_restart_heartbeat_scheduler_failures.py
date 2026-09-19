"""TDD reproduction: Durable scheduled occurrence admission. Implementation pending."""
from __future__ import annotations

from datetime import datetime, timezone
import threading
import time
from types import SimpleNamespace

from lib import agents as agents_db
from lib import (
    background_jobs,
    db,
    health,
    heartbeat,
    interrupted_turns,
    message_store,
    reconcile,
    runtime_startup,
    scheduler,
)
from lib.protocol import AgentState


def _agent(session: str, *, heartbeat_enabled: bool = False) -> str:
    agent_id = agents_db.create_agent(
        persona=session.capitalize(),
        voice_id="voice",
        cwd="/tmp",
        session=session,
        backend="claude",
    )
    agents_db.update_agent(agent_id, heartbeat_enabled=heartbeat_enabled)
    return agent_id


def _live_runtime(agent_id: str, session: str) -> str:
    agents_db.start_runtime(agent_id, session)
    backend_session_id = f"backend-{session}"
    agents_db.bind_backend_session(agent_id, backend_session_id)
    return backend_session_id


def _due_schedule(session: str, *, prompt: str = "Do the durable work") -> dict:
    item = scheduler.create_schedule(
        session=session,
        name=f"{session} schedule",
        cron_expression="* * * * *",
        prompt=prompt,
    )
    db.conn().execute(
        "UPDATE agent_schedules SET next_run_at = ? WHERE schedule_id = ?",
        (db.now_ms() - 1_000, item["schedule_id"]),
    )
    return scheduler.get_schedule(item["schedule_id"]) or {}


def _recover_runtime():
    return runtime_startup.recover_runtime(
        SimpleNamespace(stream=None),
        SimpleNamespace(recover_queued=lambda: 0),
        restore_agents=lambda _ctx: None,
        restart_agents=lambda: [],
    )




















def test_failed_scheduled_dispatch_remains_due_for_retry():
    """Advancing before dispatch silently loses a failed run until next cron."""
    _agent("scheduled-retry")
    item = _due_schedule("scheduled-retry")

    def fail(_session: str, _prompt: str) -> None:
        raise RuntimeError("backend unavailable")

    worker = scheduler.AgentScheduleRunner(dispatch_turn=fail)
    assert worker.tick() == 0

    after = scheduler.get_schedule(item["schedule_id"])
    assert after is not None
    assert after["last_run_at"] is None
    assert after["next_run_at"] <= db.now_ms()


def test_two_scheduler_workers_cannot_dispatch_the_same_due_run_twice(monkeypatch):
    """Reading due rows and claiming one must be a single atomic operation."""
    _agent("scheduled-once")
    _due_schedule("scheduled-once")
    real_due = scheduler.due_schedules
    both_have_read = threading.Barrier(2)
    dispatched: list[tuple[str, str]] = []
    dispatched_lock = threading.Lock()
    failures: list[BaseException] = []

    def synchronized_due(now: int | None = None):
        rows = real_due(now)
        both_have_read.wait(timeout=5)
        return rows

    def dispatch(session: str, prompt: str) -> None:
        with dispatched_lock:
            dispatched.append((session, prompt))

    monkeypatch.setattr(scheduler, "due_schedules", synchronized_due)

    def tick() -> None:
        try:
            scheduler.AgentScheduleRunner(dispatch_turn=dispatch).tick()
        except BaseException as exc:  # make thread failures visible to pytest
            failures.append(exc)

    threads = [threading.Thread(target=tick) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not failures
    assert not any(thread.is_alive() for thread in threads)
    assert dispatched == [("scheduled-once", "Do the durable work")]


def test_schedule_disabled_after_due_read_cannot_still_dispatch(monkeypatch):
    _agent("scheduled-disabled")
    item = _due_schedule("scheduled-disabled")
    real_due = scheduler.due_schedules
    dispatched: list[tuple[str, str]] = []

    def due_then_disable(now: int | None = None):
        rows = real_due(now)
        scheduler.update_schedule(item["schedule_id"], enabled=False)
        return rows

    monkeypatch.setattr(scheduler, "due_schedules", due_then_disable)
    worker = scheduler.AgentScheduleRunner(
        dispatch_turn=lambda session, prompt: dispatched.append((session, prompt))
    )

    assert worker.tick() == 0
    assert dispatched == []


def test_deleted_agent_cannot_leave_an_enabled_schedule_behind():
    """Soft deletion should retire autonomous work tied to that agent."""
    agent_id = _agent("scheduled-deleted")
    item = _due_schedule("scheduled-deleted")

    agents_db.soft_delete(agent_id)

    assert scheduler.get_schedule(item["schedule_id"])["enabled"] is False
    assert scheduler.due_schedules(db.now_ms()) == []




def test_leap_day_schedule_searches_far_enough_to_find_its_next_run():
    """A valid annual cron must not become permanently unscheduled."""
    start = datetime(2025, 3, 1, tzinfo=timezone.utc)
    result = scheduler.compute_next_run(
        "0 0 29 2 *",
        int(start.timestamp() * 1_000),
    )

    assert result == int(datetime(2028, 2, 29, tzinfo=timezone.utc).timestamp() * 1_000)
