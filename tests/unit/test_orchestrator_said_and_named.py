"""Whether the router's answer is said, and which session it names (issue 360, R83 and R84).

R84: a voice request is answered aloud and an app's request (a failed name delegation) never is,
whichever sentence it would be: one rule over every fixed sentence, so a new one cannot become an
exception. R83: the decision row, its eventlog row, the orchestrator-decision event and the answer
carry the session a command's name resolved to, or "" when it resolved to none; the name as the
model said it stays in its own answer (raw_response_json). A command that did nothing answers the
session spoken to."""
from __future__ import annotations

import json

import pytest

from lib import agent_lifecycle, agents, db, events, eventlog, orchestrator
from lib.orchestrator_phrases import PHRASES
from tests.unit.test_orchestrator_held_and_unknown import (  # noqa: F401
    effects, held, no_processes, router)


def _command(action, target="", confidence=0.95, **payload):
    decision = {"kind": "agent_control", "control_action": action, "target_session": target,
                "confidence": confidence, "reason": "command"}
    if payload:
        decision["control_payload"] = payload
    return decision


def _archive_antoni(_monkeypatch):
    db.conn().execute("UPDATE agents SET archived_at = 1 WHERE session = 'antoni'")


def _two_sams(_monkeypatch):
    for session in ("sam-a", "sam-b"):
        agents.create_agent(persona="Sam", voice_id="", cwd="/tmp", session=session)


def _lifecycle_refuses(monkeypatch):
    def refuse(self, payload):
        raise agent_lifecycle.AgentLifecycleError(409, "name_in_use")
    monkeypatch.setattr(agent_lifecycle.AgentLifecycleService, "create", refuse)


def _held_then_expired(say):
    """A message held on voice (an app request holds nothing), expired before the yes."""
    say("send the report")
    db.conn().execute("UPDATE orchestrator_pending_utterances SET expires_at = 1")


# key -> (the model's decision, a setup, a voice step before the request). Every key of the
# fixed-sentence table has a row: test_every_fixed_sentence_has_a_case fails for a new one.
CASES = {
    "clarify_target": ({"kind": "clarify", "target_session": "", "confidence": 0.9,
                        "spoken_text": PHRASES["clarify_target"]}, None, None),
    "ambiguous_target": ({"kind": "clarify", "target_session": "", "confidence": 0.9}, None, None),
    "repeat_missing": ({"kind": "control", "control_action": "repeat", "target_session": "",
                        "confidence": 0.95}, None, None),
    "control_unknown": ({"kind": "control", "control_action": "dance", "target_session": "",
                         "confidence": 0.95}, None, None),
    "agent_control_unknown": (_command("dance"), None, None),
    "no_such_agent": (_command("stop", "ZED"), None, None),
    "held_message_gone": ("held", None, _held_then_expired),
    "cannot_check": ({"kind": "status_query", "target_session": "antoni", "confidence": 0.95},
                     _archive_antoni, None),
    "cannot_command": (_command("stop", "ANTONI"), _archive_antoni, None),
    "which_agent": (_command("stop", "Sam"), _two_sams, None),
    "unsure_command": (_command("stop", "antoni", confidence=0.4), None, None),
    "no_voice_commands": (_command("stop", "clarp-message-delegator"), None, None),
    "fork_in_app": (_command("fork", "antoni"), None, None),
    "name_taken": (_command("create", name="Antoni"), None, None),
    "command_refused": (_command("restart", "antoni"), _lifecycle_refuses, None),
    "no_valid_target": ({"kind": "agent_message", "target_session": "zed", "confidence": 0.95},
                        None, None),
}
# Sentences outside the table, answered the same way.
FREE = {
    "a status": ({"kind": "status_query", "target_session": "antoni", "confidence": 0.95,
                  "status_text": "Antoni is idle."}, "Antoni is idle."),
    "who it is": ({"kind": "control", "control_action": "who_am_i_talking_to", "target_session": "",
                   "confidence": 0.95}, "You are talking to Mike."),
    "an unsure message": ({"kind": "agent_message", "target_session": "antoni", "confidence": 0.4},
                          "Was that for Antoni?"),
}


def test_every_fixed_sentence_has_a_case():
    assert set(CASES) == set(PHRASES)


def _model(decision, step):
    def model(packet):
        if decision == "held":
            if packet["utterance"] == "send the report":
                return {"kind": "agent_message", "target_session": "antoni", "confidence": 0.4,
                        "reason": "unsure"}
            pending = [row["pending_id"] for row in held()]
            return {"kind": "agent_message", "target_session": "antoni", "pending_id": pending[0],
                    "confidence": 0.95, "reason": "confirmed"}
        return {"reason": "case", **decision}
    return model


@pytest.mark.parametrize("app", [False, True], ids=["voice", "app"])
@pytest.mark.parametrize("key", sorted(PHRASES))
def test_a_fixed_sentence_is_said_to_voice_and_never_to_an_app(effects, monkeypatch, key, app):
    decision, setup, step = CASES[key]
    say, dispatched = router(_model(decision, step), effects)
    if setup:
        setup(monkeypatch)
    if step:
        step(say)
    effects["spoken"].clear()
    outcome = say("the request", fallback_request=app)
    assert dispatched == []
    assert effects["spoken"] == ([] if app else [PHRASES[key]])
    if app:
        assert outcome.action == "clarify"


