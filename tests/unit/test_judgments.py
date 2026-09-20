"""Optional typed judgments: the fallback contract, the breaker, and each site.

No network: `judgments._post` is replaced everywhere, so a test never depends on
the provider being reachable. The point of most of these is the same one
property — when the model cannot answer, the caller behaves exactly as it does
today.
"""
from __future__ import annotations

import pytest

from lib import judgment_sites, judgments, settings_store


@pytest.fixture(autouse=True)
def _reset_breaker():
    judgments.reset_breaker()
    yield
    judgments.reset_breaker()


def _enable(monkeypatch, site: str) -> None:
    monkeypatch.setattr(judgments, "api_key", lambda: "test-key")
    settings_store.set_bool(judgments.KEY_ENABLED, True)
    settings_store.set_bool(f"judgments.{site}", True)


def _answer(monkeypatch, payload: dict) -> list[dict]:
    """Install a fake transport and return the list of requests it received."""
    seen: list[dict] = []

    def fake_post(body, key, seconds):
        seen.append({"body": body, "key": key, "seconds": seconds})
        return payload

    monkeypatch.setattr(judgments, "_post", fake_post)
    return seen


# ---- the fallback contract --------------------------------------------

def test_no_key_means_no_call(monkeypatch):
    monkeypatch.setattr(judgments, "api_key", lambda: "")
    settings_store.set_bool(judgments.KEY_ENABLED, True)
    settings_store.set_bool("judgments.junk", True)
    seen = _answer(monkeypatch, {})
    assert judgments.judge("junk", {"a": 1}, {"q": judgments.noul("x?")}) is None
    assert seen == []


def test_master_switch_off_means_no_call(monkeypatch):
    monkeypatch.setattr(judgments, "api_key", lambda: "test-key")
    settings_store.set_bool(judgments.KEY_ENABLED, False)
    settings_store.set_bool("judgments.junk", True)
    seen = _answer(monkeypatch, {})
    assert judgments.judge("junk", {"a": 1}, {"q": judgments.noul("x?")}) is None
    assert seen == []


def test_site_switch_is_independent(monkeypatch):
    _enable(monkeypatch, "junk")
    _answer(monkeypatch, {"answers": {"q": {"type": "noul", "noul": 0.9}}})
    assert judgments.judge("junk", {}, {"q": judgments.noul("x?")}) is not None
    assert judgments.judge("errors", {}, {"q": judgments.noul("x?")}) is None


def test_unknown_site_is_a_programming_error():
    with pytest.raises(ValueError):
        judgments.site_enabled("nope")


def test_transport_failure_falls_back_and_is_logged(monkeypatch):
    _enable(monkeypatch, "junk")
    monkeypatch.setattr(judgments, "_post",
                        lambda *a, **k: (_ for _ in ()).throw(TimeoutError("slow")))
    assert judgments.judge("junk", {}, {"q": judgments.noul("x?")}) is None
    row = judgments.recent(limit=1, site="junk")[0]
    assert row["fallback_used"] == 1 and "slow" in row["error"]


def test_missing_answer_is_treated_as_failure(monkeypatch):
    _enable(monkeypatch, "junk")
    _answer(monkeypatch, {"answers": {"other": {"type": "noul", "noul": 0.9}}})
    assert judgments.judge("junk", {}, {"q": judgments.noul("x?")}) is None


# ---- breaker ----------------------------------------------------------

def test_breaker_opens_after_repeated_failures(monkeypatch):
    _enable(monkeypatch, "junk")
    calls = {"n": 0}

    def fail(*a, **k):
        calls["n"] += 1
        raise OSError("down")

    monkeypatch.setattr(judgments, "_post", fail)
    for _ in range(judgments.FAILURES_BEFORE_OPEN):
        judgments.judge("junk", {}, {"q": judgments.noul("x?")})
    assert calls["n"] == judgments.FAILURES_BEFORE_OPEN
    # Further calls short-circuit instead of paying the timeout again.
    assert judgments.judge("junk", {}, {"q": judgments.noul("x?")}) is None
    assert calls["n"] == judgments.FAILURES_BEFORE_OPEN


def test_success_resets_the_failure_count(monkeypatch):
    _enable(monkeypatch, "junk")
    state = {"fail": True}

    def flaky(*a, **k):
        if state["fail"]:
            raise OSError("down")
        return {"answers": {"q": {"type": "noul", "noul": 0.1}}}

    monkeypatch.setattr(judgments, "_post", flaky)
    judgments.judge("junk", {}, {"q": judgments.noul("x?")})
    state["fail"] = False
    assert judgments.judge("junk", {}, {"q": judgments.noul("x?")}) is not None
    state["fail"] = True
    judgments.judge("junk", {}, {"q": judgments.noul("x?")})
    assert not judgments._breaker_open()


