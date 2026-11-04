"""TP-03: agent create loaded the whole roster, did seconds of I/O (session
listing, a native fork), then wrote the whole stale roster back: an agent
deleted meanwhile came back, and another agent's change was reverted. A create
writes only the agent it creates."""
from __future__ import annotations

from types import SimpleNamespace

from lib import agents
from lib.agent_lifecycle import AgentLifecycleService


def _service(tmp_path):
    return AgentLifecycleService(SimpleNamespace(
        agents_path=tmp_path / "unused.json", stream=SimpleNamespace(broadcast=lambda _event: None),
        speak_announcement=lambda *_a, **_k: None))


def _create_newcomer_while(tmp_path, monkeypatch, meanwhile):
    service = _service(tmp_path)

    def during_io(_spec):
        meanwhile()
        return ""

    monkeypatch.setattr(service, "_fork_or_resume", during_io)
    service.create({"name": "Newcomer", "session": "newcomer", "voice_id": "new-voice",
                    "cwd": str(tmp_path), "backend": "codex"})
    assert agents.get_by_session("newcomer") is not None


def test_a_create_does_not_bring_back_an_agent_deleted_during_its_io(tmp_path, monkeypatch):
    victim = agents.create_agent(persona="Victim", voice_id="v", cwd=str(tmp_path), session="victim")
    _create_newcomer_while(tmp_path, monkeypatch, lambda: agents.soft_delete(victim))
    assert agents.get_by_session("victim") is None


def test_a_create_does_not_revert_another_agents_change_during_its_io(tmp_path, monkeypatch):
    bob = agents.create_agent(persona="Bob", voice_id="v", cwd=str(tmp_path), session="bob",
                              backend="claude")
    _create_newcomer_while(tmp_path, monkeypatch,
                           lambda: agents.update_agent(bob, backend="codex", cwd=str(tmp_path / "work")))
    after = agents.get_by_agent_id(bob)
    assert (after["backend"], after["cwd"]) == ("codex", str(tmp_path / "work"))
