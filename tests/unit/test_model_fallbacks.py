import json
import time
from types import SimpleNamespace

import pytest
from lib import agents, model_fallbacks as fallback

GEMINI = {"backend": "agy", "model": "gemini-3.8-flash-low", "effort": ""}


@pytest.fixture
def agent():
    from lib import settings_store

    settings_store.set_text(
        "provider.agy.last_observed_model_ids", '["gemini-3.8-flash-low"]'
    )
    agents.create_agent(
        persona="Robot", voice_id="", cwd="/tmp", session="robot", backend="codex"
    )
    return agents.get_by_session("robot")


def test_round_trip_and_revision_guard(agent):
    saved = fallback.configure(agent["agent_id"], [GEMINI], expected_revision=0)
    assert saved["models"] == [GEMINI]
    assert saved["revision"] == 1
    with pytest.raises(fallback.Conflict):
        fallback.configure(agent["agent_id"], [], expected_revision=0)
    assert fallback.get(agent["agent_id"]) == saved


@pytest.mark.parametrize(
    "value",
    [
        None,
        {},
        [GEMINI] * 5,
        [{"backend": "fake", "model": "x"}],
        [{**GEMINI, "effort": "high"}],
    ],
)
def test_invalid_configuration_rejected(agent, value):
    with pytest.raises(ValueError):
        fallback.configure(agent["agent_id"], value, expected_revision=0)


def test_failed_model_uses_fallback_once_and_records_actual_model(agent):
    fallback.configure(agent["agent_id"], [GEMINI], expected_revision=0)
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
def test_user_stop_and_permission_failure_never_fall_back(agent, error):
    fallback.configure(agent["agent_id"], [GEMINI], expected_revision=0)
    calls = []

    def invoke(config):
        calls.append(config)
        raise RuntimeError(error)

    with pytest.raises(RuntimeError):
        fallback.execute(agent["agent_id"], "r", {"backend": "codex"}, invoke)
    assert len(calls) == 1


def test_primary_success_does_not_invoke_fallback(agent):
    fallback.configure(agent["agent_id"], [GEMINI], expected_revision=0)
    assert (
        fallback.execute(agent["agent_id"], "r", {"backend": "codex"}, lambda _: "done")
        == "done"
    )
    assert fallback.attempts(agent["agent_id"], "r") == []


def test_stale_request_does_not_invoke_fallback(agent):
    fallback.configure(agent["agent_id"], [GEMINI], expected_revision=0)
    with pytest.raises(fallback.Cancelled):
        fallback.execute(
            agent["agent_id"],
            "r",
            {"backend": "codex"},
            lambda _: 1,
            current=lambda: False,
        )


def test_same_fallback_attempt_cannot_run_twice(agent):
    fallback.configure(agent["agent_id"], [GEMINI], expected_revision=0)
    assert fallback.claim(agent["agent_id"], "r", 0, GEMINI, "quota")
    assert not fallback.claim(agent["agent_id"], "r", 0, GEMINI, "quota")


def test_dispatch_failure_preserves_primary_identity_and_delivers_result(
    tmp_path, monkeypatch
):
    import threading
    from test_turn_dispatch import _make_service
    from lib import turn_model_fallback, settings_store

    settings_store.set_text(
        "provider.agy.last_observed_model_ids", '["gemini-3.8-flash-low"]'
    )
    service, backends, aid = _make_service(tmp_path)
    fallback.configure(aid, [GEMINI], expected_revision=0)
    completed = threading.Event()
    original_finish = service._finish_turn

    def finish(spec):
        original_finish(spec)
        completed.set()

    monkeypatch.setattr(service, "_finish_turn", finish)
    called = []

    def invoke(registry, spec, model, prompt, owned):
        assert owned(lambda: None)
        called.append(model)
        assert "unfinished request" in prompt
        return {"last_agent_message": "Recovered without repeating completed work"}

    monkeypatch.setattr(turn_model_fallback, "invoke", invoke)
    service.dispatch(
        text="Do the work",
        requested_session="mike",
        synthesize_audio=False,
        trace_id="recover",
    )
    primary_id = agents.live_backend_session(aid)
    backends.spawned[0][1]["on_error"]("usage limit reached")
    assert completed.wait(3)
    assert called == [GEMINI]
    assert agents.get_by_agent_id(aid)["backend"] == "claude"
    assert agents.live_backend_session(aid) == primary_id
    assert (
        fallback.conversation_rows(aid)[0]["text"]
        == "Recovered without repeating completed work"
    )
    assert "Recovered without repeating" in fallback.continuation_context(aid)