# ---- answers ----------------------------------------------------------

def test_typed_accessors():
    answers = judgments.Answers({
        "pick": {"type": "choice", "choice": "b", "confidence": 0.8,
                 "probabilities": {"a": 0.2, "b": 0.7, "c": 0.1}},
        "yes": {"type": "noul", "noul": 0.42},
    }, {})
    assert answers.choice("pick") == "b"
    assert answers.probability("pick", "a") == pytest.approx(0.2)
    assert answers.confidence("pick") == pytest.approx(0.8)
    assert answers.noul("yes") == pytest.approx(0.42)
    assert answers.top("pick", 2) == "b 70%, a 20%"
    assert answers.noul("absent") == 0.0


def test_request_carries_model_and_key(monkeypatch):
    _enable(monkeypatch, "junk")
    seen = _answer(monkeypatch, {"answers": {"q": {"type": "noul", "noul": 0.1}}})
    judgments.judge("junk", {"transcript": "hi"}, {"q": judgments.noul("x?")})
    assert seen[0]["key"] == "test-key"
    assert seen[0]["body"]["model"] == judgments.MODEL
    assert seen[0]["body"]["state"] == {"transcript": "hi"}


# ---- settings ---------------------------------------------------------

def test_update_settings_roundtrip():
    status = judgments.update_settings(
        {"enabled": True, "timeout_ms": 900, "sites": {"junk": True}})
    assert status["enabled"] is True
    assert status["timeout_ms"] == 900
    assert status["sites"]["junk"] is True
    assert status["sites"]["router"] is False


@pytest.mark.parametrize("patch", [
    {"enabled": "yes"},
    {"timeout_ms": 10},
    {"timeout_ms": 10**9},
    {"sites": {"nope": True}},
    {"sites": {"junk": "on"}},
    {"sites": []},
])
def test_update_settings_rejects_bad_input(patch):
    with pytest.raises(ValueError):
        judgments.update_settings(patch)


# ---- junk site --------------------------------------------------------

def test_junk_falls_back_to_the_blocklist_when_off():
    # Today's behaviour: a real one-word reply is discarded with the outros.
    assert judgment_sites.is_junk("Thanks for watching.") is True
    assert judgment_sites.is_junk("Okay.") is True
    assert judgment_sites.is_junk("Main.") is False


def test_junk_keeps_speech_the_blocklist_would_delete(monkeypatch):
    _enable(monkeypatch, "junk")
    _answer(monkeypatch, {"answers": {"junk": {"type": "noul", "noul": 0.34}}})
    assert judgment_sites.is_junk("Okay.") is False


def test_junk_needs_more_than_half_certainty_to_discard(monkeypatch):
    _enable(monkeypatch, "junk")
    _answer(monkeypatch, {"answers": {"junk": {"type": "noul", "noul": 0.55}}})
    assert judgment_sites.is_junk("Main.") is False
    _answer(monkeypatch, {"answers": {"junk": {"type": "noul", "noul": 0.85}}})
    assert judgment_sites.is_junk("Like and subscribe.") is True


def test_junk_context_is_sent_when_known(monkeypatch):
    _enable(monkeypatch, "junk")
    seen = _answer(monkeypatch, {"answers": {"junk": {"type": "noul", "noul": 0.1}}})
    judgment_sites.is_junk("Okay.", agent_last_message="Should I merge?")
    assert seen[0]["body"]["state"]["agent_last_message"] == "Should I merge?"


# ---- error site -------------------------------------------------------

def test_error_escalation_only_touches_unknown(monkeypatch):
    _enable(monkeypatch, "errors")
    seen = _answer(monkeypatch, {"answers": {
        "kind": {"type": "choice", "choice": "usage_limit", "confidence": 0.9,
                 "probabilities": {"usage_limit": 0.9}}}})
    assert judgment_sites.classify_error_or_unknown("whatever", "timeout") == "timeout"
    assert seen == []


def test_error_escalation_names_an_unmatched_failure(monkeypatch):
    _enable(monkeypatch, "errors")
    _answer(monkeypatch, {"answers": {
        "kind": {"type": "choice", "choice": "usage_limit", "confidence": 0.9,
                 "probabilities": {"usage_limit": 0.9}}}})
    assert judgment_sites.classify_error_or_unknown(
        "Weekly allowance used up", "unknown") == "usage_limit"


