"""Typed job options belong to Janitor configuration and its execution fence."""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from lib import agents, backends, db, janitor_builtins, janitors


@pytest.fixture(autouse=True)
def no_processes(monkeypatch):
    monkeypatch.setattr(backends, "active_handles", lambda *args: [])


def seed(role="message-delegator", **values):
    return janitor_builtins.ensure_builtins(initial={role: values})[role]


def test_templates_describe_all_job_options_with_typed_defaults():
    templates = {item["id"]: item for item in janitors.templates()}
    router = {item["key"]: item for item in templates["message-delegator"]["options"]}
    assert set(router) == {"fallback_only", "hands_free_only", "confidence_threshold", "timeout_ms", "voice_id"}
    assert router["fallback_only"]["type"] == "boolean" and router["fallback_only"]["default"] is True
    assert router["hands_free_only"]["default"] is True
    assert router["confidence_threshold"]["default"] == .78
    assert router["confidence_threshold"]["min"] == .5 and router["confidence_threshold"]["max"] == .99
    assert router["timeout_ms"]["type"] == "integer" and router["timeout_ms"]["default"] == 30000
    assert router["timeout_ms"]["min"] == 250 and router["timeout_ms"]["max"] == 60000
    assert router["voice_id"]["default"] == "79f8b5fb-2cc8-479a-80df-29f7a7cf1a3e"
    detail = templates["tool-explainer"]["options"][0]
    assert detail["key"] == "detail_level" and detail["type"] == "choice" and detail["default"] == 0
    assert detail["choices"] == [{"value": value, "label": label} for value, label in enumerate(
        ["Developer", "Technical", "Balanced", "Plain English", "Grandma"])]
    assert templates["task-labels"]["options"] == []


def test_unconfigured_options_resolve_defaults_without_claiming_device_preference():
    config = seed("tool-explainer")
    assert config["options"] == {"detail_level": 0}
    assert config["configured_option_keys"] == []
    assert db.conn().execute("SELECT options_json FROM janitor_configs WHERE agent_id=?", (config["agent_id"],)).fetchone()[0] == "{}"


def test_legacy_seed_imports_options_once_and_preserves_blank_effort():
    config = seed(enabled=True, effort="", options={"fallback_only": False, "hands_free_only": False,
        "confidence_threshold": .91, "timeout_ms": 60000, "voice_id": "existing-voice"})
    assert config["enabled"] and config["effort"] == ""
    assert config["options"] == {"fallback_only": False, "hands_free_only": False,
        "confidence_threshold": .91, "timeout_ms": 60000, "voice_id": "existing-voice"}
    assert config["configured_option_keys"] == sorted(config["options"])
    assert seed(options={"timeout_ms": 250}) == config


def test_configuration_patches_options_pauses_and_fences_frozen_run():
    config = seed(enabled=True, options={"timeout_ms": 55000})
    run = janitor_builtins.begin_run("message-delegator", "old-options")
    assert run["configuration"]["options"] == config["options"]
    new = janitors.configure(config["session"], config["revision"], options={"fallback_only": False})
    assert not new["enabled"] and new["generation"] == config["generation"] + 1
    assert new["options"]["timeout_ms"] == 55000 and new["options"]["fallback_only"] is False
    assert new["configured_option_keys"] == ["fallback_only", "timeout_ms"]
    assert not janitor_builtins.is_current(run["run_id"])
    assert not janitor_builtins.complete_run(run["run_id"])


@pytest.mark.parametrize("values", [
    {"fallback_only": 1}, {"hands_free_only": "false"}, {"timeout_ms": True}, {"timeout_ms": 250.5},
    {"timeout_ms": 249}, {"timeout_ms": 60001}, {"confidence_threshold": False},
    {"confidence_threshold": .49}, {"confidence_threshold": 1}, {"confidence_threshold": float("nan")},
    {"confidence_threshold": float("inf")}, {"voice_id": 42}, {"voice_id": "x" * 161},
    {"voice_id": "bad\nvoice"}, {"enabled": True}, {"future_option": 1}, None, []])
def test_invalid_options_do_not_pause_or_mutate_configuration(values):
    config = seed(enabled=True)
    with pytest.raises(janitors.JanitorError, match="option"):
        janitors.configure(config["session"], config["revision"], options=values)
    assert janitors.get(config["session"]) == config


