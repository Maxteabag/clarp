"""The router is an ephemeral Janitor job with bounded durable receipts."""
from __future__ import annotations

import importlib
import json
from types import SimpleNamespace

import pytest

from lib import agents, backends, db, janitors, orchestrator, prompt_admissions, settings_store

ROLE = "message-delegator"


def builtins():
    return importlib.import_module("lib.janitor_builtins")


@pytest.fixture(autouse=True)
def no_processes(monkeypatch):
    monkeypatch.setattr(backends, "active_handles", lambda *args: [])


def setup_router(model_call, *, enabled=True, options=None):
    for session in ("mike", "antoni"):
        agents.create_agent(persona=session.title(), voice_id="", cwd="/tmp", session=session)
    configured = builtins().ensure_builtins(initial={ROLE: {
        "enabled": enabled, "backend": "codex", "provider": "codex",
        "model": "gpt-5.3-codex-spark", "effort": "",
        "options": {"fallback_only": False, **(options or {})},
    }})[ROLE]
    events, speech, dispatched = [], [], []
    context = SimpleNamespace(
        default_session="mike", stream=SimpleNamespace(broadcast=events.append),
        speak_announcement=lambda *args, **kwargs: speech.append((args, kwargs)),
    )
    service = orchestrator.OrchestratorService(context, model_call=model_call)

    def send(*, trace_id="route-1", request_id="request-1", fallback_request=False, hands_free=True):
        admission = prompt_admissions.create(
            authenticated_at_admission=True, origin="user", sender_agent_id="",
            channel="voice", observed_at=db.now_ms(), client_admission_id=request_id,
            trace_id=trace_id, original_text="private user utterance",
        )
        def dispatch(**kwargs):
            dispatched.append(kwargs)
            return SimpleNamespace(session=kwargs["forced_session"], backend="claude")
        return service.handle_send(
            text=admission.original_text, requested_session="mike", trace_id=trace_id,
            prompt_admission=admission, hands_free=hands_free, synthesize_audio=False,
            dispatch=dispatch, fallback_request=fallback_request,
        )
    return configured, send, dispatched, events, speech


def route():
    return {"kind": "agent_message", "target_session": "mike", "confidence": .99,
            "addressing": True, "reason": "private user utterance"}


def test_router_context_excludes_janitors_by_shared_capability_policy():
    agents.create_agent(persona="Mike", voice_id="", cwd="/tmp", session="mike")
    agents.create_agent(persona="Maintenance", voice_id="", cwd="/tmp", session="maintenance")
    janitors.create("maintenance")
    packet = orchestrator.build_context_packet(
        utterance="maintenance", requested_session="mike", trace_id="context",
        hands_free=True, settings=orchestrator.get_settings(),
    )
    assert [a["session"] for a in packet["agents"]] == ["mike"]
    assert packet["candidate_name_matches"] == []


def test_settings_read_does_not_install_agents_or_change_database():
    assert orchestrator.get_settings().enabled is False
    assert db.conn().execute("SELECT COUNT(*) FROM agents").fetchone()[0] == 0


def test_janitor_identity_owns_model_and_pause_over_stale_legacy_settings():
    configured, send, dispatched, _, _ = setup_router(lambda *_: route())
    settings_store.set_text("orchestrator.model", "stale-legacy-model")
    settings_store.set_bool("orchestrator.enabled", True)
    changed = janitors.configure(configured["session"], configured["revision"], model="updated-model", effort="low")
    settings = orchestrator.get_settings()
    assert settings.model == "updated-model" and settings.effort == "low"
    assert settings.enabled is False and changed["enabled"] is False
    assert send() is None and not dispatched


def test_legacy_settings_update_changes_same_entity_without_second_model_store():
    configured, _, _, _, _ = setup_router(lambda *_: route())
    changed = orchestrator.update_settings({"model": "updated-model", "effort": "low", "enabled": True})
    identity = builtins().get_builtin(ROLE)
    assert changed.model == identity["model"] == "updated-model"
    assert changed.effort == identity["effort"] == "low"
    assert changed.enabled and identity["enabled"]
    assert identity["agent_id"] == configured["agent_id"]
    assert settings_store.get_text("orchestrator.model", default="absent") == "absent"
    janitors.set_enabled(identity["session"], identity["revision"], False)
    assert not orchestrator.get_settings().enabled


