"""Whether the router's answer is said, and which session it names (issue 360, R83 and R84).

R84: a voice request is answered aloud and an app's request (a failed name delegation) never is,
whichever sentence it would be: one rule over every fixed sentence, so a new one cannot become an
exception. R83: the decision row, its eventlog row, the orchestrator-decision event and the answer
carry the session a command's name resolved to, or "" when it resolved to none; the name as the
model said it stays in its own answer (raw_response_json). A command that did nothing answers the
session spoken to."""
from __future__ import annotations

import json
from types import SimpleNamespace

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
    "unsure, naming a real agent": (_command("stop", "ANTONI", confidence=0.4), None, "antoni", "mike",
                                    "clarify"),
    "unsure, naming nobody": (_command("stop", "ZED", confidence=0.4), None, "", "mike", "clarify"),
    "fork of a real agent": (_command("fork", "ANTONI"), None, "antoni", "mike", "clarify"),
    "fork naming nobody": (_command("fork", "ZED"), None, "", "mike", "clarify"),
    "unsure, the name only in the payload": (_command("stop", "", confidence=0.4, name="Antoni"), None,
                                             "antoni", "mike", "clarify"),
    "unsure, names that disagree": (_command("stop", "ANTONI", confidence=0.4, name="Mike"), None, "",
                                    "mike", "clarify"),
    "fork, the name only in the payload": (_command("fork", "", name="Antoni"), None, "antoni", "mike",
                                           "clarify"),
    "fork, names that disagree": (_command("fork", "ANTONI", name="Mike"), None, "", "mike", "clarify"),
    "an archived agent": (_command("stop", "ANTONI"), _archive_antoni, "antoni", "mike", "clarify"),
    "an agent taking no voice commands": (_command("stop", "clarp-message-delegator"), None,
                                          "clarp-message-delegator", "mike", "clarify"),
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


# ---- R83, ruled for every clarify (00:47): the answer names the session spoken to; the row, the
# event and the decision's suggestion name the session the decision's target resolves to, or "" ----

def _message(target, confidence=0.95):
    return {"kind": "agent_message", "target_session": target, "confidence": confidence}


CLARIFIES = {
    "an unsure message": (_message("antoni", 0.4), None, "antoni"),
    "a message to nobody": (_message("zed"), None, ""),
    "a message to one out of reach": (_message("antoni"), _archive_antoni, "antoni"),
    "the model's question with a name as said": ({"kind": "clarify", "target_session": "ANTONI",
                                                  "confidence": 0.9}, None, "antoni"),
    "the model's question naming nobody": ({"kind": "clarify", "target_session": "ZED",
                                            "confidence": 0.9}, None, ""),
    "a status about nobody": ({"kind": "status_query", "target_session": "zed", "confidence": 0.95},
                              None, ""),
    "a status about two of a name": ({"kind": "status_query", "target_session": "Sam",
                                      "confidence": 0.95}, _two_sams, ""),
    "a status out of reach": ({"kind": "status_query", "target_session": "antoni", "confidence": 0.95},
                              _archive_antoni, "antoni"),
}


@pytest.mark.parametrize("case", sorted(CLARIFIES))
def test_every_clarify_answers_the_session_spoken_to_and_names_what_it_resolved_to(
        effects, monkeypatch, recorded, case):
    decision, setup, resolved = CLARIFIES[case]
    say, dispatched = router(lambda packet: {"reason": "case", **decision}, effects)
    if setup:
        setup(monkeypatch)
    outcome = say("the request")
    assert outcome.action == "clarify" and dispatched == []
    assert outcome.session == "mike"
    assert _row()["target_session"] == resolved
    assert recorded["events"][-1]["target_session"] == resolved
    assert recorded["log"][-1]["detail"].get("target_session", "<absent>") == resolved
    assert outcome.decision["target_session"] == resolved


def test_a_message_no_longer_held_answers_the_session_spoken_to(effects, recorded):
    say, dispatched = router(_model("held", None), effects)
    _held_then_expired(say)
    outcome = say("yes")
    assert outcome.action == "clarify" and dispatched == []
    assert outcome.session == "mike"
    assert _row()["target_session"] == "antoni" == outcome.decision["target_session"]


# ---- R83, rulings of 01:28: a command that did nothing answers the session spoken to whatever its
# final action; the run's receipt names the resolved target; a replay answers as the request did ----