@pytest.mark.parametrize("value", [-1, 5, 1.5, True, "2", None])
def test_detail_level_accepts_only_the_declared_integer_choices(value):
    config = seed("tool-explainer")
    with pytest.raises(janitors.JanitorError, match="option"):
        janitors.configure(config["session"], config["revision"], options={"detail_level": value})
    assert janitors.get(config["session"]) == config


def test_future_stored_keys_survive_patches_and_unchanged_roundtrip_only():
    config = seed(enabled=True)
    c = db.conn()
    c.execute("UPDATE janitor_configs SET options_json=? WHERE agent_id=?",
        (json.dumps({"future_option": {"mode": "future"}, "timeout_ms": 5000}), config["agent_id"]))
    config = janitors.get(config["session"])
    current = janitors.configure(config["session"], config["revision"], options={"fallback_only": False})
    assert current["options"]["future_option"] == {"mode": "future"}
    current = janitors.configure(config["session"], current["revision"], options=current["options"])
    with pytest.raises(janitors.JanitorError, match="option"):
        janitors.configure(config["session"], current["revision"], options={"future_option": {"mode": "changed"}})


def test_direct_option_change_cannot_accept_old_provider_output():
    config = seed(enabled=True)
    run = janitor_builtins.begin_run("message-delegator", "frozen-options")
    db.conn().execute("UPDATE janitor_configs SET options_json=? WHERE agent_id=?", ('{"timeout_ms":250}', config["agent_id"]))
    assert not janitor_builtins.is_current(run["run_id"])
    assert not janitor_builtins.complete_run(run["run_id"])


def test_managed_label_runs_also_freeze_and_revalidate_job_options():
    from lib.janitor_context import build_context_from_connection
    for session in ("labels", "worker"):
        aid = agents.create_agent(persona=session.title(), voice_id="", cwd="/tmp", session=session, backend="codex")
        agents.record_state(aid, "done")
    config = janitors.create("labels")
    config = janitors.set_enabled("labels", config["revision"], True)
    context = build_context_from_connection(db.conn(), "worker")
    run = janitors.create_run(config["attachments"][0]["attachment_id"], config["generation"], [context])
    assert run["configuration"]["options"] == {}
    assert janitors.validate_dispatch("labels", run["run_id"], run["trace_id"])
    db.conn().execute("UPDATE janitor_configs SET options_json=? WHERE agent_id=?", ('{"future_setting":true}', config["agent_id"]))
    assert not janitors.validate_dispatch("labels", run["run_id"], run["trace_id"])


def test_template_change_discards_options_from_the_previous_job():
    aid = agents.create_agent(persona="Custom", voice_id="", cwd="/tmp", session="custom", backend="codex")
    config = janitors.create("custom", template_id="message-delegator")
    config = janitors.configure("custom", config["revision"], options={"timeout_ms": 250})
    new = janitors.configure("custom", config["revision"], template_id="tool-explainer")
    assert new["agent_id"] == aid and new["options"] == {"detail_level": 0}
    assert new["configured_option_keys"] == []


@pytest.mark.parametrize("enabled", [True, False])
def test_adopt_legacy_detail_first_write_wins_and_preserves_enabled(enabled):
    config = seed("tool-explainer", enabled=enabled)
    first = janitors.adopt_options(config["session"], config["revision"], {"detail_level": 3})
    assert first["options"]["detail_level"] == 3 and first["enabled"] == enabled
    assert first["revision"] == config["revision"] + 1 and first["generation"] == config["generation"] + 1
    second = janitors.adopt_options(config["session"], config["revision"], {"detail_level": 1})
    assert second == first


def test_adoption_fences_old_runs_and_unset_stale_revision_is_rejected():
    config = seed("tool-explainer")
    run = janitor_builtins.begin_run("tool-explainer", "before-adopt")
    with pytest.raises(janitors.JanitorError, match="changed"):
        janitors.adopt_options(config["session"], config["revision"] - 1, {"detail_level": 2})
    assert janitor_builtins.is_current(run["run_id"])
    adopted = janitors.adopt_options(config["session"], config["revision"], {"detail_level": 2})
    assert adopted["enabled"] and not janitor_builtins.is_current(run["run_id"])


def test_two_devices_adopting_concurrently_get_one_authoritative_choice():
    config = seed("tool-explainer")
    start = Barrier(2)
    def adopt(value):
        start.wait(timeout=5)
        try:
            return janitors.adopt_options(config["session"], config["revision"], {"detail_level": value})
        finally:
            db.close_local()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(adopt, [1, 4]))
    assert first == second and first["options"]["detail_level"] in {1, 4}
    assert first["revision"] == config["revision"] + 1 and first["enabled"]


