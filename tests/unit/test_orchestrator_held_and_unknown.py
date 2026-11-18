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

    def say(text, *, fallback_request=False, only_sessions=None):
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
            prompt_admission=admission, hands_free=True, synthesize_audio=False, dispatch=dispatch,
            fallback_request=fallback_request, only_sessions=only_sessions)
    return say, dispatched


KEPT = ("pending_id", "utterance", "trace_id", "prompt_admission_json", "created_at", "requested_session")


def held():
    return [dict(row) for row in db.conn().execute(
        "SELECT pending_id, utterance, trace_id, prompt_admission_json, status, candidate_session, reason,"
        " created_at, expires_at, requested_session FROM orchestrator_pending_utterances")]


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
    assert {k: again[0][k] for k in KEPT} == {k: first[0][k] for k in KEPT}
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
    assert effects["spoken"] == [orchestrator.NO_SUCH_AGENT]
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
    assert set(rows) == {"send the report", "and the invoice"}
    assert rows["send the report"]["status"] == "sent" and rows["send the report"]["pending_id"] == sent_id[0]
    assert rows["and the invoice"]["status"] == "pending" and rows["and the invoice"]["pending_id"] != sent_id[0]
    assert [d["text"] for d in dispatched] == ["send the report"]


def test_renewing_one_of_two_held_messages_leaves_the_other_untouched(effects):
    def model(packet):
        held_now = {row["utterance"]: row["pending_id"] for row in packet["pending"]}
        if packet["utterance"] in ("send the report", "book the room"):
            return {"kind": "agent_message", "target_session": "antoni", "confidence": 0.4, "reason": "unsure"}
        return {"kind": "clarify", "target_session": "mike", "pending_id": held_now["send the report"],
                "confidence": 0.9, "reason": "the report, again"}
    say, dispatched = router(model, effects)
    say("send the report")
    say("book the room")
    before = {row["utterance"]: row for row in held()}
    assert say("which one?").action == "clarify"
    after = {row["utterance"]: row for row in held()}
    assert set(after) == {"send the report", "book the room"}
    assert after["book the room"] == before["book the room"]
    assert after["send the report"]["reason"] == "the report, again"
    assert after["send the report"]["candidate_session"] == "mike"
    assert dispatched == []


def test_the_clarifying_answer_is_neither_sent_nor_held_unless_the_model_holds_it_as_new(effects):
    """'what?' answers the question: it renews the held message and is itself kept nowhere. Held as
    new (the model names no held id) it is a message of its own, still not sent."""
    renew = [True]

    def model(packet):
        if packet["utterance"] == "send the report":
            return {"kind": "agent_message", "target_session": "antoni", "confidence": 0.4, "reason": "unsure"}
        ident = packet["pending"][0]["pending_id"] if renew[0] else ""
        return {"kind": "clarify", "target_session": "antoni", "pending_id": ident, "confidence": 0.9,
                "reason": "again"}
    say, dispatched = router(model, effects)
    say("send the report")
    say("what?")
    assert [row["utterance"] for row in held()] == ["send the report"]
    renew[0] = False
    say("what?")
    assert sorted(row["utterance"] for row in held()) == ["send the report", "what?"]
    assert dispatched == []


def test_a_held_message_that_expired_before_the_yes_is_not_sent_and_the_user_is_told(effects, monkeypatch):
    def model(packet):
        if packet["utterance"] == "send the report":
            return {"kind": "agent_message", "target_session": "antoni", "confidence": 0.4, "reason": "unsure"}
        return {"kind": "agent_message", "target_session": "antoni", "pending_id": ident[0],
                "confidence": 0.95, "reason": "confirmed"}
    ident = []
    say, dispatched = router(model, effects)
    say("send the report")
    ident.append(held()[0]["pending_id"])
    db.conn().execute("UPDATE orchestrator_pending_utterances SET expires_at = 1")
    effects["spoken"].clear()
    outcome = say("yes")
    assert dispatched == []
    assert outcome.action == "clarify"
    assert effects["spoken"] == [orchestrator.HELD_MESSAGE_GONE]
    assert held()[0]["status"] == "pending" and held()[0]["utterance"] == "send the report"


