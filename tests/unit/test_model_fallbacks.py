import pytest
from lib import agents, model_fallbacks as fallback

GEMINI = {"backend": "agy", "model": "gemini-3.8-flash-low", "effort": ""}


@pytest.fixture
def agent():
    agents.create_agent(
        persona="Robot", voice_id="", cwd="/tmp", session="robot", backend="codex"
    )
    return agents.get_by_session("robot")


def chain(monkeypatch, models):
    """Give every agent this retry chain (it normally comes from Janitor policy)."""
    monkeypatch.setattr(
        fallback, "get", lambda agent_id: {"models": models, "revision": 1}
    )


def test_an_agent_without_janitor_policy_has_no_fallback_chain(agent):
    assert fallback.get(agent["agent_id"]) == {"models": [], "revision": 0}


def test_failed_model_uses_fallback_once_and_records_actual_model(agent, monkeypatch):
    chain(monkeypatch, [GEMINI])
    calls = []

    def invoke(config):
        calls.append(config["model"])
        if config["backend"] == "codex":
            raise RuntimeError("usage limit reached")
        return {"ok": True}

    result = fallback.execute(
        agent["agent_id"],
        "request",
        {"backend": "codex", "model": "spark", "effort": "low"},
        invoke,
    )
    assert result == {"ok": True}
    assert calls == ["spark", GEMINI["model"]]
    assert fallback.attempts(agent["agent_id"], "request")[0]["status"] == "completed"
    assert (
        fallback.attempts(agent["agent_id"], "request")[0]["model"] == GEMINI["model"]
    )


@pytest.mark.parametrize(
    "error", ["aborted by user", "permission denied", "approval required"]
)
def test_user_stop_and_permission_failure_never_fall_back(agent, error, monkeypatch):
    chain(monkeypatch, [GEMINI])
    calls = []

    def invoke(config):
        calls.append(config)
        raise RuntimeError(error)

    with pytest.raises(RuntimeError):
        fallback.execute(agent["agent_id"], "r", {"backend": "codex"}, invoke)
    assert len(calls) == 1


def test_primary_success_does_not_invoke_fallback(agent, monkeypatch):
    chain(monkeypatch, [GEMINI])
    assert (
        fallback.execute(agent["agent_id"], "r", {"backend": "codex"}, lambda _: "done")
        == "done"
    )
    assert fallback.attempts(agent["agent_id"], "r") == []


def test_stale_request_does_not_invoke_fallback(agent, monkeypatch):
    chain(monkeypatch, [GEMINI])
    with pytest.raises(fallback.Cancelled):
        fallback.execute(
            agent["agent_id"],
            "r",
            {"backend": "codex"},
            lambda _: 1,
            current=lambda: False,
        )


def test_same_fallback_attempt_cannot_run_twice(agent):
    assert fallback.claim(agent["agent_id"], "r", 0, GEMINI, "quota")
    assert not fallback.claim(agent["agent_id"], "r", 0, GEMINI, "quota")


def test_agy_finishing_tool_metadata_is_not_mistaken_for_the_answer():
    schema = {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
        "additionalProperties": False,
    }
    reply = '```json\n{"answer":"List files"}\n```\n{"answer":"Finished generating a label","toolAction":"Finish task"}'
    assert fallback.schema_object(reply, schema) == {"answer": "List files"}
    with pytest.raises(fallback.ProviderFailure):
        fallback.schema_object(
            '{"answer":"Finished generating a label","toolAction":"Finish task"}',
            schema,
        )


def test_exhausted_fallbacks_are_bounded_and_visible(agent, monkeypatch):
    model2 = {"backend": "codex", "model": "gpt-5.3-codex-spark", "effort": "low"}
    chain(monkeypatch, [GEMINI, model2])
    calls = []

    def invoke(model):
        calls.append(model)
        raise RuntimeError("provider failed")

    with pytest.raises(RuntimeError, match="provider failed"):
        fallback.execute(agent["agent_id"], "r", {"backend": "claude"}, invoke)
    assert len(calls) == 3
    assert [r["status"] for r in fallback.attempts(agent["agent_id"], "r")] == [
        "failed",
        "failed",
    ]