@pytest.mark.parametrize("values", [{"detail_level": 5}, {"detail_level": True}, {"timeout_ms": 5000}, {"detail_level": 2, "enabled": True}, {}])
def test_adoption_is_narrow_and_rejects_invalid_settings(values):
    config = seed("tool-explainer")
    with pytest.raises(janitors.JanitorError):
        janitors.adopt_options(config["session"], config["revision"], values)
    assert janitors.get(config["session"]) == config


def test_adoption_cannot_write_routing_or_custom_janitor_settings():
    config = seed()
    with pytest.raises(janitors.JanitorError):
        janitors.adopt_options(config["session"], config["revision"], {"detail_level": 2})
    agents.create_agent(persona="Custom", voice_id="", cwd="/tmp", session="custom", backend="codex")
    custom = janitors.create("custom", template_id="tool-explainer")
    with pytest.raises(janitors.JanitorError):
        janitors.adopt_options(custom["session"], custom["revision"], {"detail_level": 2})


def test_v74_migration_adds_options_without_enabling_or_rewriting_legacy_data():
    aid = agents.create_agent(persona="Labels", voice_id="", cwd="/tmp", session="labels")
    config = janitors.create("labels")
    c = db.conn()
    c.execute("ALTER TABLE janitor_configs DROP COLUMN options_json")
    c.execute("PRAGMA user_version=74")
    db._migrate(c)
    assert janitors.get("labels") == config
    assert c.execute("SELECT options_json FROM janitor_configs WHERE agent_id=?", (aid,)).fetchone()[0] == "{}"


def test_demand_templates_list_supported_providers_without_probing_runtime(monkeypatch):
    def forbidden(*args):
        raise AssertionError("Template metadata cannot query runtime providers")
    monkeypatch.setattr(backends, "active_handles", forbidden)
    values = {item["id"]: item for item in janitors.templates()}
    assert values["tool-explainer"]["supported_providers"] == ["codex"]
    assert set(values["message-delegator"]["supported_providers"]) == {"openai", *(item.id for item in backends.routing_adapters())}
    assert "supported_providers" not in values["task-labels"]


def creation_payload(**changes):
    return {"name": "Custom router", "backend": "codex", "cwd": "/tmp", "model": "gpt-5.4-mini", "effort": "minimal",
            "template_id": "message-delegator", "execution": {"executor": "ephemeral", "provider": "openai"},
            "options": {"confidence_threshold": .92, "timeout_ms": 45000}, **changes}


def test_new_identity_options_and_provider_are_validated_before_reservation_and_retained_on_retry():
    payload = creation_payload()
    intent = janitors.begin_creation("options-request", payload)
    assert janitors.begin_creation("options-request", payload) == intent
    identity = dict(intent["identity"])
    identity["persona"] = identity.pop("name")
    aid = agents.create_agent(**identity, voice_id="", session=intent["session"], creation_request_id=intent["request_id"])
    configured = janitors.create(intent["session"], template_id=payload["template_id"], options=payload["options"], execution=payload["execution"])
    assert configured["agent_id"] == aid and configured["execution"]["provider"] == "openai"
    assert configured["effort"] == "minimal" and configured["options"]["confidence_threshold"] == .92
    assert not configured["enabled"]
    assert janitors.complete_creation("options-request", intent["session"]) == configured
    with pytest.raises(janitors.JanitorError, match="different"):
        janitors.begin_creation("options-request", creation_payload(options={"confidence_threshold": .93}))


@pytest.mark.parametrize("changes", [{"options": {"timeout_ms": 0}}, {"options": {"enabled": True}},
    {"execution": {"executor": "ephemeral", "provider": "claude"}},
    {"execution": {"executor": "interactive", "provider": "openai"}}])
def test_new_identity_invalid_options_or_provider_leave_no_reservation(changes):
    with pytest.raises(janitors.JanitorError):
        janitors.begin_creation("invalid-options", creation_payload(**changes))
    assert not db.conn().execute("SELECT 1 FROM janitor_creation_requests").fetchone()
    assert not agents.list_agents()


def test_unbounded_integer_option_is_a_validation_error():
    config = seed()
    with pytest.raises(janitors.JanitorError, match="option"):
        janitors.configure(config["session"], config["revision"], options={"timeout_ms": 10**1000})