def test_provider_compatibility_updates_janitor_executor_and_backend():
    setup_router(lambda *_: route())
    changed = orchestrator.update_settings({"provider": "antigravity"})
    identity = builtins().get_builtin(ROLE)
    assert changed.provider == "agy" == identity["execution"]["provider"]
    assert identity["backend"] == "agy"


def test_model_call_has_frozen_running_janitor_and_bounded_history():
    observed = []
    def model(packet, settings):
        owner = builtins().get_builtin(ROLE)
        run = janitors.list_runs(owner["session"])[0]
        assert run["status"] == "running"
        assert settings.provider == run["configuration"]["provider"] == "codex"
        assert settings.model == run["configuration"]["model"] == "gpt-5.3-codex-spark"
        assert set(a["session"] for a in packet["agents"]) == {"mike", "antoni"}
        observed.append(run)
        return route()
    configured, send, dispatched, _, _ = setup_router(model)
    result = send()
    assert result.action == "route" and len(dispatched) == 1 and len(observed) == 1
    finished = janitors.list_runs(configured["session"])[0]
    assert finished["outcome"] == "completed" and finished["status"] == "completed"
    assert "private user utterance" not in json.dumps(finished)
    assert finished["demand_result"]["status"] == "route"
    assert not db.conn().execute("SELECT 1 FROM queued_turns").fetchone()


@pytest.mark.parametrize("kind", ["agent_message", "control", "agent_control", "clarify"])
def test_pause_during_model_blocks_routing_and_control_side_effects(kind):
    def model(*_):
        owner = builtins().get_builtin(ROLE)
        janitors.set_enabled(owner["session"], owner["revision"], False)
        return {**route(), "kind": kind, "control_action": "switch", "target_session": "antoni", "spoken_text": "A question"}
    configured, send, dispatched, events, speech = setup_router(model)
    result = send()
    assert result.action == "fallback"
    assert not dispatched and not speech and not agents.get_focus()
    assert not [e for e in events if e.get("type") == "agent-focus"]
    assert janitors.list_runs(configured["session"])[0]["outcome"] == "cancelled"


def test_model_change_during_first_pass_prevents_second_paid_context_scan():
    calls = []
    def model(*_):
        calls.append(True)
        owner = builtins().get_builtin(ROLE)
        agents.update_agent(owner["agent_id"], model="changed-directly")
        return {"kind": "ambiguous", "confidence": .1}
    _, send, dispatched, _, speech = setup_router(model)
    assert send().action == "fallback"
    assert calls == [True] and not dispatched and not speech


def test_busy_janitor_falls_back_without_model_or_routing():
    configured, send, dispatched, _, _ = setup_router(lambda *_: pytest.fail("paid call while busy"))
    builtins().begin_run(ROLE, "other-request")
    assert send().action == "fallback"
    assert not dispatched and len(janitors.list_runs(configured["session"])) == 1


def test_retry_client_request_id_never_repeats_model_or_effect():
    calls = []
    configured, send, dispatched, _, _ = setup_router(lambda *_: calls.append(True) or route())
    first = send(trace_id="first-trace")
    retried = send(trace_id="retry-trace")
    assert first.action == retried.action == "route"
    assert first.session == retried.session == "mike"
    assert len(calls) == len(dispatched) == 1
    assert len(janitors.list_runs(configured["session"])) == 1


def test_duplicate_running_request_returns_pending_without_second_model_call():
    calls, retries = [], []
    def model(*_):
        calls.append(True)
        retries.append(send(trace_id="overlapping-trace"))
        return route()
    _, send, dispatched, _, _ = setup_router(model)
    assert send().action == "route"
    assert retries[0].action == "pending" and retries[0].status == 202
    assert len(calls) == len(dispatched) == 1


def test_model_failure_records_only_bounded_error_and_uses_existing_fallback():
    def model(*_):
        raise RuntimeError("private user utterance and provider response")
    configured, send, dispatched, _, _ = setup_router(model)
    assert send().action == "fallback" and not dispatched
    run = janitors.list_runs(configured["session"])[0]
    assert run["outcome"] == "failed"
    assert "private user utterance" not in json.dumps(run)


def test_retry_after_pause_replays_control_receipt_without_becoming_chat():
    calls = []
    configured, send, dispatched, _, speech = setup_router(
        lambda *_: calls.append(True) or {"kind": "control", "control_action": "current_agent"})
    assert send().action == "control"
    owner = janitors.get(configured["session"])
    janitors.set_enabled(owner["session"], owner["revision"], False)
    assert send(trace_id="after-pause").action == "control"
    assert len(calls) == len(speech) == 1 and not dispatched


