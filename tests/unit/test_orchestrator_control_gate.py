"""RTO-03: router control decisions get the confidence bar messages have,
reach only the agents in the utterance's packet (a group call's
participants), and carry no model-supplied identity, directory, backend,
tools or fork source into agent creation. The packet holds other agents'
transcript text, so the model's payload is untrusted."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from lib import agent_lifecycle, agents, backends, db, orchestrator, prompt_admissions
from tests.unit.test_orchestrator_janitor import ROLE, builtins


@pytest.fixture(autouse=True)
def no_processes(monkeypatch):
    monkeypatch.setattr(backends, "active_handles", lambda *args: [])


@pytest.fixture
def effects(monkeypatch):
    seen = {"interrupted": [], "created": []}
    monkeypatch.setattr(backends, "interrupt_any", lambda agent_id: seen["interrupted"].append(agent_id))

    class Lifecycle:
        def __init__(self, _ctx):
            pass

        def create(self, payload):
            seen["created"].append(dict(payload))
            return SimpleNamespace(session=payload.get("session") or "new")
    monkeypatch.setattr(agent_lifecycle, "AgentLifecycleService", Lifecycle)
    return seen


def router(decision, *, fallback_only=False):
    for session in ("mike", "antoni"):
        agents.create_agent(persona=session.title(), voice_id="", cwd="/tmp", session=session)
    builtins().ensure_builtins(initial={ROLE: {
        "enabled": True, "backend": "codex", "provider": "codex",
        "model": "gpt-5.3-codex-spark", "effort": "",
        "options": {"fallback_only": fallback_only}}})
    dispatched = []
    context = SimpleNamespace(
        default_session="mike", stream=SimpleNamespace(broadcast=lambda _e: None),
        speak_announcement=lambda *args, **kwargs: None)
    service = orchestrator.OrchestratorService(context, model_call=lambda *_: dict(decision))

    def send(*, fallback_request=False, only_sessions=None):
        admission = prompt_admissions.create(
            authenticated_at_admission=True, origin="user", sender_agent_id="",
            channel="voice", observed_at=db.now_ms(), client_admission_id="request-1",
            trace_id="route-1", original_text="private user utterance")

        def dispatch(**kwargs):
            dispatched.append(kwargs)
            return SimpleNamespace(session=kwargs["forced_session"], backend="claude")
        return service.handle_send(
            text=admission.original_text, requested_session="mike", trace_id="route-1",
            prompt_admission=admission, hands_free=True, synthesize_audio=False,
            dispatch=dispatch, fallback_request=fallback_request, only_sessions=only_sessions)
    return send, dispatched


def stop(confidence):
    return {"kind": "agent_control", "control_action": "stop", "target_session": "antoni",
            "confidence": confidence, "reason": "stop"}


def test_a_low_confidence_stop_only_clarifies(effects):
    send, _ = router(stop(0.3))
    assert send().action == "clarify"
    assert effects["interrupted"] == []


def test_a_confident_stop_still_stops(effects):
    send, _ = router(stop(0.95))
    assert send().action == "control"
    assert effects["interrupted"] == [agents.get_by_session("antoni")["agent_id"]]


def test_control_reaches_only_the_agents_of_the_call(effects):
    send, _ = router(stop(0.95))
    assert send(only_sessions=("mike",)).action == "clarify"
    assert effects["interrupted"] == []


INJECTED = {"name": "Zed", "replace_sid": "mike", "cwd": "/", "backend": "codex",
            "mcp_servers": ["shell"], "fork_session_id": "victim-thread",
            "resume_session_id": "victim-thread", "open_existing": True, "session": "mike",
            "synthesize_audio": False}


def test_a_router_create_carries_only_the_name(effects):
    send, _ = router({"kind": "agent_control", "control_action": "create", "target_session": "",
                      "confidence": 0.95, "control_payload": INJECTED})
    send()
    assert effects["created"] == [{"name": "Zed", "synthesize_audio": True}]


def test_a_router_relaunch_takes_everything_from_the_agents_own_record(effects):
    send, _ = router({"kind": "agent_control", "control_action": "relaunch",
                      "target_session": "antoni", "confidence": 0.95, "control_payload": INJECTED})
    send()
    antoni = agents.get_by_session("antoni")
    assert effects["created"] == [{
        "name": "Antoni", "session": "antoni", "replace_sid": "antoni", "cwd": antoni["cwd"],
        "backend": antoni["backend"], "synthesize_audio": True}]


def test_a_router_fork_only_clarifies_and_never_creates(effects):
    """No fork source is trusted from the router, and a fresh agent where the
    user asked for a fork would be wrong: fork is said in the app."""
    send, _ = router({"kind": "agent_control", "control_action": "fork",
                      "target_session": "antoni", "confidence": 0.99, "control_payload": INJECTED})
    assert send().action == "clarify"
    assert effects["created"] == [] and effects["interrupted"] == []


def test_an_implicit_control_target_still_obeys_packet_scope(effects):
    """A command with no named target acts on the requested session; that
    one must be in the packet too (reviewer B's control on 6e0abe92)."""
    send, _ = router({"kind": "agent_control", "control_action": "stop",
                      "target_session": "", "confidence": .95})
    outcome = send(only_sessions=("antoni",))
    assert effects["interrupted"] == []
    assert outcome.action == "clarify"


def test_the_live_default_configuration_is_unchanged(effects):
    """fallback_only (the default and the live setting): the router runs only
    for a failed delegation, and there a control decision only clarifies,
    as before; a confident fallback route still routes."""
    send, dispatched = router(stop(0.99), fallback_only=True)
    assert send() is None                        # an ordinary send never reaches the router
    assert send(fallback_request=True).action == "clarify"
    assert effects["interrupted"] == [] and effects["created"] == [] and dispatched == []