def test_usage_limit_needs_high_confidence(monkeypatch):
    # A false usage_limit can switch accounts on its own.
    _enable(monkeypatch, "errors")
    _answer(monkeypatch, {"answers": {
        "kind": {"type": "choice", "choice": "usage_limit", "confidence": 0.6,
                 "probabilities": {"usage_limit": 0.6}}}})
    assert judgment_sites.classify_error_or_unknown("odd text", "unknown") == "unknown"


def test_error_stays_unknown_when_off():
    assert judgment_sites.classify_error_or_unknown("odd text", "unknown") == "unknown"


# ---- naming site ------------------------------------------------------

AGENTS = {"s-rachel": {"name": "Rachel"}, "s-bella": {"name": "Bella"}}


def test_naming_falls_through_when_off():
    from lib import routing
    assert routing.resolve_agent_by_spoken_name(
        "Rachel, check the branch", AGENTS) == ("s-rachel", "check the branch")


def test_naming_can_reject_a_mention(monkeypatch):
    _enable(monkeypatch, "naming")
    _answer(monkeypatch, {"answers": {
        "to": {"type": "choice", "choice": "nobody", "confidence": 0.9,
               "probabilities": {"nobody": 0.9, "Rachel": 0.1}}}})
    from lib import routing
    session, text = routing.resolve_agent_by_spoken_name(
        "Did Rachel finish the migration?", AGENTS)
    assert session is None and text == "Did Rachel finish the migration?"


def test_naming_strips_the_address_prefix(monkeypatch):
    _enable(monkeypatch, "naming")
    _answer(monkeypatch, {"answers": {
        "to": {"type": "choice", "choice": "Bella", "confidence": 0.9,
               "probabilities": {"Bella": 0.95, "nobody": 0.05}}}})
    from lib import routing
    assert routing.resolve_agent_by_spoken_name(
        "Bella run the tests", AGENTS) == ("s-bella", "run the tests")


def test_naming_low_confidence_keeps_the_caller_default(monkeypatch):
    _enable(monkeypatch, "naming")
    _answer(monkeypatch, {"answers": {
        "to": {"type": "choice", "choice": "Bella", "confidence": 0.5,
               "probabilities": {"Bella": 0.5, "Rachel": 0.5}}}})
    from lib import routing
    session, _ = routing.resolve_agent_by_spoken_name("something vague", AGENTS)
    assert session is None


# ---- router provider --------------------------------------------------

PACKET = {
    "utterance": "ship the patch",
    "trace_id": "t-1",
    "agents": [
        {"session": "clarp-ios", "persona": "Rachel", "recent_user_messages": [],
         "recent_agent_messages": [{"text": "CarPlay crash fixed, run the UI tests?"}]},
        {"session": "clarp-server", "persona": "Bella", "recent_user_messages": [],
         "recent_agent_messages": [{"text": "Patch is ready for review."}]},
    ],
}


def _router_reply(kind, kind_conf, target, target_probabilities):
    return {"answers": {
        "kind": {"type": "choice", "choice": kind, "confidence": kind_conf,
                 "probabilities": {kind: kind_conf}},
        "target": {"type": "choice", "choice": target, "confidence": 0.9,
                   "probabilities": target_probabilities},
    }}


def test_router_provider_is_registered():
    from lib import orchestrator
    assert orchestrator.normalize_provider("jev") == orchestrator.TYPESAFE_PROVIDER
    assert orchestrator.is_routing_provider("typesafe")
    assert orchestrator.TYPESAFE_PROVIDER in {
        row["id"] for row in orchestrator.provider_options()}


def test_router_delivers_to_the_agent_the_content_fits(monkeypatch):
    from lib import orchestrator
    _enable(monkeypatch, "router")
    _answer(monkeypatch, _router_reply(
        "agent_message", 0.9, "clarp-server", {"clarp-server": 0.94, "clarp-ios": 0.05}))
    decision = orchestrator.parse_decision(
        orchestrator._call_typesafe(PACKET, orchestrator.get_settings(), "prompt"))
    assert decision.kind == "agent_message"
    assert decision.target_session == "clarp-server"
    assert decision.addressing is True