def test_retry_changed_payload_is_rejected_even_when_janitor_paused():
    configured, send, _, _, _ = setup_router(lambda *_: route())
    assert send().action == "route"
    owner = janitors.get(configured["session"])
    janitors.set_enabled(owner["session"], owner["revision"], False)
    # Same request ID, different fallback contract must not become a new send.
    retry = send(trace_id="changed-payload", fallback_request=True)
    assert retry.action == "error" and retry.status == 409


def test_replacement_janitor_receives_scoped_demand_with_its_own_model():
    calls = []
    _, send, dispatched, _, _ = setup_router(
        lambda _, settings: calls.append(settings.model) or route(), enabled=False)
    agents.create_agent(persona="Scoped", voice_id="", cwd="/tmp", session="scoped", backend="codex", model="custom-model")
    configured = janitors.create("scoped", template_id=ROLE,
        scope={"agent_ids": [agents.get_by_session("mike")["agent_id"]]}, options={"fallback_only": False})
    janitors.set_enabled("scoped", configured["revision"], True)
    assert send().action == "route" and len(dispatched) == 1
    assert calls == ["custom-model"]


def test_provider_output_cannot_control_a_janitor_identity():
    _, send, dispatched, _, speech = setup_router(lambda *_: {
        "kind": "agent_control", "control_action": "switch",
        "target_session": builtins().get_builtin(ROLE)["session"], "confidence": .99})
    assert send().action in {"clarify", "fallback"}
    assert not agents.get_focus() and not dispatched


def test_retry_of_orphaned_claim_after_deadline_returns_terminal_cancellation(monkeypatch):
    calls = []
    def interrupted_model(*_):
        calls.append(True)
        raise KeyboardInterrupt("Simulate Host stopping after admission")
    configured, send, dispatched, _, speech = setup_router(interrupted_model)
    with pytest.raises(KeyboardInterrupt):
        send()
    run = janitors.list_runs(configured["session"])[0]
    assert run["status"] == "running"
    monkeypatch.setattr(db, "now_ms", lambda: run["configuration"]["expires_at"] + 1)

    retry = send(trace_id="after-deadline")

    assert retry.action == "cancelled" and retry.status == 409 and not retry.ok
    assert "expired" in retry.error.lower()
    retired = janitors.get_run(run["run_id"])
    assert retired["status"] == retired["outcome"] == "cancelled"
    assert retired["finished_at"] is not None
    assert send(trace_id="another-retry").action == "cancelled"
    assert calls == [True] and not dispatched and not speech
    assert len(janitors.list_runs(configured["session"])) == 1


POLICY = {"fallback_only": True, "hands_free_only": False, "confidence_threshold": .91,
          "timeout_ms": 1250, "voice_id": "chosen-routing-voice"}


def test_all_routing_settings_read_the_janitor_options_over_stale_legacy_values(monkeypatch):
    setup_router(lambda *_: route(), options=POLICY)
    settings_store.set_text("orchestrator.confidence_threshold", ".5")
    settings_store.set_text("orchestrator.timeout_ms", "60000")
    settings_store.set_text("orchestrator.voice_id", "stale-legacy-voice")
    settings = orchestrator.get_settings()
    assert {key: getattr(settings, key) for key in POLICY} == POLICY
    monkeypatch.setattr(orchestrator, "get_legacy_settings", lambda: pytest.fail("read legacy after migration"))
    assert orchestrator.get_settings() == settings


def test_legacy_policy_update_patches_same_options_and_never_writes_legacy_keys():
    owner, _, _, _, _ = setup_router(lambda *_: route())
    before = {key: settings_store.get_text(f"orchestrator.{key}", default="absent") for key in POLICY}
    settings = orchestrator.update_settings(POLICY)
    changed = builtins().get_builtin(ROLE)
    assert changed["agent_id"] == owner["agent_id"] and changed["generation"] > owner["generation"]
    assert changed["options"] == POLICY
    assert {key: getattr(settings, key) for key in POLICY} == POLICY
    assert settings.enabled and changed["enabled"]
    assert before == {key: settings_store.get_text(f"orchestrator.{key}", default="absent") for key in POLICY}