def test_prior_runtime_fallbacks_do_not_leak_into_new_conversation(agent):
    aid = agent["agent_id"]
    agents.start_runtime(aid, "robot")
    fallback.claim(aid, "r", 0, GEMINI, "quota")
    fallback.finish(
        aid, "r", 0, status="completed", result={"text": "Old private conversation"}
    )
    assert fallback.conversation_rows(aid)
    agents.end_current_runtime(aid)
    agents.start_runtime(aid, "robot")
    assert fallback.conversation_rows(aid) == []


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


def test_delivery_failure_never_repeats_successful_fallback_work(agent, monkeypatch):
    import threading
    from types import SimpleNamespace
    from lib import turn_model_fallback as worker

    model2 = {"backend": "codex", "model": "gpt-5.3-codex-spark", "effort": "low"}
    fallback.configure(agent["agent_id"], [GEMINI, model2], expected_revision=0)
    ready = threading.Event()
    ready.set()
    calls = []
    failures = []
    monkeypatch.setattr(worker, "context", lambda *_: "context")

    def invoke(*args):
        calls.append(args[2])
        return {"last_agent_message": "Work completed"}

    monkeypatch.setattr(worker, "invoke", invoke)
    spec = SimpleNamespace(
        agent_id=agent["agent_id"],
        trace_id="r",
        backend="claude",
        model="",
        effort="",
        text="work",
        cwd="/tmp",
        session="robot",
    )

    def owned(action):
        action()
        return True

    def delivery(*_):
        raise RuntimeError("delivery failed")

    worker.run(
        SimpleNamespace(backends=None),
        spec,
        {"spawn_ready": ready},
        fallback.get(agent["agent_id"]),
        owned,
        delivery,
        failures.append,
    )
    assert calls == [GEMINI]
    assert failures == ["delivery failed"]
    assert fallback.attempts(agent["agent_id"], "r")[0]["status"] == "completed"


def test_exhausted_fallbacks_are_bounded_and_visible(agent):
    model2 = {"backend": "codex", "model": "gpt-5.3-codex-spark", "effort": "low"}
    fallback.configure(agent["agent_id"], [GEMINI, model2], expected_revision=0)
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


def test_isolated_agy_does_not_finalize_against_primary_conversation(monkeypatch):
    from lib import agy_runner
    def forbidden(**_):
        pytest.fail("isolated fallback must not claim primary conversation authority")
    monkeypatch.setattr(agents, "commit_agy_assistant_turn", forbidden)
    state = agy_runner._TurnState(pending_result={"last_agent_message": "Recovered"})
    agy_runner._finalize_success(state, agent_id="", session="robot", trace_id="r", stream=None, enqueue=None)
    assert state.live_text == "Recovered"


# --- Trigger policy: only an AI/provider failure switches models -------------


@pytest.mark.parametrize(
    "category",
    ["usage_limit", "connection", "transient", "runner_exit", "timeout"],
)
def test_provider_failures_are_the_only_fallback_trigger(category):
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


def test_failing_tool_output_does_not_start_a_fallback(tmp_path, monkeypatch):
    """A clean turn whose tools failed must stay on the agent's own model."""
    from test_turn_dispatch import _make_service
    from lib import settings_store, turn_model_fallback

    settings_store.set_text(
        "provider.agy.last_observed_model_ids", '["gemini-3.8-flash-low"]'
    )
    service, _backends, aid = _make_service(tmp_path)
    fallback.configure(aid, [GEMINI], expected_revision=0)
    monkeypatch.setattr(
        turn_model_fallback,
        "run",
        lambda *a, **k: pytest.fail("a tool error must not switch models"),
    )
    spec = SimpleNamespace(agent_id=aid, trace_id="r", session="mike")
    for category, message in [
        ("clean", "pytest: 3 failed"),
        ("unknown", "make: *** [build] Error 2"),
        ("interrupted", "turn_aborted"),
    ]:
        assert not service._start_model_fallback(spec, {}, category, message)


def test_usage_limit_starts_a_fallback(tmp_path, monkeypatch):
    from test_turn_dispatch import _make_service
    from lib import settings_store, turn_model_fallback

    settings_store.set_text(
        "provider.agy.last_observed_model_ids", '["gemini-3.8-flash-low"]'
    )
    service, _backends, aid = _make_service(tmp_path)
    fallback.configure(aid, [GEMINI], expected_revision=0)
    started = []
    monkeypatch.setattr(
        turn_model_fallback, "run", lambda *a, **k: started.append(True)
    )
    spec = SimpleNamespace(agent_id=aid, trace_id="r", session="mike")
    state = {}
    assert service._start_model_fallback(
        spec, state, "usage_limit", "You've hit your usage limit"
    )
    assert state["fallback_reason"] == "usage_limit"
    # A second failure in the same turn must not start a second fallback.
    assert service._start_model_fallback(spec, state, "connection", "fetch failed")
    for _ in range(50):
        if started:
            break
        time.sleep(0.02)
    assert len(started) == 1