def _receipt():
    row = db.conn().execute(
        "SELECT result_json FROM janitor_demand_results ORDER BY created_at DESC, rowid DESC LIMIT 1").fetchone()
    return json.loads(row["result_json"]) if row else {}


def _agent_id(session):
    return agents.get_by_session(session)["agent_id"] if session else ""


def test_a_create_that_fails_after_it_was_accepted_answers_the_session_spoken_to(effects, monkeypatch, recorded):
    def broken(self, payload):
        raise RuntimeError("the lifecycle could not start it")
    monkeypatch.setattr(agent_lifecycle.AgentLifecycleService, "create", broken)
    say, _ = router(lambda packet: _command("create", "NOVA", name="Nova"), effects)
    outcome = say("make a new agent called Nova")
    assert outcome.action == "error" and outcome.status == 500
    assert outcome.session == "mike"
    assert _row()["target_session"] == "" == recorded["events"][-1]["target_session"]
    assert _receipt().get("target_agent_id") == ""


def test_a_clarifys_receipt_names_its_resolved_target_and_its_replay_answers_as_it_did(effects):
    from lib import prompt_admissions
    from tests.unit.test_orchestrator_janitor import ROLE, builtins
    for session in ("mike", "antoni"):
        agents.create_agent(persona=session.title(), voice_id="", cwd="/tmp", session=session)
    builtins().ensure_builtins(initial={ROLE: {
        "enabled": True, "backend": "codex", "provider": "codex",
        "model": "gpt-5.3-codex-spark", "effort": "", "options": {"fallback_only": False}}})
    asked = []
    service = orchestrator.OrchestratorService(
        SimpleNamespace(default_session="mike", stream=SimpleNamespace(broadcast=lambda _e: None),
                        speak_announcement=lambda *a, **k: None),
        model_call=lambda packet, *_: asked.append(1) or {"reason": "unsure", **_message("antoni", 0.4)})
    admission = prompt_admissions.create(
        authenticated_at_admission=True, origin="user", sender_agent_id="", channel="voice",
        observed_at=db.now_ms(), client_admission_id="request-replayed", trace_id="route-r",
        original_text="send the report")

    def send():
        return service.handle_send(text="send the report", requested_session="mike", trace_id="route-r",
                                   prompt_admission=admission, hands_free=True, synthesize_audio=False,
                                   dispatch=lambda **kw: pytest.fail("nothing is dispatched"))
    first = send()
    assert first.action == "clarify" and first.session == "mike"
    assert _receipt().get("target_agent_id") == _agent_id("antoni")     # the resolved target
    calls = len(asked)
    again = send()                                                       # answered from the run
    assert len(asked) == calls
    assert again.session == "mike"


# ---- the hold of 02:43: a replay answers what the first answer said, for every outcome kind ----

def _broken_create(monkeypatch):
    def broken(self, payload):
        raise RuntimeError("the lifecycle could not start it")
    monkeypatch.setattr(agent_lifecycle.AgentLifecycleService, "create", broken)


def _dispatch_fails(status):
    from lib.turn_dispatch import DispatchError

    def dispatch(**kwargs):
        raise DispatchError(status, "dispatch failed")
    return dispatch


def _model_fails(packet):
    raise RuntimeError("the routing model failed")


# outcome kind -> (the model's decision or a model, a setup, how dispatch answers)
OUTCOMES = {
    "route": (_message("antoni"), None, None),
    "control": ({"kind": "control", "control_action": "repeat", "target_session": "ANTONI",
                 "confidence": 0.95}, None, None),
    "a command carried out": (_command("stop", "ANTONI"), None, None),
    "status": ({"kind": "status_query", "target_session": "antoni", "confidence": 0.95}, None, None),
    "clarify": (_message("antoni", 0.4), None, None),
    "the model's own clarify": ({"kind": "clarify", "target_session": "ANTONI", "confidence": 0.9}, None, None),
    "refused: a name nobody has": (_command("stop", "ZED"), None, None),
    "refused: two of that name": (_command("stop", "SAM"), _two_sams, None),
    "refused: an archived agent": (_command("stop", "ANTONI"), _archive_antoni, None),
    "refused: no voice commands": (_command("stop", "clarp-message-delegator"), None, None),
    "refused by the lifecycle": (_command("restart", "ANTONI"), _lifecycle_refuses, None),
    "a routed dispatch fails 4xx": (_message("antoni"), None, _dispatch_fails(413)),
    "a routed dispatch fails 5xx": (_message("antoni"), None, _dispatch_fails(503)),
    "an error after a command was accepted": (_command("create", "NOVA", name="Nova"), _broken_create, None),
    "the model fails": (_model_fails, None, None),
}