def test_legacy_policy_update_rejects_unknown_or_invalid_options_atomically():
    owner, _, _, _, _ = setup_router(lambda *_: route())
    for data in ({"made_up_setting": True}, {"confidence_threshold": float("nan")},
                 {"fallback_only": "false"}, {"timeout_ms": 100000}):
        with pytest.raises(ValueError):
            orchestrator.update_settings(data)
        assert builtins().get_builtin(ROLE) == owner


def test_scoped_subscriber_owns_eligibility_and_confidence_before_model_admission():
    observed = []
    def model(packet, settings):
        observed.append((packet, settings))
        return {**route(), "confidence": .6}
    _, send, dispatched, _, speech = setup_router(model, enabled=False,
        options={"hands_free_only": True, "fallback_only": True, "confidence_threshold": .99})
    agents.create_agent(persona="Scoped", voice_id="", cwd="/tmp", session="scoped", backend="codex", model="custom-model")
    options = {**POLICY, "fallback_only": False, "confidence_threshold": .55}
    owner = janitors.create("scoped", template_id=ROLE,
        scope={"agent_ids": [agents.get_by_session("mike")["agent_id"]]}, options=options)
    janitors.set_enabled("scoped", owner["revision"], True)
    result = send(hands_free=False)
    assert result.action == "route" and len(dispatched) == 1 and not speech
    frozen = janitors.list_runs("scoped")[0]["configuration"]["options"]
    assert frozen == options
    assert {key: getattr(observed[0][1], key) for key in options} == frozen
    assert {key: observed[0][0]["settings"][key] for key in options} == frozen


def test_scoped_subscriber_can_require_fallback_even_if_builtin_policy_does_not():
    _, send, dispatched, _, _ = setup_router(lambda *_: pytest.fail("policy should prevent admission"), enabled=False)
    agents.create_agent(persona="Scoped", voice_id="", cwd="/tmp", session="scoped", backend="codex")
    owner = janitors.create("scoped", template_id=ROLE,
        scope={"agent_ids": [agents.get_by_session("mike")["agent_id"]]}, options={"fallback_only": True})
    janitors.set_enabled("scoped", owner["revision"], True)
    assert send() is None and not dispatched and janitors.list_runs("scoped") == []


def test_routing_control_uses_selected_janitor_voice_option():
    _, send, _, _, speech = setup_router(lambda *_: {"kind": "control", "control_action": "current_agent"},
                                         options={"voice_id": "chosen-routing-voice"})
    assert send().action == "control"
    assert speech[0][0][1] == "chosen-routing-voice"


def test_legacy_policy_edit_fences_already_admitted_output():
    def model(*_):
        orchestrator.update_settings({"confidence_threshold": .95})
        return route()
    owner, send, dispatched, _, speech = setup_router(model)
    assert send().action == "fallback" and not dispatched and not speech
    assert builtins().get_builtin(ROLE)["generation"] > owner["generation"]
    assert janitors.list_runs(owner["session"])[0]["outcome"] == "cancelled"


def test_execution_uses_policy_frozen_at_admission_instead_of_earlier_read(monkeypatch):
    observed = []
    _, send, dispatched, _, _ = setup_router(lambda _, settings: observed.append(settings) or {**route(), "confidence": .6})
    original = builtins().begin_run
    def changed_before_admission(*args, **kwargs):
        owner = builtins().get_builtin(ROLE)
        changed = janitors.configure(owner["session"], owner["revision"], options={"confidence_threshold": .55, "timeout_ms": 1250})
        janitors.set_enabled(changed["session"], changed["revision"], True)
        return original(*args, **kwargs)
    monkeypatch.setattr(builtins(), "begin_run", changed_before_admission)
    assert send().action == "route" and len(dispatched) == 1
    assert observed[0].confidence_threshold == .55 and observed[0].timeout_ms == 1250


def test_policy_frozen_at_admission_can_decline_previously_eligible_request(monkeypatch):
    owner, send, dispatched, _, _ = setup_router(lambda *_: pytest.fail("new policy forbids model call"))
    original = builtins().begin_run
    def changed_before_admission(*args, **kwargs):
        changed = janitors.configure(owner["session"], owner["revision"], options={"fallback_only": True})
        janitors.set_enabled(changed["session"], changed["revision"], True)
        return original(*args, **kwargs)
    monkeypatch.setattr(builtins(), "begin_run", changed_before_admission)
    assert send().action == "fallback" and not dispatched
    run = janitors.list_runs(owner["session"])[0]
    assert run["status"] == "cancelled" and run["configuration"]["options"]["fallback_only"]