def test_no_configured_fallback_leaves_the_turn_alone(tmp_path):
    from test_turn_dispatch import _make_service

    service, _backends, aid = _make_service(tmp_path)
    spec = SimpleNamespace(agent_id=aid, trace_id="r", session="mike")
    assert not service._start_model_fallback(spec, {}, "usage_limit", "out of credits")


# --- Every provider can serve as a fallback ---------------------------------


def test_every_routing_backend_is_offered_as_a_fallback(agent):
    from lib import backends as registry

    offered = fallback.get(agent["agent_id"])["supported_backends"]
    assert offered == [a.id for a in registry.routing_adapters()]
    assert {"claude", "codex", "agy", "opencode"} <= set(offered)


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
    import importlib
    from lib import backends as registry

    adapter = registry.get(backend)
    runner = importlib.import_module(f"lib.{adapter.routing_module}")
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
    import importlib
    from lib import backends as registry

    runner = importlib.import_module(f"lib.{registry.get('codex').routing_module}")
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


def _fallback_row(agent_id, *, text="fallback already did this", finished_ms=1_000_000):
    from lib import agents as agents_db, db
    runtime = agents_db.current_runtime_id(agent_id)
    db.conn().execute(
        """INSERT INTO model_fallback_attempts
             (request_id, agent_id, runtime_id, attempt, backend, model, effort,
              status, reason, started_at, finished_at, result_json)
           VALUES ('req-1', ?, ?, 0, 'gemini', 'gemini-test', '',
                   'completed', 'limit', ?, ?, ?)""",
        (agent_id, runtime, finished_ms - 10, finished_ms,
         json.dumps({"text": text})),
    )


def test_continuation_context_is_delimited_so_the_importer_can_strip_it(tmp_path):
    from lib import agents as agents_db, message_store, model_fallbacks

    agent_id = agents_db.create_agent(
        persona="Mike", voice_id="v", cwd=str(tmp_path), session="mike")
    agents_db.start_runtime(agent_id, "claude")
    agents_db.bind_backend_session(agent_id, "bs1")
    _fallback_row(agent_id)

    context = model_fallbacks.continuation_context(agent_id)
    assert "fallback already did this" in context
    assert context.count(message_store.FALLBACK_CONTEXT_OPEN) == 1
    assert context.count(message_store.FALLBACK_CONTEXT_CLOSE) == 1

    # The importer sees the augmented prompt and must keep only what was typed.
    assert message_store.strip_injected_context("do the thing" + context) == "do the thing"


def test_augmented_prompt_does_not_duplicate_the_user_turn(tmp_path):
    """The reported bug: every turn appeared twice, once several kilobytes long."""
    from lib import agents as agents_db, message_store, model_fallbacks

    agent_id = agents_db.create_agent(
        persona="Mike", voice_id="v", cwd=str(tmp_path), session="mike")
    agents_db.start_runtime(agent_id, "claude")
    agents_db.bind_backend_session(agent_id, "bs1")
    _fallback_row(agent_id)
    message_store.record_user_message(
        agent_id=agent_id, backend_session_id="bs1",
        client_msg_id="c1", text="why is it doing that")

    augmented = "why is it doing that" + model_fallbacks.continuation_context(agent_id)
    message_store.store_transcript_turns(
        agent_id=agent_id, backend_session_id="bs1", source_file="f",
        turns=[{"role": "user", "text": augmented},
               {"role": "assistant", "text": "because of the injection"}])

    rows = [m for m in message_store.list_messages(
        agent_id=agent_id, backend_session_id="bs1") if m["role"] == "user"]
    assert len(rows) == 1, [r["text"][:60] for r in rows]
    assert rows[0]["text"] == "why is it doing that"
    assert "fallback" not in rows[0]["text"].lower()


def test_context_stops_once_the_agents_own_model_answers_again(tmp_path):
    """It was re-appended to every prompt for the life of the runtime."""
    from lib import agents as agents_db, message_store, model_fallbacks

    agent_id = agents_db.create_agent(
        persona="Mike", voice_id="v", cwd=str(tmp_path), session="mike")
    agents_db.start_runtime(agent_id, "claude")
    agents_db.bind_backend_session(agent_id, "bs1")
    _fallback_row(agent_id, finished_ms=1_000_000)

    assert model_fallbacks.continuation_context(agent_id) != ""

    message_store.store_transcript_turns(
        agent_id=agent_id, backend_session_id="bs1", source_file="f",
        turns=[{"role": "assistant", "text": "my own answer",
                "timestamp": "2026-09-07T12:00:00Z"}])

    assert model_fallbacks.continuation_context(agent_id) == ""