# --- Trigger policy: only an AI/provider failure switches models -------------


@pytest.mark.parametrize(
    "category",
    ["usage_limit", "connection", "transient", "runner_exit", "timeout"],
)
def test_provider_failures_are_the_only_retry_trigger(category):
    assert fallback.is_provider_failure(category)


@pytest.mark.parametrize(
    "category, message",
    [
        # The user pressed stop; the agent is not broken.
        ("interrupted", "turn_aborted"),
        # A turn the dispatcher could not name is not evidence of an outage.
        ("unknown", "something odd happened"),
        # The turn finished; its tools merely reported errors.
        ("clean", "2 tests failed"),
        # A refusal is an answer, not an outage.
        ("runner_exit", "codex exited rc=1: permission denied"),
        ("runner_exit", "codex exited rc=1: approval required"),
    ],
)
def test_agent_work_failures_never_switch_models(category, message):
    assert not fallback.is_provider_failure(category, message)


@pytest.mark.parametrize(
    "message, expected",
    [
        ("You've hit your usage limit. Try again at Sep 13th", "usage_limit"),
        ("workspace is out of credits", "usage_limit"),
        ("fetch failed", "connection"),
        ("Error 529: overloaded", "transient"),
        ("agy exited rc=1", "runner_exit"),
    ],
)
def test_real_provider_outage_messages_classify_as_failures(message, expected):
    promoted = fallback.provider_error(message)
    assert isinstance(promoted, fallback.ProviderFailure)
    assert promoted.category == expected
    assert fallback.is_provider_failure(promoted)


def test_user_interrupt_from_a_runner_is_never_a_provider_failure():
    promoted = fallback.provider_error("turn_aborted by user")
    assert isinstance(promoted, fallback.Cancelled)
    assert not fallback.is_provider_failure(promoted)


ANSWER_SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}


# Grok is excluded: its Build account balance is exhausted (HTTP 402), so a
# live probe cannot distinguish a code fault from an unpaid account. The
# generic argv/extraction path it shares with the others is covered here.
@pytest.mark.parametrize("backend", ["claude", "codex", "agy", "opencode"])
def test_structured_fallback_runs_on_every_provider(backend, monkeypatch, tmp_path):
    """One extractor, one argv builder: no per-CLI branching in the fallback."""
    from lib import backends as registry

    runner = registry.get(backend)
    script = tmp_path / "cli"
    script.write_text('#!/bin/sh\necho \'{"answer":"from ' + backend + '"}\'\n')
    script.chmod(0o755)
    monkeypatch.setattr(
        runner, "routing_cmd", lambda prompt, **kw: [str(script), prompt]
    )
    monkeypatch.setattr(runner, "routing_text", lambda stdout: stdout)
    assert fallback.json_call(
        {"backend": backend, "model": "m", "effort": ""}, "explain", ANSWER_SCHEMA
    ) == {"answer": f"from {backend}"}


def test_structured_fallback_reports_a_dead_provider_as_a_provider_failure(
    monkeypatch, tmp_path
):
    from lib import backends as registry

    runner = registry.get('codex')
    script = tmp_path / "cli"
    script.write_text("#!/bin/sh\necho 'fetch failed' >&2\nexit 1\n")
    script.chmod(0o755)
    monkeypatch.setattr(runner, "routing_cmd", lambda prompt, **kw: [str(script)])
    with pytest.raises(fallback.ProviderFailure) as raised:
        fallback.json_call(
            {"backend": "codex", "model": "m", "effort": ""}, "x", ANSWER_SCHEMA
        )
    assert raised.value.category == "connection"


def test_structured_fallback_rejects_a_non_routing_provider():
    with pytest.raises(ValueError):
        fallback.json_call(
            {"backend": "nonesuch", "model": "m", "effort": ""}, "x", ANSWER_SCHEMA
        )