def _service(model, effects):
    from tests.unit.test_orchestrator_janitor import ROLE, builtins
    for session in ("mike", "antoni"):
        agents.create_agent(persona=session.title(), voice_id="", cwd="/tmp", session=session)
    builtins().ensure_builtins(initial={ROLE: {
        "enabled": True, "backend": "codex", "provider": "codex",
        "model": "gpt-5.3-codex-spark", "effort": "", "options": {"fallback_only": False}}})
    return orchestrator.OrchestratorService(
        SimpleNamespace(default_session="mike", stream=SimpleNamespace(broadcast=lambda _e: None),
                        speak_announcement=lambda text, *a, **k: effects["spoken"].append(text)),
        model_call=lambda packet, *_: model(packet) if callable(model) else {"reason": "case", **model})


def _sender(service, *, app, dispatch=None, client="request-replayed", requested="mike"):
    from lib import prompt_admissions
    admission = prompt_admissions.create(
        authenticated_at_admission=True, origin="user", sender_agent_id="", channel="voice",
        observed_at=db.now_ms(), client_admission_id=client, trace_id="route-r",
        original_text="the request")

    def delivered(**kwargs):
        return SimpleNamespace(session=kwargs["forced_session"], backend="claude")

    def send():
        return service.handle_send(text="the request", requested_session=requested, trace_id="route-r",
                                   prompt_admission=admission, hands_free=True, synthesize_audio=False,
                                   dispatch=dispatch or delivered, fallback_request=app)
    return send


def _said(outcome):
    return (outcome.status, outcome.action, outcome.session, outcome.dispatch)


@pytest.mark.parametrize("app", [False, True], ids=["voice", "app"])
@pytest.mark.parametrize("kind", sorted(OUTCOMES))
def test_a_replay_answers_what_the_first_answer_said(effects, monkeypatch, kind, app):
    model, setup, dispatch = OUTCOMES[kind]
    service = _service(model, effects)
    if setup:
        setup(monkeypatch)
    send = _sender(service, app=app, dispatch=dispatch)
    first = send()
    assert _said(send()) == _said(first)
    assert _said(send()) == _said(first)
    if kind == "a routed dispatch fails 4xx":
        assert _said(first) == (413, "error", "antoni", "")         # the routed target, as main


def test_a_replay_while_the_request_runs_is_pending_and_after_it_the_first_answer(effects):
    service = _service({}, effects)
    seen = {}

    def model(packet):
        seen["during"] = _said(seen["send"]())        # the same request again while its run is held
        return {"reason": "case", **_message("antoni")}
    service.model_call = lambda packet, *_: model(packet)
    seen["send"] = _sender(service, app=False)
    first = seen["send"]()
    assert seen["during"] == (202, "pending", "mike", "")
    assert first.action == "route" and _said(seen["send"]()) == _said(first)


def test_a_receipt_written_before_the_answer_was_kept_replays_as_main_did(effects):
    service = _service(_message("antoni"), effects)
    send = _sender(service, app=False)
    first = send()
    row = db.conn().execute("SELECT rowid, result_json FROM janitor_demand_results"
                            " ORDER BY created_at DESC, rowid DESC LIMIT 1").fetchone()
    older = {key: value for key, value in json.loads(row["result_json"]).items()
             if not key.startswith("answer_")}
    db.conn().execute("UPDATE janitor_demand_results SET result_json=? WHERE rowid=?",
                      (json.dumps(older), row["rowid"]))
    assert _said(send()) == _said(first) == (200, "route", "antoni", "claude")


# ---- the hold of 05:09: the receipt never costs the answer ----

def _stored_receipt():
    row = db.conn().execute("SELECT rowid, result_json FROM janitor_demand_results"
                            " ORDER BY created_at DESC, rowid DESC LIMIT 1").fetchone()
    return row["rowid"], json.loads(row["result_json"])