def test_router_ignores_speech_that_fits_no_agent(monkeypatch):
    from lib import orchestrator
    _enable(monkeypatch, "router")
    _answer(monkeypatch, _router_reply(
        "agent_message", 0.7, "none", {"none": 0.88, "clarp-ios": 0.1}))
    decision = orchestrator.parse_decision(
        orchestrator._call_typesafe(PACKET, orchestrator.get_settings(), "prompt"))
    assert decision.kind == "ignored"
    assert decision.target_session == ""


def test_router_asks_when_no_agent_is_clear(monkeypatch):
    from lib import orchestrator
    _enable(monkeypatch, "router")
    # No OpenAI key, so the clarify escalation cannot run and the typed verdict
    # stands on its own.
    monkeypatch.setattr(orchestrator.config, "load",
                        lambda: type("C", (), {"openai_key": lambda self: "",
                                               "typesafe_key": lambda self: "k"})())
    _answer(monkeypatch, _router_reply(
        "agent_message", 0.8, "clarp-ios", {"clarp-ios": 0.52, "clarp-server": 0.48}))
    decision = orchestrator.parse_decision(
        orchestrator._call_typesafe(PACKET, orchestrator.get_settings(), "prompt"))
    assert decision.kind == "ambiguous"


def test_router_falls_back_to_openai_when_unavailable(monkeypatch):
    from lib import orchestrator
    # Site switched off: the provider must not silently stop routing.
    called = {"openai": False}

    def fake_openai(prompt, settings):
        called["openai"] = True
        return {"kind": "agent_message", "target_session": "clarp-ios",
                "confidence": 0.9, "addressing": True}

    monkeypatch.setattr(orchestrator, "_call_openai", fake_openai)
    monkeypatch.setattr(orchestrator.config, "load",
                        lambda: type("C", (), {"openai_key": lambda self: "k",
                                               "typesafe_key": lambda self: ""})())
    orchestrator._call_typesafe(PACKET, orchestrator.get_settings(), "prompt")
    assert called["openai"] is True


def test_router_state_carries_only_what_the_questions_reference():
    from lib import orchestrator
    state = orchestrator._typesafe_state(PACKET)
    assert state["user_said"] == "ship the patch"
    assert {a["session"] for a in state["agents"]} == {"clarp-ios", "clarp-server"}
    assert "trace_id" not in state


# ---- diagnostics ----------------------------------------------------------

def test_every_row_keeps_the_question_and_the_judged_state(monkeypatch):
    _enable(monkeypatch, "junk")
    _answer(monkeypatch, {"answers": {"junk": {"type": "noul", "noul": 0.08}},
                          "usage": {"input_tokens": 400, "output_tokens": 20}})
    question = judgments.noul("Is this message junk?", yes="filler", no="real")
    judgments.judge("junk", {"text": "Sure, on it."}, {"junk": question}, trace_id="t-1")
    row = judgments.recent(limit=1, site="junk")[0]
    import json
    assert json.loads(row["question_json"]) == {"junk": question}
    assert json.loads(row["state_json"]) == {"text": "Sure, on it."}
    assert json.loads(row["answers_json"]) == {"junk": {"type": "noul", "noul": 0.08}}
    assert row["trace_id"] == "t-1" and row["fallback_used"] == 0


def test_failed_calls_are_logged_with_their_input_too(monkeypatch):
    _enable(monkeypatch, "junk")
    monkeypatch.setattr(judgments, "_post", lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    judgments.judge("junk", {"text": "hello"}, {"junk": judgments.noul("x?")})
    row = judgments.recent(limit=1, site="junk")[0]
    assert row["fallback_used"] == 1 and "down" in row["error"]
    assert '"hello"' in row["state_json"] and '"x?"' in row["question_json"]


def test_billing_failure_holds_the_breaker_for_an_hour(monkeypatch):
    _enable(monkeypatch, "junk")

    class _Response:
        status_code = 402
        text = "insufficient credits"

    class _Billing(Exception):
        response = _Response()

    calls = {"n": 0}

    def broke(*a, **k):
        calls["n"] += 1
        raise _Billing("402")

    monkeypatch.setattr(judgments, "_post", broke)
    assert judgments.judge("junk", {}, {"q": judgments.noul("x?")}) is None
    assert judgments._breaker_open()
    with judgments._BREAKER_LOCK:
        remaining = judgments._open_until - judgments.time.monotonic()
    assert remaining > judgments.OPEN_SECONDS * 2
    # One call, not three, was enough; the next call never reaches the wire.
    assert judgments.judge("junk", {}, {"q": judgments.noul("x?")}) is None
    assert calls["n"] == 1
    assert judgments.recent(limit=1, site="junk")[0]["error"] == "breaker open"
