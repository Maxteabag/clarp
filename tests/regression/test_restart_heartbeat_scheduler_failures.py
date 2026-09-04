"""Unresolved restart, heartbeat, and schedule reliability regressions.

These tests intentionally describe required behavior that the current server
does not satisfy. Keep them red until the corresponding invariant is fixed.
"""
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


def test_production_boot_order_does_not_erase_interrupted_turn_evidence():
    """A runtime crash records dead work before stale-state reconciliation.

    HTTP replacement never owns this recovery. Exercise the production runtime
    entry point with the real SQLite recovery and reconciliation functions.
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

    result = _recover_runtime()

    assert result["interrupted"] == 1
    assert agents_db.latest_state(agent_id)["kind"] == AgentState.INTERRUPTED
    visible = message_store.list_messages(
        agent_id=agent_id,
        backend_session_id=backend_session_id,
        include_automated=False,
    )
    assert [row["text"] for row in visible][-1] == (
        interrupted_turns.RESTART_MARKER_TEXT
    )


def test_restart_recovery_closes_the_orphaned_durable_turn():
    agent_id = _agent("restart-turn-row")
    backend_session_id = _live_runtime(agent_id, "restart-turn-row")
    message_store.record_user_message(
        agent_id=agent_id,
        backend_session_id=backend_session_id,
        client_msg_id="restart-turn-row-message",
        text="Close the ledger row too",
        origin="user",
    )
    agents_db.open_turn(
        agent_id=agent_id,
        source="pwa",
        trace_id="restart-turn-row-trace",
        synthesize_audio=True,
    )
    agents_db.record_state(
        agent_id,
        AgentState.THINKING,
        {
            "trace_id": "restart-turn-row-trace",
            "backend_session_id": backend_session_id,
            "origin": "user",
        },
    )

    assert _recover_runtime()["interrupted"] == 1

    open_rows = db.conn().execute(
        "SELECT COUNT(*) AS n FROM turns WHERE agent_id=? AND ended_at IS NULL",
        (agent_id,),
    ).fetchone()["n"]
    assert open_rows == 0


def test_restart_finalizes_running_subagent_cells():
    """A killed child must not render as running forever after its parent dies."""
    agent_id = _agent("subagent-restart")
    backend_session_id = _live_runtime(agent_id, "subagent-restart")
    message_store.record_user_message(
        agent_id=agent_id,
        backend_session_id=backend_session_id,
        client_msg_id="subagent-parent-message",
        text="Delegate this work",
        origin="user",
    )
    message_store.store_transcript_turns(
        agent_id=agent_id,
        backend_session_id=backend_session_id,
        source_file="/tmp/interrupted-subagent.jsonl",
        turns=[{
            "id": "assistant-subagent-cell",
            "role": "assistant",
            "timestamp": "2026-09-04T00:00:00Z",
            "text": "",
            "display_cells": [{
                "id": "spawn-child",
                "kind": "subagents",
                "title": "Waiting for agents",
                "summary": "1 agent",
                "status": "running",
                "lines": [],
            }],
        }],
    )
    agents_db.record_state(
        agent_id,
        AgentState.TOOL,
        {
            "trace_id": "subagent-parent-trace",
            "backend_session_id": backend_session_id,
            "origin": "user",
        },
    )

    assert _recover_runtime()["interrupted"] == 1

    rows = message_store.list_messages(
        agent_id=agent_id,
        backend_session_id=backend_session_id,
        include_automated=False,
    )
    cells = [cell for row in rows for cell in row["display_cells"]]
    assert cells
    assert all(cell.get("status") != "running" for cell in cells)


def test_one_trace_cannot_create_two_durable_turn_rows():
    """Server dispatch and Claude's UserPromptSubmit hook both call open_turn."""
    agent_id = _agent("one-turn")
    _live_runtime(agent_id, "one-turn")

    agents_db.open_turn(
        agent_id=agent_id, source="pwa", trace_id="same-trace",
        synthesize_audio=True,
    )
    agents_db.open_turn(
        agent_id=agent_id, source="pwa", trace_id="same-trace",
        synthesize_audio=True,
    )

    rows = db.conn().execute(
        "SELECT turn_id FROM turns WHERE agent_id=? AND trace_id=?",
        (agent_id, "same-trace"),
    ).fetchall()
    assert len(rows) == 1


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