@pytest.mark.parametrize("app", [False, True], ids=["voice", "app"])
@pytest.mark.parametrize("length", [500, 501, 600])
def test_a_requested_session_past_the_receipts_bound_is_still_answered_and_replays_alike(
        effects, length, app):
    """A clarify answers the requested session; one of 501 or 600 characters no longer fits the
    demand history's bound (500). Its status and dispatch are stored and its session is left out,
    which means 'the first answer named the requested session': every retry of the client id
    answers it again. First = second = third at every length, as on main (no answer at all and 202
    pending at ba9c34a0; the replay named the resolved target at 12ba2078)."""
    service = _service(_message("antoni", 0.4), effects)
    send = _sender(service, app=app, requested="s" * length)
    first = send()
    assert _said(first) == (200, "clarify", "s" * length, "")
    assert _said(send()) == _said(first) and _said(send()) == _said(first)
    _rowid, receipt = _stored_receipt()
    assert receipt["answer_status"] == 200 and receipt["answer_dispatch"] == ""
    assert ("answer_session" in receipt) is (length <= 500)


def test_a_failed_completion_is_logged_and_the_answer_still_sent_and_the_run_closed(effects, monkeypatch, capfd):
    service = _service(_message("antoni"), effects)
    real = orchestrator.janitor_builtins.complete_run
    calls = []

    def refuses_first(run_id, *args, **kwargs):
        calls.append(kwargs.get("result"))
        if len(calls) == 1:
            raise orchestrator.janitors.JanitorError("Demand history accepts bounded metadata only")
        return real(run_id, *args, **kwargs)
    monkeypatch.setattr(orchestrator.janitor_builtins, "complete_run", refuses_first)
    send = _sender(service, app=False)
    first = send()
    assert _said(first) == (200, "route", "antoni", "claude")
    assert calls[1] == {"status": "route", "target_agent_id": _agent_id("antoni")}     # its least receipt
    assert "orchestratorReceiptFail" in capfd.readouterr().err
    assert _said(send()) == (200, "route", "antoni", "claude")      # not pending: the run is closed


# an odd stored receipt -> it replays as main: the recipient, its backend, the recomputed status
ODD = {
    "answer_status a word": {"answer_status": "x"},
    "answer_status null": {"answer_status": None},
    "answer_status missing": {"answer_status": "<drop>"},
    "answer_status true": {"answer_status": True},
    "answer_session null": {"answer_session": None},
    "answer_session empty": {"answer_session": ""},
    "answer_dispatch a number": {"answer_dispatch": 7},
}


@pytest.mark.parametrize("case", sorted(ODD))
def test_an_odd_stored_receipt_replays_as_main_never_an_error(effects, case):
    service = _service(_message("antoni"), effects)
    send = _sender(service, app=False)
    first = send()
    rowid, receipt = _stored_receipt()
    for key, value in ODD[case].items():
        if value == "<drop>":
            receipt.pop(key, None)
        else:
            receipt[key] = value
    db.conn().execute("UPDATE janitor_demand_results SET result_json=? WHERE rowid=?", (json.dumps(receipt), rowid))
    expected = (200, "route", "antoni", "" if case == "answer_dispatch a number" else "claude")
    assert _said(send()) == expected and _said(first) == (200, "route", "antoni", "claude")


def test_a_replay_answers_the_stored_status_not_one_recomputed(effects):
    """The receipt's answer_status is what a replay answers, even where the rest would give another."""
    service = _service(_message("antoni"), effects)
    send = _sender(service, app=False)
    first = send()
    rowid, receipt = _stored_receipt()
    assert receipt["answer_status"] == first.status == 200          # the first answer's, stored
    receipt["answer_status"] = 299
    db.conn().execute("UPDATE janitor_demand_results SET result_json=? WHERE rowid=?", (json.dumps(receipt), rowid))
    assert _said(send()) == (299, "route", "antoni", "claude")


def test_a_status_without_a_session_replays_the_requested_session(effects):
    """A receipt with the first answer's status but no session stored: the first answer named the
    requested session (too long for the bound), so the replay answers the request's own session."""
    service = _service(_message("antoni"), effects)
    send = _sender(service, app=False)
    send()
    rowid, receipt = _stored_receipt()
    receipt.pop("answer_session")
    db.conn().execute("UPDATE janitor_demand_results SET result_json=? WHERE rowid=?", (json.dumps(receipt), rowid))
    assert _said(send()) == (200, "route", "mike", "claude")
