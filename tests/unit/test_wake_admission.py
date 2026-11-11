"""Shared autonomous wake-admission and queue-hold checks."""
from __future__ import annotations

from lib import agents as agents_db
from lib import backends, compaction, turn_lifecycle, turn_queue, wake_admission
from lib.protocol import AgentState
from lib.turn_lifecycle import TurnEvent
from tests.patching import patch_shared


def test_holds_autonomous_wake_covers_pause_queued_and_claimed_per_agent():
    target = agents_db.create_agent(
        persona="Target", voice_id="V", cwd="/tmp", session="target-wake")
    other = agents_db.create_agent(
        persona="Other", voice_id="V", cwd="/tmp", session="other-wake")

    assert turn_queue.holds_autonomous_wake(target) is False

    turn_queue.set_paused(target, True)
    assert turn_queue.holds_autonomous_wake(target) is True
    assert turn_queue.holds_autonomous_wake(other) is False
    turn_queue.set_paused(target, False)
    assert turn_queue.holds_autonomous_wake(target) is False

    turn_queue.enqueue(
        queue_id="q-1", agent_id=target, session="target-wake",
        text="next", trace_id="t-1", client_msg_id="q-1",
        synthesize_audio=False, origin="user", sender_agent_id="",
    )
    assert turn_queue.holds_autonomous_wake(target) is True
    assert turn_queue.holds_autonomous_wake(other) is False

    claimed = turn_queue.claim("q-1")
    assert claimed is not None and claimed["queue_id"] == "q-1"
    # Even after leaving 'queued' for 'claimed', autonomous wakes must stay out.
    assert turn_queue.state(target)["count"] == 0
    assert turn_queue.holds_autonomous_wake(target) is True

    turn_queue.mark_started("q-1")
    assert turn_queue.holds_autonomous_wake(target) is False
    turn_queue.settle_receipt(target, "t-1", "completed")
    assert turn_queue.holds_autonomous_wake(target) is False


def test_execution_busy_reason_distinguishes_busy_active_and_compacting(monkeypatch):
    agent_id = agents_db.create_agent(
        persona="Dreamer", voice_id="V", cwd="/tmp", session="dreamer-wake")
    agent = agents_db.get_by_agent_id(agent_id)
    assert wake_admission.execution_busy_reason(agent) == ""

    turn_lifecycle.transition(agent_id, TurnEvent.PROMPT_ADMITTED, {"source": "pwa"})
    assert wake_admission.execution_busy_reason(agent) == "busy"
    assert wake_admission.execution_busy_reason(agent, ignore_busy_handles=True) == ""

    turn_lifecycle.transition(agent_id, TurnEvent.PROCESS_EXITED_OK)
    patch_shared(
        monkeypatch,
        backends,
        "active_handles",
        lambda _backend, aid: [object()] if aid == agent_id else [],
    )
    assert wake_admission.execution_busy_reason(agent) == "active"
    assert wake_admission.execution_busy_reason(agent, ignore_busy_handles=True) == ""

    monkeypatch.setattr(
        compaction,
        "is_compacting",
        lambda session: session == "dreamer-wake",
    )
    assert (
        wake_admission.execution_busy_reason(agent, ignore_busy_handles=True)
        == "compacting"
    )
    monkeypatch.setattr(compaction, "is_compacting", lambda _session: False)
    assert (
        wake_admission.execution_busy_reason(
            agent,
            latest={"kind": AgentState.COMPACTING},
            ignore_busy_handles=True,
        )
        == "compacting"
    )


def test_recent_real_activity_reason_respects_window_and_logs_errors(monkeypatch):
    agent_id = agents_db.create_agent(
        persona="Leader", voice_id="V", cwd="/tmp", session="leader-wake")
    agents_db.start_runtime(agent_id, "leader-wake")
    agents_db.bind_backend_session(agent_id, "bs-wake")
    agents_db.record_user_message(
        agent_id=agent_id, backend_session_id="bs-wake",
        text="hello", client_msg_id="m-1", origin="user", trace_id="tr-1",
    )
    from lib import message_store
    last_ms = message_store.last_real_message_activity(agent_id=agent_id)
    now = (last_ms / 1000.0) + 12.0

    assert (
        wake_admission.recent_real_activity_reason(
            agent_id, now=now, quiet_period_sec=0, failure_event="testWakeFail"
        )
        == ""
    )
    assert (
        wake_admission.recent_real_activity_reason(
            agent_id, now=now, quiet_period_sec=30, failure_event="testWakeFail"
        )
        == "recent-activity:12s"
    )
    assert (
        wake_admission.recent_real_activity_reason(
            agent_id, now=now + 30.0, quiet_period_sec=30, failure_event="testWakeFail"
        )
        == ""
    )

    logged: list[str] = []
    monkeypatch.setattr(
        wake_admission,
        "log_exception",
        lambda event, _exc, **kw: logged.append(f"{event}:{kw.get('detail')}"),
    )
    assert (
        wake_admission.recent_real_activity_reason(
            agent_id,
            now=now,
            quiet_period_sec=30,
            failure_event="testWakeFail",
            activity_ms_fn=lambda _aid: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        == "activity-unknown"
    )
    assert logged == [f"testWakeFail:{agent_id}"]
