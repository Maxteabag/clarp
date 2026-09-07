from __future__ import annotations

from types import SimpleNamespace

from lib.runtime_startup import recover_runtime


def test_ephemeral_janitors_do_not_start_unused_chat_runtimes(tmp_path, monkeypatch):
    from lib import agents, janitors, janitor_builtins, runtime_startup
    agents.create_agent(persona="Sam", voice_id="", cwd=str(tmp_path), session="sam")
    agents.create_agent(persona="Rivet", voice_id="", cwd=str(tmp_path), session="rivet")
    janitors.create("rivet")
    janitor_builtins.ensure_builtins(cwd=str(tmp_path))
    restored = []
    monkeypatch.setattr(runtime_startup, "resume_missing_sessions",
        lambda rows, *args, **kwargs: restored.extend(rows) or [])
    runtime_startup.restore_persisted_agents(SimpleNamespace(agents_path=tmp_path / "unused.json"))
    assert set(restored) == {"sam", "rivet"}


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


def test_runtime_recovery_raises_sqlite_busy_timeout_during_boot(monkeypatch):
    from contextlib import contextmanager

    from lib import runtime_startup
    from lib.timing import SQLITE_RECOVERY_BUSY_TIMEOUT_MS

    seen: list[int] = []

    @contextmanager
    def fake_timeout(timeout_ms):
        seen.append(timeout_ms)
        yield

    monkeypatch.setattr(runtime_startup.db, "busy_timeout", fake_timeout)
    recover_runtime(
        SimpleNamespace(stream=None),
        SimpleNamespace(recover_queued=lambda: 0),
        restore_agents=lambda _ctx: None,
        mark_interrupted=lambda stream=None: [],
        reconcile=lambda: 0,
        restart_agents=lambda: [],
        restart_prompt=lambda _agent: "unused",
    )
    assert seen == [SQLITE_RECOVERY_BUSY_TIMEOUT_MS]
