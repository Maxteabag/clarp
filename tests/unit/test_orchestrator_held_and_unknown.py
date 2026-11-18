"""The router keeps what the user said and invents nothing (issue 360, R32 and R33).

R32: a held message keeps its own words, id and admission when the user is asked about it
again; a renewed question renews only its expiry, reason and candidate.
R33: a voice command to start again an agent nobody has makes no agent; the user is told so."""
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
    seen = {"created": [], "spoken": []}

    class Lifecycle:
        def __init__(self, _ctx):
            pass

        def create(self, payload):
            seen["created"].append(dict(payload))
            return SimpleNamespace(session=payload.get("session") or "new")
    monkeypatch.setattr(agent_lifecycle, "AgentLifecycleService", Lifecycle)
    return seen


def router(model, effects):
    """A router over two agents whose model is ``model(packet)``; ``say(text)`` sends one utterance."""
    for session in ("mike", "antoni"):
        agents.create_agent(persona=session.title(), voice_id="", cwd="/tmp", session=session)
    builtins().ensure_builtins(initial={ROLE: {
        "enabled": True, "backend": "codex", "provider": "codex",
        "model": "gpt-5.3-codex-spark", "effort": "", "options": {"fallback_only": False}}})
    dispatched = []
    context = SimpleNamespace(
        default_session="mike", stream=SimpleNamespace(broadcast=lambda _e: None),
        speak_announcement=lambda text, *args, **kwargs: effects["spoken"].append(text))
    service = orchestrator.OrchestratorService(context, model_call=lambda packet, *_: dict(model(packet)))
    count = [0]

    def say(text):
        count[0] += 1
        admission = prompt_admissions.create(
            authenticated_at_admission=True, origin="user", sender_agent_id="", channel="voice",
            observed_at=db.now_ms(), client_admission_id=f"request-{count[0]}",
            trace_id=f"route-{count[0]}", original_text=text)

        def dispatch(**kwargs):
            dispatched.append(kwargs)
            return SimpleNamespace(session=kwargs["forced_session"], backend="claude")
        return service.handle_send(
            text=text, requested_session="mike", trace_id=f"route-{count[0]}",
            prompt_admission=admission, hands_free=True, synthesize_audio=False, dispatch=dispatch)
    return say, dispatched


def held():
    return [dict(row) for row in db.conn().execute(
        "SELECT pending_id, utterance, trace_id, prompt_admission_json, status, candidate_session, reason"
        " FROM orchestrator_pending_utterances")]


def test_a_held_message_keeps_its_words_when_its_user_is_asked_a_second_time(effects):
    """"send the report" is held with a question; "what?" is asked about again under the same
    id; "yes" sends it. Antoni receives "send the report", not "what?"."""
    def model(packet):
        pending = packet["pending"]
        if not pending:          # the first utterance: unsure it is for Antoni, so held
            return {"kind": "agent_message", "target_session": "antoni", "confidence": 0.4,
                    "reason": "unsure"}
        ident = pending[0]["pending_id"]
        if packet["utterance"] == "what?":
            return {"kind": "clarify", "target_session": "antoni", "pending_id": ident,
                    "confidence": 0.9, "reason": "asked again", "spoken_text": "The report, for Antoni?"}
        return {"kind": "agent_message", "target_session": "antoni", "pending_id": ident,
                "confidence": 0.95, "reason": "confirmed"}
    say, dispatched = router(model, effects)
    assert say("send the report").action == "clarify"
    first = held()
    assert [row["utterance"] for row in first] == ["send the report"]
    assert say("what?").action == "clarify"
    again = held()
    assert len(again) == 1
    assert {k: again[0][k] for k in ("pending_id", "utterance", "trace_id", "prompt_admission_json")} == {
        k: first[0][k] for k in ("pending_id", "utterance", "trace_id", "prompt_admission_json")}
    assert again[0]["reason"] == "asked again"
    assert say("yes").action == "route"
    assert [d["text"] for d in dispatched] == ["send the report"]
    assert dispatched[0]["client_msg_id"] == "request-1"
    assert held()[0]["status"] == "sent"


@pytest.mark.parametrize("action", ["relaunch", "restart", "resume"])
def test_starting_again_an_agent_nobody_has_makes_no_agent(effects, action):
    say, _ = router(lambda packet: {
        "kind": "agent_control", "control_action": action, "target_session": "zed",
        "confidence": 0.95, "control_payload": {"name": "Zed"}, "reason": "start zed again"}, effects)
    outcome = say("restart Zed")
    assert effects["created"] == []
    assert outcome.action == "clarify"
    assert effects["spoken"] == ["I do not have an agent by that name."]
    assert held() == []       # nothing to clarify, so nothing is held


def test_starting_again_an_agent_it_has_still_starts_it_again(effects):
    say, _ = router(lambda packet: {
        "kind": "agent_control", "control_action": "restart", "target_session": "antoni",
        "confidence": 0.95, "reason": "restart antoni"}, effects)
    assert say("restart Antoni").action == "control"
    assert [c["replace_sid"] for c in effects["created"]] == ["antoni"]


def test_create_still_makes_a_new_agent_by_name(effects):
    say, _ = router(lambda packet: {
        "kind": "agent_control", "control_action": "create", "target_session": "",
        "confidence": 0.95, "control_payload": {"name": "Zed"}, "reason": "new agent"}, effects)
    assert say("make an agent called Zed").action == "control"
    assert effects["created"] == [{"name": "Zed", "synthesize_audio": True}]


def test_a_question_naming_a_message_already_sent_holds_the_new_words_apart(effects):
    """The model names the id of a held message that was sent already: that one stays sent, and
    what the user just said is held under an id of its own (it is not lost either)."""
    sent_id = []

    def model(packet):
        pending = packet["pending"]
        if packet["utterance"] == "send the report":
            return {"kind": "agent_message", "target_session": "antoni", "confidence": 0.4, "reason": "unsure"}
        if packet["utterance"] == "yes":
            sent_id.append(pending[0]["pending_id"])
            return {"kind": "agent_message", "target_session": "antoni", "pending_id": sent_id[0],
                    "confidence": 0.95, "reason": "confirmed"}
        return {"kind": "clarify", "target_session": "antoni", "pending_id": sent_id[0],
                "confidence": 0.9, "reason": "which one"}
    say, dispatched = router(model, effects)
    say("send the report")
    say("yes")
    assert say("and the invoice").action == "clarify"
    rows = {row["utterance"]: row for row in held()}
    assert rows["send the report"]["status"] == "sent" and rows["send the report"]["pending_id"] == sent_id[0]
    assert rows["and the invoice"]["status"] == "pending" and rows["and the invoice"]["pending_id"] != sent_id[0]
    assert [d["text"] for d in dispatched] == ["send the report"]
