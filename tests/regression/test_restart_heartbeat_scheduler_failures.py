"""Unresolved restart, heartbeat, and schedule reliability regressions.

These tests intentionally describe required behavior that the current server
does not satisfy. Keep them red until the corresponding invariant is fixed.
"""
from __future__ import annotations

from datetime import datetime, timezone
import threading

from lib import agents as agents_db
from lib import db, heartbeat, interrupted_turns, message_store, reconcile, scheduler
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


def test_production_boot_order_does_not_erase_interrupted_turn_evidence():
    """Issue #11's recovery must run before generic stale-state repair.

    The production ``__main__`` path currently calls ``reconcile_all`` before
    ``build_server(..., restart_recovery=True)``. Reconciliation changes the
    stale THINKING row to IDLE, so restart recovery no longer recognizes the
    orphaned turn and never writes its user-visible marker.
    """
    agent_id = _agent("restart-order")
    backend_session_id = _live_runtime(agent_id, "restart-order")
    message_store.record_user_message(
        agent_id=agent_id,
        backend_session_id=backend_session_id,
        client_msg_id="restart-order-message",
        text="Do not lose this turn",
        origin="user",
    )
    agents_db.record_state(
        agent_id,
        AgentState.THINKING,
        {
            "trace_id": "restart-order-trace",
            "backend_session_id": backend_session_id,
            "origin": "user",
        },
    )

    # Mirror the ordering in server/server.py's production entry point.
    reconcile.reconcile_all()
    recovered = interrupted_turns.recover_after_restart()

    assert [row["agent_id"] for row in recovered] == [agent_id]
    assert agents_db.latest_state(agent_id)["kind"] == AgentState.INTERRUPTED
    visible = message_store.list_messages(
        agent_id=agent_id,
        backend_session_id=backend_session_id,
        include_automated=False,
    )
    assert [row["text"] for row in visible][-1] == (
        interrupted_turns.RESTART_MARKER_TEXT
    )


def test_failed_periodic_heartbeat_is_retryable_without_waiting_a_full_interval(
    monkeypatch,
):
    """A failed dispatch is not a successfully started heartbeat."""
    monkeypatch.setenv("CLAUDE_PWA_HEARTBEAT_INTERVAL_SEC", "60")
    monkeypatch.setenv("CLAUDE_PWA_HEARTBEAT_BACKOFF_CAP_SEC", "60")
    monkeypatch.setenv("CLAUDE_PWA_HEARTBEAT_QUIET_PERIOD_SEC", "0")
    agent_id = _agent("heartbeat-retry", heartbeat_enabled=True)
    attempts: list[str] = []

    def fail(session: str, _text: str) -> None:
        attempts.append(session)
        raise RuntimeError("transient dispatch failure")

    worker = heartbeat.HeartbeatScheduler(
        send_heartbeat=fail,
        now=lambda: 1_000.0,
    )

    assert worker.run_once() == 0
    assert worker.run_once() == 0
    assert attempts == ["heartbeat-retry", "heartbeat-retry"]
    assert heartbeat._state_for(agent_id).last_started == 0.0  # noqa: SLF001


def test_restart_heartbeat_retries_an_agent_whose_first_dispatch_failed(monkeypatch):
    """The once-per-process latch must not permanently consume failed work."""
    monkeypatch.setenv("CLAUDE_PWA_HEARTBEAT_QUIET_PERIOD_SEC", "0")
    flaky = _agent("restart-flaky")
    healthy = _agent("restart-healthy")
    _live_runtime(flaky, "restart-flaky")
    _live_runtime(healthy, "restart-healthy")
    attempts: list[str] = []

    def send(session: str, _text: str) -> None:
        attempts.append(session)
        if session == "restart-flaky" and attempts.count(session) == 1:
            raise RuntimeError("database was briefly locked")

    worker = heartbeat.HeartbeatScheduler(
        send_heartbeat=send,
        now=lambda: 1_000.0,
    )

    assert worker.run_restart_recovery_once() == 1
    assert worker.run_restart_recovery_once() == 1
    assert attempts.count("restart-flaky") == 2
    assert attempts.count("restart-healthy") == 1


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
