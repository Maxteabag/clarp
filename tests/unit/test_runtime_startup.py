from __future__ import annotations

from types import SimpleNamespace

from lib.runtime_startup import recover_runtime


def test_runtime_recovery_marks_dead_work_before_reconcile_and_continuity():
    order = []
    dispatch = SimpleNamespace(
        dispatch=lambda **kwargs: order.append(("dispatch", kwargs)),
        recover_queued=lambda: order.append("queues") or 3,
    )

    result = recover_runtime(
        SimpleNamespace(stream="stream"),
        dispatch,
        restore_agents=lambda _ctx: order.append("restore"),
        mark_interrupted=lambda stream=None: order.append(
            ("interrupt", stream)) or [{"agent_id": "a"}],
        reconcile=lambda: order.append("reconcile") or 1,
        restart_agents=lambda: [{"session": "theo"}],
        restart_prompt=lambda _agent: "runtime restarted",
    )

    assert order == [
        "restore",
        ("interrupt", "stream"),
        "reconcile",
        ("dispatch", {
            "text": "runtime restarted",
            "requested_session": "theo",
            "forced_session": "theo",
            "trace_id": result["restart_trace_ids"][0],
            "synthesize_audio": False,
            "origin": "heartbeat",
        }),
        "queues",
    ]
    assert result["interrupted"] == 1
    assert result["reconciled"] == 1
    assert result["restart_heartbeats"] == 1
    assert result["queued"] == 3


def test_runtime_recovery_isolates_one_failed_continuity_prompt():
    sent = []

    def dispatch(**kwargs):
        sent.append(kwargs["requested_session"])
        if kwargs["requested_session"] == "broken":
            raise RuntimeError("provider unavailable")

    result = recover_runtime(
        SimpleNamespace(stream=None),
        SimpleNamespace(dispatch=dispatch, recover_queued=lambda: 0),
        restore_agents=lambda _ctx: None,
        mark_interrupted=lambda stream=None: [],
        reconcile=lambda: 0,
        restart_agents=lambda: [
            {"session": "broken"}, {"session": "healthy"}],
        restart_prompt=lambda agent: f"continue {agent['session']}",
    )

    assert sent == ["broken", "healthy"]
    assert result["restart_heartbeats"] == 1


def test_clean_runtime_handoff_does_not_invent_an_interruption():
    order = []
    result = recover_runtime(
        SimpleNamespace(stream="stream"),
        SimpleNamespace(recover_queued=lambda: order.append("queues") or 1),
        clean_handoff=True,
        restore_agents=lambda _ctx: order.append("restore"),
        mark_interrupted=lambda stream=None: order.append("interrupt") or [],
        reconcile=lambda: order.append("reconcile") or 0,
        restart_agents=lambda: order.append("restart-agents") or [],
        restart_prompt=lambda _agent: "unused",
    )

    assert order == ["restore", "reconcile", "queues"]
    assert result["interrupted"] == 0
    assert result["restart_heartbeats"] == 0