def test_an_expired_held_message_is_not_brought_back_and_an_invented_id_is_not_used(effects):
    """Asked about after it expired, the held row stays as it was; what the user said is held
    under a fresh id. An id the model made up never becomes a row's id."""
    named = ["made-up-id"]

    def model(packet):
        if packet["utterance"] == "send the report":
            return {"kind": "agent_message", "target_session": "antoni", "confidence": 0.4, "reason": "unsure"}
        return {"kind": "clarify", "target_session": "antoni", "pending_id": named[0], "confidence": 0.9,
                "reason": "again"}
    say, dispatched = router(model, effects)
    say("what about it?")
    assert [row["pending_id"] for row in held()] != ["made-up-id"]
    db.conn().execute("DELETE FROM orchestrator_pending_utterances")
    say("send the report")
    old = held()[0]
    db.conn().execute("UPDATE orchestrator_pending_utterances SET expires_at = 1")
    named[0] = old["pending_id"]
    say("and the invoice")
    rows = {row["utterance"]: row for row in held()}
    assert set(rows) == {"send the report", "and the invoice"}
    assert rows["send the report"]["expires_at"] == 1 and rows["send the report"]["status"] == "pending"
    assert rows["and the invoice"]["pending_id"] not in (old["pending_id"], "made-up-id")
    assert dispatched == []


def test_a_failed_delegation_asking_to_restart_nobody_only_clarifies_as_before(effects):
    """In a failed delegation the router's one authority is to pick a recipient: a command there
    clarifies without a word, as on main; it neither creates nor answers about agents."""
    say, _ = router(lambda packet: {
        "kind": "agent_control", "control_action": "restart", "target_session": "zed",
        "confidence": 0.95, "reason": "restart zed"}, effects)
    assert say("restart Zed", fallback_request=True).action == "clarify"
    assert effects["created"] == [] and effects["spoken"] == [] and held() == []


@pytest.mark.parametrize("told", ["", "Zed is idle."])
def test_a_status_query_about_an_agent_nobody_has_is_not_answered_for_another(effects, told):
    """R34: "what is Zed doing?" with no Zed is answered that there is no such agent, not with the
    status of the agent the message was said to, nor with a status the model made up."""
    say, _ = router(lambda packet: {
        "kind": "status_query", "target_session": "zed", "confidence": 0.95, "status_text": told,
        "reason": "status of zed"}, effects)
    assert say("what is Zed doing?").action == "clarify"
    assert effects["spoken"] == [orchestrator.NO_SUCH_AGENT]


@pytest.mark.parametrize("why", ["archived", "outside the call", "not voice-targetable"])
def test_a_status_query_about_an_agent_out_of_reach_says_so_and_not_that_it_does_not_exist(
        effects, monkeypatch, why):
    say, _ = router(lambda packet: {
        "kind": "status_query", "target_session": "antoni", "confidence": 0.95,
        "status_text": "Antoni is idle.", "reason": "status of antoni"}, effects)
    only = None
    if why == "archived":
        db.conn().execute("UPDATE agents SET archived_at = 1 WHERE session = 'antoni'")
    elif why == "outside the call":
        only = ("mike",)
    else:
        capabilities = agents.interaction_capabilities
        monkeypatch.setattr(agents, "interaction_capabilities", lambda agent: {
            **capabilities(agent), "can_voice_target": agent["session"] != "antoni"})
    assert say("what is Antoni doing?", only_sessions=only).action == "clarify"
    assert effects["spoken"] == [orchestrator.CANNOT_CHECK]


def test_a_status_query_naming_nobody_is_still_about_the_agent_spoken_to(effects):
    say, _ = router(lambda packet: {
        "kind": "status_query", "target_session": "", "confidence": 0.95, "reason": "status"}, effects)
    assert say("what are you doing?").action == "status"
    assert effects["spoken"] and effects["spoken"][0].startswith("Mike")


def test_a_status_query_about_an_agent_it_can_answer_for_is_unchanged(effects):
    say, _ = router(lambda packet: {
        "kind": "status_query", "target_session": "antoni", "confidence": 0.95, "reason": "status"}, effects)
    assert say("what is Antoni doing?").action == "status"
    assert effects["spoken"] and effects["spoken"][0].startswith("Antoni")
