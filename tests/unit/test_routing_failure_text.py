"""A routing CLI's failure keeps its own words only when they hold none of the request (issue 360,
R85). A stub CLI on PATH fails with what ECHO_MODE says: its input repeated (whole, JSON-escaped,
30 characters of it), or a provider's own message. No provider is asked anything."""
from __future__ import annotations

import json
import os
import sys
import textwrap
from types import SimpleNamespace

import pytest

from lib import agents, backends, eventlog, events, model_fallbacks, orchestrator
from tests.unit.test_orchestrator_janitor import ROLE, builtins

START = "MARKER-START-7d21"
DEEP = "MARKER-DEEP-7d21"
BACKENDS = ("claude", "codex", "grok", "muse", "agy", "opencode")
STUB = textwrap.dedent(f"""\
    #!{sys.executable}
    import json, os, sys
    argv = sys.argv[1:]
    raw = b"" if sys.stdin is None or sys.stdin.isatty() else sys.stdin.buffer.read()
    given = raw.decode("utf-8", "surrogateescape")
    if "--prompt-file" in argv:
        given += open(argv[argv.index("--prompt-file") + 1], encoding="utf-8").read()
    request = " ".join(argv) + " " + given
    mode = os.environ.get("ECHO_MODE", "whole")
    if mode == "whole":
        said = "error: could not handle: " + request
    elif mode == "json":
        said = "error: " + json.dumps({{"input": given}})
    elif mode == "denied":
        said = "permission denied: " + request
    elif mode == "partial":
        at = given.find(os.environ["PARTIAL_FROM"])
        said = "bad input near '" + given[at:at + 30] + "'"
    else:
        said = os.environ["SAID"]
    sys.stderr.write(said)
    sys.exit(3)
""")


@pytest.fixture
def failing(tmp_path, monkeypatch, fake_backend_cli):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for backend in BACKENDS:
        name = backends.get(backend).routing_cmd("x")[0]
        path = bin_dir / name
        path.write_text(STUB)
        path.chmod(0o755)
        fake_backend_cli(path)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    def mode(name, **env):
        monkeypatch.setenv("ECHO_MODE", name)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
    return mode


def _router(monkeypatch, provider):
    """A real router on ``provider``; DEEP is in the spoken-to agent's recent words, so it is in the
    prompt (on argv for agy and opencode) and never in the utterance."""
    for session in ("mike", "antoni"):
        agents.create_agent(persona=session.title(), voice_id="", cwd="/tmp", session=session)
    orchestrator.record_routing_message(session="mike", role="assistant", text=f"{DEEP} the plan")
    builtins().ensure_builtins(initial={ROLE: {
        "enabled": True, "backend": provider, "provider": provider, "model": "", "effort": "",
        "options": {"fallback_only": False}}})
    seen = []
    real_emit = eventlog.emit
    monkeypatch.setattr(eventlog, "emit", lambda *a, **k: (seen.append(repr((a, k))), real_emit(*a, **k))[1])
    monkeypatch.setattr(events, "broadcast", lambda stream, event: seen.append(repr(event)))
    service = orchestrator.OrchestratorService(SimpleNamespace(
        default_session="mike", stream=None, speak_announcement=lambda *a, **k: None))
    return service, seen


@pytest.mark.parametrize("provider", BACKENDS)
def test_a_router_cli_that_repeats_its_input_leaves_it_in_no_sink(failing, monkeypatch, capfd, provider):
    from lib import db
    failing("whole")
    service, seen = _router(monkeypatch, provider)
    outcome = service.handle_send(text="send the report", requested_session="mike", trace_id="t-85",
                                  hands_free=True, synthesize_audio=False,
                                  dispatch=lambda **kwargs: pytest.fail("nothing is dispatched"))
    stored = [dict(row) for row in db.conn().execute("SELECT * FROM orchestrator_decisions")]
    assert stored and stored[0]["error"] == f"{provider} exited with status 3 (runner_exit)"
    sinks = {"log lines": capfd.readouterr().err, "eventlog and events": "\n".join(seen),
             "stored errors": repr([row["error"] for row in stored]), "send answer": repr(outcome),
             "listing": repr([row.get("error") for row in orchestrator.recent_decisions()])}
    assert [name for name, text in sinks.items()
            if DEEP in text or "could not handle" in text or "low-latency" in text] == []


