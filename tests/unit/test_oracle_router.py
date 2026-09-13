import io
import json
import threading

import pytest

from lib import oracle_router as router


TOOLS = [{"type": "function", "name": "delegate_to_agent", "parameters": {
    "type": "object", "properties": {"agent": {"type": "string"}, "request": {"type": "string", "maxLength": 16000}},
    "required": ["agent", "request"], "additionalProperties": False}}]
BODY = {"model": router.MODEL, "instructions": "Route the request", "input": "{}", "tools": TOOLS}


def result(arguments=None):
    return {"output": [{"type": "function_call", "name": "delegate_to_agent", "call_id": "p-1",
        "arguments": json.dumps(arguments if arguments is not None else {"agent": "marcus", "request": "check README.md"})}]}


@pytest.mark.parametrize("arguments", [None, [], {"agent": "marcus"}, {"agent": 7, "request": "check"},
    {"agent": "marcus", "request": "x"*16001}, {"agent": "marcus", "request": "check", "approve": True}])
def test_bad_arguments_rejected_before_admission(arguments):
    value = result()
    value["output"][0]["arguments"] = json.dumps(arguments)
    with pytest.raises(router.RouterError, match="invalid_router_arguments"):
        router.validate_result(value, TOOLS)


def test_subscription_environment_cannot_pick_up_an_api_key_or_cli_configuration(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-be-inherited")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://wrong.invalid")
    monkeypatch.setenv("CODEX_HOME", "/fixture-login")
    env = router.subscription_environment()
    assert "OPENAI_API_KEY" not in env and "OPENAI_BASE_URL" not in env
    assert env["CODEX_HOME"] == "/fixture-login"


def test_codex_failure_never_tries_paid_api(monkeypatch):
    calls = []
    def failure(*args):
        raise router.RouterError("codex_router_failed")
    monkeypatch.setattr(router, "_codex", failure)
    monkeypatch.setattr(router, "urlopen", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(router.RouterError, match="codex_router_failed"):
        router.route(BODY, backend="codex", api_key="present", stop=threading.Event())
    assert calls == []


@pytest.mark.parametrize("backend", router.BACKENDS)
def test_routes_use_same_model_contract_and_have_explicit_billing_provenance(monkeypatch, backend):
    monkeypatch.setattr(router, "_codex", lambda *args: result())
    monkeypatch.setattr(router, "urlopen", lambda *args, **kwargs: io.BytesIO(json.dumps(result()).encode()))
    value = router.route(BODY, backend=backend, api_key="fixture", stop=threading.Event())
    assert value["router"]["model"] == "gpt-5.6-luna"
    assert value["router"]["billing"] == ("chatgpt_subscription" if backend == "codex" else "openai_api")
    assert value["output"] == result()["output"]


def test_cancelled_request_never_starts_provider(monkeypatch):
    calls = []
    monkeypatch.setattr(router, "_codex", lambda *args: calls.append(args))
    stop = threading.Event(); stop.set()
    with pytest.raises(router.RouterError, match="router_cancelled"):
        router.route(BODY, backend="codex", api_key="", stop=stop)
    assert not calls


def test_multiple_actions_and_unknown_tools_rejected():
    value = result(); value["output"] *= 2
    with pytest.raises(router.RouterError, match="invalid_router_action"):
        router.validate_result(value, TOOLS)
    value = result(); value["output"][0]["name"] = "run_shell"
    with pytest.raises(router.RouterError, match="invalid_router_action"):
        router.validate_result(value, TOOLS)
