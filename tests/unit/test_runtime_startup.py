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


def test_runtime_restart_marks_interrupted_work_without_submitting_continuity():
    from lib import agents, turn_lifecycle
    from lib.turn_lifecycle import TurnEvent

    aid = agents.create_agent(persona="Theo", voice_id="", cwd="/tmp", session="theo")
    agents.start_runtime(aid, "theo")
    agents.open_turn(agent_id=aid, source="pwa", trace_id="original-request")
    turn_lifecycle.transition(aid, TurnEvent.SPAWN_STARTED,
                              {"trace_id": "original-request", "origin": "user"})
    submitted = []
    result = recover_runtime(
        SimpleNamespace(stream=None),
        SimpleNamespace(submit=submitted.append, recover_queued=lambda: 3),
        restore_agents=lambda _ctx: None,
    )
    assert result["interrupted"] == 1
    assert agents.latest_state(aid)["detail"]["source"] == "server_restart"
    assert submitted == [], "the restarting agent, not startup, decides who to continue"
    assert result["queued"] == 3


def test_clean_runtime_handoff_does_not_invent_an_interruption():
    order = []
    result = recover_runtime(
        SimpleNamespace(stream="stream"),
        SimpleNamespace(recover_queued=lambda: order.append("queues") or 1),
        clean_handoff=True,
        restore_agents=lambda _ctx: order.append("restore"),
        mark_interrupted=lambda stream=None: order.append("interrupt") or [],
        reconcile=lambda: order.append("reconcile") or 0,
    )

    assert order == ["restore", "reconcile", "queues"]
    assert result["interrupted"] == 0


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
    )
    assert seen == [SQLITE_RECOVERY_BUSY_TIMEOUT_MS]