@pytest.mark.parametrize("app", [False, True], ids=["voice", "app"])
@pytest.mark.parametrize("case", sorted(FREE))
def test_a_sentence_outside_the_table_follows_the_same_rule(effects, case, app):
    decision, sentence = FREE[case]
    say, _ = router(lambda packet: {"reason": "case", **decision}, effects)
    say("the request", fallback_request=app)
    assert effects["spoken"] == ([] if app else [sentence])


def test_an_app_asking_about_a_message_no_longer_held_is_not_told_aloud(effects):
    """The one exception before R84: HELD_MESSAGE_GONE was said when an app asked."""
    say, dispatched = router(_model("held", None), effects)
    _held_then_expired(say)
    effects["spoken"].clear()
    outcome = say("yes", fallback_request=True)
    assert effects["spoken"] == [] and dispatched == []
    assert outcome.action == "clarify" and outcome.decision["phrase_key"] == "held_message_gone"


# ---- R83 ----

@pytest.fixture
def recorded(monkeypatch):
    seen = {"events": [], "log": []}
    real = events.orchestrator_decision
    monkeypatch.setattr(events, "orchestrator_decision",
                        lambda **kw: seen["events"].append(kw) or real(**kw))
    real_emit = eventlog.emit
    monkeypatch.setattr(eventlog, "emit", lambda source, kind, **kw: (
        seen["log"].append(kw) if (source, kind) == ("orchestrator", "decision") else None)
        or real_emit(source, kind, **kw))
    return seen


def _row():
    return dict(db.conn().execute(
        "SELECT target_session, raw_response_json FROM orchestrator_decisions"
        " ORDER BY decision_id DESC LIMIT 1").fetchone())


# what the model said -> (setup, the session decided on, the answer's session, its action)
NAMED = {
    "a name nobody has": (_command("stop", "ZED"), None, "", "mike", "clarify"),
    "two of that name": (_command("stop", "SAM"), _two_sams, "", "mike", "clarify"),
    "names that disagree": (_command("restart", "ANTONI", name="Mike"), None, "", "mike", "clarify"),
    "unsure": (_command("stop", "ANTONI", confidence=0.4), None, "", "mike", "clarify"),
    "fork": (_command("fork", "ANTONI"), None, "", "mike", "clarify"),
    "an archived agent": (_command("stop", "ANTONI"), _archive_antoni, "antoni", "mike", "clarify"),
    "the lifecycle refuses": (_command("restart", "ANTONI"), _lifecycle_refuses, "antoni", "mike",
                              "clarify"),
    "carried out": (_command("stop", "ANTONI"), None, "antoni", "antoni", "control"),
    "a create carried out": (_command("create", "NOVA", name="Nova"), None, "new", "new", "control"),
    "a control carried out": ({"kind": "control", "control_action": "repeat",
                               "target_session": "ANTONI", "confidence": 0.95},
                              None, "antoni", "antoni", "control"),
}


@pytest.mark.parametrize("case", sorted(NAMED))
def test_a_commands_row_event_and_answer_carry_the_resolved_session_or_none(
        effects, monkeypatch, recorded, case):
    decision, setup, decided, answered, action = NAMED[case]
    say, _ = router(lambda packet: {"reason": "case", **decision}, effects)
    if setup:
        setup(monkeypatch)
    outcome = say("the command")
    assert outcome.action == action
    row = _row()
    assert row["target_session"] == decided
    assert recorded["events"][-1]["target_session"] == decided
    assert recorded["log"][-1]["detail"].get("target_session", "<absent>") == decided
    assert outcome.decision["target_session"] == decided
    assert outcome.session == answered
    # The name as said is kept in the model's own answer only.
    assert json.loads(row["raw_response_json"])["target_session"] == decision["target_session"]


def test_an_apps_command_that_does_nothing_names_the_session_asked_about_and_resolves_the_name(
        effects, recorded):
    say, _ = router(lambda packet: _command("stop", "ANTONI"), effects)
    outcome = say("stop Antoni", fallback_request=True)
    assert outcome.action == "clarify" and outcome.session == "mike"
    assert _row()["target_session"] == "antoni" == outcome.decision["target_session"]


def test_a_refused_command_and_a_held_question_answer_the_same_shape(effects, recorded):
    """A refusal answers as a held clarify already does when nothing was delivered: the session
    spoken to, nothing dispatched, the same decision fields."""
    answers = {"stop Zed": _command("stop", "ZED"),
               "tell Zed hello": {"kind": "agent_message", "target_session": "zed", "confidence": 0.95}}
    say, dispatched = router(lambda packet: {"reason": "case", **answers[packet["utterance"]]}, effects)
    refused, asked = say("stop Zed"), say("tell Zed hello")
    assert dispatched == [] and len(held()) == 1
    for outcome in (refused, asked):
        assert (outcome.action, outcome.session, outcome.dispatch) == ("clarify", "mike", "")
    assert set(refused.decision) == set(asked.decision)
