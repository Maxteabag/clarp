"""TDD reproduction: Turn admission, ledger closure, and stale hook ownership. Implementation pending."""
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






