SCHEMA = {"type": "object", "properties": {"kind": {"type": "string"}}, "required": ["kind"]}


def _json_call_failure(backend, prompt):
    with pytest.raises(model_fallbacks.ProviderFailure) as caught:
        model_fallbacks.json_call({"backend": backend, "model": ""}, prompt, SCHEMA)
    return str(caught.value)


@pytest.mark.parametrize("provider", ["claude", "agy"])
def test_a_fallback_call_that_repeats_its_input_from_the_start_keeps_none_of_it(failing, provider):
    failing("whole")
    said = _json_call_failure(provider, f"{START} the secret plan " + "and more words " * 400 + DEEP)
    assert said == f"{provider} exited with status 3 (runner_exit)"


def test_a_json_escaped_echo_is_recognised(failing):
    """Every 15 characters a quote or a newline, so the escaped form shares no 24 characters with
    the prompt unless its escapes are undone."""
    failing("json")
    prompt = "".join(f'part {n:04d} of it"\n' for n in range(200))
    assert "\\n" not in prompt and '\\"' not in prompt
    assert _json_call_failure("claude", prompt) == "claude exited with status 3 (runner_exit)"


def test_an_echo_of_30_characters_from_deep_in_the_prompt_is_recognised(failing):
    prompt = "Answer as JSON. " + "filler words " * 300 + "the quarterly numbers for Nordic sales " + "x" * 50
    failing("partial", PARTIAL_FROM="quarterly numbers")
    assert _json_call_failure("claude", prompt) == "claude exited with status 3 (runner_exit)"


@pytest.mark.parametrize("message", [
    "Error: rate limit reached for this organisation; retry after 30 s",
    "unknown model claude-opus-5-5-very-long-model-identifier-20261118",
    "not logged in: run `claude login`",
])
def test_a_providers_own_message_is_kept_word_for_word(failing, message):
    failing("plain", SAID=message)
    assert _json_call_failure("claude", "Answer as JSON: " + "the plan " * 100) == message


def test_a_providers_message_is_one_line_of_at_most_300_characters(failing):
    failing("plain", SAID="first line\n\tsecond\x07 line " + "y" * 400)
    said = _json_call_failure("claude", "Answer as JSON: " + "the plan " * 100)
    assert said == ("first line second line " + "y" * 400)[:300]


def test_a_common_phrase_shorter_than_the_run_is_kept_and_one_as_long_is_not(failing):
    phrase = "please try again later."          # 23 characters
    assert len(phrase) == 23                     # one short of the run (24)
    prompt = f"Text x{phrase}x Answer as JSON. " + "z" * 200     # shared: the phrase alone
    failing("plain", SAID=f"busy:{phrase}")
    assert _json_call_failure("claude", prompt) == f"busy:{phrase}"
    longer = "please try again later!!"         # 24 characters; shared with the prompt, no more
    assert len(longer) == 24
    failing("plain", SAID=f"busy:{longer}")
    assert _json_call_failure("claude", f"Text x{longer}x " + "z" * 200).endswith("(runner_exit)")


def test_a_janitors_question_that_is_repeated_back_keeps_none_of_it(failing, monkeypatch):
    from lib import janitor_autonomy, janitor_builtins
    failing("whole")
    monkeypatch.setattr(janitor_builtins, "is_current", lambda run_id: True)
    monkeypatch.setattr(model_fallbacks, "execute",
                        lambda agent_id, request_id, primary, invoke, current=None: invoke(primary))
    run = {"agent_id": "a", "run_id": "r", "configuration": {
        "provider": "claude", "model": "", "effort": "", "template_id": "t", "isolated": False}}
    with pytest.raises(model_fallbacks.ProviderFailure) as caught:
        janitor_autonomy.model_json(run, f"{START} the secret plan")
    assert str(caught.value) == "claude exited with status 3 (runner_exit)"


