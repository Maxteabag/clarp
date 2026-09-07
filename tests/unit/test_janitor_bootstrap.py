"""Startup migration imports existing choices once, never on a read request."""
from lib import settings_store


def test_fresh_host_has_visible_spark_workers_without_enabling_routing(tmp_path):
    from lib.janitor_bootstrap import initialize
    from lib import janitor_builtins
    initialize(cwd=str(tmp_path))
    router = janitor_builtins.get_builtin("message-delegator")
    explainer = janitor_builtins.get_builtin("tool-explainer")
    assert router["model"] == explainer["model"] == "gpt-5.3-codex-spark"
    assert router["enabled"] is False and explainer["enabled"] is True


def test_startup_migrates_explicit_router_settings_once_and_preserves_pause(tmp_path):
    from lib.janitor_bootstrap import initialize
    from lib import janitor_builtins, janitors
    settings_store.set_bool("orchestrator.enabled", True)
    settings_store.set_text("orchestrator.provider", "codex")
    settings_store.set_text("orchestrator.model", "gpt-5.3-codex-spark")
    settings_store.set_text("orchestrator.effort", "")
    initialize(cwd=str(tmp_path))
    first = janitor_builtins.get_builtin("message-delegator")
    assert first["enabled"] and first["effort"] == ""
    janitors.set_enabled(first["session"], first["revision"], False)
    initialize(cwd="/another/place")
    second = janitor_builtins.get_builtin("message-delegator")
    assert second["agent_id"] == first["agent_id"] and not second["enabled"]
    assert second["revision"] == first["revision"] + 1


def test_legacy_api_provider_is_preserved_as_execution_configuration(tmp_path):
    from lib.janitor_bootstrap import initialize
    from lib import janitor_builtins
    settings_store.set_bool("orchestrator.enabled", True)
    settings_store.set_text("orchestrator.provider", "openai")
    settings_store.set_text("orchestrator.model", "gpt-5.4-mini")
    initialize(cwd=str(tmp_path))
    router = janitor_builtins.get_builtin("message-delegator")
    assert router["execution"]["provider"] == "openai"
    assert router["model"] == "gpt-5.4-mini" and router["enabled"]