def test_heartbeat_dispatch_failure_is_visible_in_subsystem_health(monkeypatch):
    monkeypatch.setenv("CLAUDE_PWA_HEARTBEAT_INTERVAL_SEC", "60")
    monkeypatch.setenv("CLAUDE_PWA_HEARTBEAT_BACKOFF_CAP_SEC", "60")
    monkeypatch.setenv("CLAUDE_PWA_HEARTBEAT_QUIET_PERIOD_SEC", "0")
    _agent("heartbeat-health", heartbeat_enabled=True)
    health.reset_for_tests()
    worker = heartbeat.HeartbeatScheduler(
        send_heartbeat=lambda _session, _text: (_ for _ in ()).throw(
            RuntimeError("heartbeat dispatch broke")
        ),
        now=lambda: 1_000.0,
    )

    assert worker.run_once() == 0

    status = health.snapshot()["heartbeat"]
    assert status["last_error_at"] is not None
    assert "dispatch broke" in status["last_error"]


def test_heartbeat_callback_must_confirm_that_dispatch_was_accepted(monkeypatch):
    """The server callback can decline on a busy-race without raising."""
    monkeypatch.setenv("CLAUDE_PWA_HEARTBEAT_INTERVAL_SEC", "60")
    monkeypatch.setenv("CLAUDE_PWA_HEARTBEAT_BACKOFF_CAP_SEC", "60")
    monkeypatch.setenv("CLAUDE_PWA_HEARTBEAT_QUIET_PERIOD_SEC", "0")
    agent_id = _agent("heartbeat-declined", heartbeat_enabled=True)
    attempts: list[str] = []

    def decline(session: str, _text: str) -> bool:
        attempts.append(session)
        return False

    worker = heartbeat.HeartbeatScheduler(
        send_heartbeat=decline,
        now=lambda: 1_000.0,
    )

    assert worker.run_once() == 0
    assert heartbeat._state_for(agent_id).last_started == 0.0  # noqa: SLF001
    assert attempts == ["heartbeat-declined"]


def test_non_user_interruption_is_not_mistaken_for_an_explicit_stop(monkeypatch):
    monkeypatch.setenv("CLAUDE_PWA_HEARTBEAT_INTERVAL_SEC", "60")
    monkeypatch.setenv("CLAUDE_PWA_HEARTBEAT_BACKOFF_CAP_SEC", "60")
    monkeypatch.setenv("CLAUDE_PWA_HEARTBEAT_QUIET_PERIOD_SEC", "0")
    agent_id = _agent("heartbeat-interrupted", heartbeat_enabled=True)
    agents_db.record_state(
        agent_id,
        AgentState.INTERRUPTED,
        {"source": "backend", "reason": "interrupted"},
    )
    now = time.time() + 61

    due = heartbeat.pending_heartbeat_agents(now=now)

    assert [agent["agent_id"] for agent in due] == [agent_id]


def test_runtime_crash_recovery_isolates_continuity_dispatch_failure():
    """The new runtime continues recovering healthy agents after one failure.

    The old audit tested a legacy HTTP scheduler latch. That is not the runtime
    startup path. Retry/backoff for failed crash-continuity prompts remains a
    separate design question; never replay healthy agents to retry one failure.
    """
    attempts = []

    def send(**kwargs):
        session = kwargs["requested_session"]
        attempts.append(session)
        if session == "restart-flaky":
            raise RuntimeError("database was briefly locked")

    result = runtime_startup.recover_runtime(
        SimpleNamespace(stream=None),
        SimpleNamespace(dispatch=send, recover_queued=lambda: 0),
        restore_agents=lambda _ctx: None,
        mark_interrupted=lambda **_kwargs: [],
        reconcile=lambda: 0,
        restart_agents=lambda: [
            {"session": "restart-flaky"}, {"session": "restart-healthy"}],
        restart_prompt=lambda _agent: "Continue after runtime crash",
    )

    assert result["restart_heartbeats"] == 1
    assert attempts == ["restart-flaky", "restart-healthy"]


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


def test_deleted_agent_cannot_leave_active_background_work_behind():
    agent_id = _agent("background-deleted")
    job = background_jobs.upsert(
        session="background-deleted",
        job_id="deleted-agent-worker",
        kind="watcher",
        title="Watch forever",
    )

    agents_db.soft_delete(agent_id)

    current = background_jobs.get(job["job_id"], reconcile=False)
    assert current["status"] == "cancelled"
    assert not background_jobs.is_active(job["job_id"])


def test_leap_day_schedule_searches_far_enough_to_find_its_next_run():
    """A valid annual cron must not become permanently unscheduled."""
    start = datetime(2025, 3, 1, tzinfo=timezone.utc)
    result = scheduler.compute_next_run(
        "0 0 29 2 *",
        int(start.timestamp() * 1_000),
    )

    assert result == int(datetime(2028, 2, 29, tzinfo=timezone.utc).timestamp() * 1_000)