def test_the_heartbeat_keepers_question_that_is_repeated_back_keeps_none_of_it(failing, monkeypatch):
    from lib import janitor_autonomy, janitor_builtins
    failing("whole")
    monkeypatch.setattr(janitor_builtins, "is_current", lambda run_id: True)
    monkeypatch.setattr(model_fallbacks, "execute",
                        lambda agent_id, request_id, primary, invoke, current=None: invoke(primary))
    run = {"agent_id": "a", "run_id": "r", "configuration": {
        "provider": "codex", "model": "", "effort": "", "template_id": "t"}}
    with pytest.raises(model_fallbacks.ProviderFailure) as caught:
        janitor_autonomy.decision_model({"goal": f"{START} the secret plan"}, run)
    assert str(caught.value) == "codex exited with status 3 (runner_exit)"


def test_an_isolated_antigravity_failure_that_repeats_the_question_keeps_none_of_it(monkeypatch):
    from lib import janitor_autonomy, janitor_builtins
    monkeypatch.setattr(janitor_builtins, "is_current", lambda run_id: True)
    monkeypatch.setattr(model_fallbacks, "execute",
                        lambda agent_id, request_id, primary, invoke, current=None: invoke(primary))
    agy = type(backends.get("agy"))

    def fails(self, prompt, **kwargs):
        raise RuntimeError(f"agy exited 1 without JSON: --print={prompt}")
    monkeypatch.setattr(agy, "isolated_oneshot", fails)
    monkeypatch.setattr("shutil.which", lambda name: "/bin/true")
    run = {"agent_id": "a", "run_id": "r", "configuration": {
        "provider": "agy", "model": "", "effort": "", "template_id": "t", "isolated": True}}
    with pytest.raises(model_fallbacks.ProviderFailure) as caught:
        janitor_autonomy.model_json(run, f"{START} the secret plan and its details")
    assert str(caught.value) == "agy exited with status 1 (runner_exit)"
    from lib import goal_keeper
    assert goal_keeper.error_code(caught.value) == "agy_exit_1"
    assert caught.value.__cause__ is None and caught.value.__suppress_context__   # no traceback shows it


def test_the_map_rule_authors_failure_that_repeats_its_prompt_keeps_none_of_it(failing, monkeypatch, capfd):
    """Outside the fallback chain: its failure was logged and answered as the CLI's stderr."""
    from lib import viz_rule_author
    failing("whole")
    monkeypatch.setattr(viz_rule_author, "DEFAULT_MODEL_CMD", [backends.get("claude").routing_cmd("x")[0]])
    clusters = [{"hint": f"{DEEP}-tool", "count": 3, "example": f"{START} ran it"}]
    answer = viz_rule_author.propose(clusters)
    said = repr(answer) + capfd.readouterr().err
    assert START not in said and DEEP not in said and "could not handle" not in said
    assert "exited with status 3 (runner_exit)" in repr(answer)


# ---- R85 (a) and (b), ruled 01:03: a failure's facts are attributes; its words are for a person ----

def test_a_refusal_that_repeats_its_prompt_is_still_a_refusal_not_a_fallback(failing):
    failing("denied")
    with pytest.raises(model_fallbacks.ProviderFailure) as caught:
        model_fallbacks.json_call({"backend": "claude", "model": ""}, f"{START} the secret plan", SCHEMA)
    failure = caught.value
    assert START not in str(failure) and "permission denied" not in str(failure)   # the words go
    assert failure.denied is True                                                  # the fact stays
    assert model_fallbacks._as_provider_failure(failure) is None
    assert model_fallbacks.is_provider_failure(failure) is False


@pytest.mark.parametrize("provider", BACKENDS)
def test_every_backends_exit_is_named_by_its_facts(failing, provider):
    from lib import goal_keeper
    failing("whole")
    with pytest.raises(model_fallbacks.ProviderFailure) as caught:
        model_fallbacks.json_call({"backend": provider, "model": ""}, f"{START} the plan", SCHEMA)
    assert (caught.value.backend, caught.value.returncode) == (provider, 3)
    assert goal_keeper.error_code(caught.value) == f"{provider}_exit_3"
