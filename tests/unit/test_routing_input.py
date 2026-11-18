"""A one-shot routing prompt never stands on its CLI's command line (issue 360, R35).

A stub CLI on a temporary PATH writes down its argv, its stdin and the prompt file it was given;
no provider is asked anything."""
from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys
import textwrap

import pytest

from lib import agents, backends, model_fallbacks, orchestrator, orchestrator_model, routing_input
from lib.orchestrator_context import build_context_packet
from lib.orchestrator_types import OrchestratorSettings

DECISION = {"kind": "agent_message", "target_session": "agent000", "confidence": 0.9, "reason": "stub"}
STUB = textwrap.dedent(f"""\
    #!{sys.executable}
    import json, os, sys, time
    argv = sys.argv[1:]
    # Bytes as they came, read back the way the command line would give them (surrogateescape).
    raw = b"" if sys.stdin is None or sys.stdin.isatty() else sys.stdin.buffer.read()
    record = {{"argv": argv, "stdin": raw.decode("utf-8", "surrogateescape")}}
    for flag in ("--prompt-file",):
        if flag in argv:
            path = argv[argv.index(flag) + 1]
            record["file"] = path
            record["file_text"] = open(path, "rb").read().decode("utf-8", "surrogateescape")
            record["file_mode"] = oct(os.stat(os.path.dirname(path)).st_mode & 0o777)
            record["file_perm"] = oct(os.stat(path).st_mode & 0o777)
    with open(os.environ["STUB_LOG"], "a", encoding="utf-8") as log:
        log.write(json.dumps(record) + "\\n")
    behaviour = os.environ.get("STUB_BEHAVIOUR", "")
    if behaviour == "slow":
        time.sleep(0.6)
    if behaviour == "fail":
        sys.exit(3)
    if behaviour == "hang":
        time.sleep(30)
    print(json.dumps({json.dumps(DECISION)}))
""")
QUIET = ("claude", "codex", "grok", "muse")
ON_ARGV = ("agy", "opencode")


@pytest.fixture
def stub(tmp_path, monkeypatch, fake_backend_cli):
    """Every routing CLI on PATH is the stub (registered with the suite's CLI guard as a fake);
    its records are read back with ``calls()``."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "stub.jsonl"
    for backend in (*QUIET, *ON_ARGV):
        runner = backends.get(backend)
        name = runner.routing_cmd("x")[0]
        assert "/" not in name, name
        path = bin_dir / name
        path.write_text(STUB)
        path.chmod(0o755)
        fake_backend_cli(path)
        # The stub answers with the decision itself, whatever the CLI's own output format is.
        monkeypatch.setattr(type(runner), "routing_text", lambda self, stdout: stdout)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("STUB_LOG", str(log))

    def calls():
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return calls


def ask(backend, packet=None, timeout_ms=10_000):
    packet = packet or {"utterance": "send the report", "agents": [], "pending": []}
    return orchestrator_model.call_model(packet, OrchestratorSettings(
        provider=backend, model="", effort="", timeout_ms=timeout_ms))


def prompt_of(packet):
    return orchestrator_model._model_prompt(packet)


@pytest.mark.parametrize("backend", QUIET)
def test_the_routing_prompt_never_stands_on_the_command_line(stub, backend):
    packet = {"utterance": "the secret plan", "agents": [], "pending": []}
    assert ask(backend, packet)["kind"] == "agent_message"
    [call] = stub()
    assert not any("the secret plan" in value for value in call["argv"]), call["argv"]
    assert (call["stdin"] or call.get("file_text")) == prompt_of(packet)
    if backend == "codex":
        assert call["argv"][-1] == "-"      # its help: "-" reads the instructions from stdin


@pytest.mark.parametrize("backend", ("grok", "muse"))
@pytest.mark.parametrize("behaviour", ["", "fail", "hang"])
def test_a_prompt_file_is_private_and_gone_after_the_call(stub, monkeypatch, backend, behaviour):
    monkeypatch.setenv("STUB_BEHAVIOUR", behaviour)
    if behaviour == "":
        ask(backend)
    else:
        with pytest.raises((RuntimeError, subprocess.TimeoutExpired)):
            ask(backend, timeout_ms=1500)
    [call] = stub()
    assert call["file_mode"] == "0o700"
    assert not pathlib.Path(call["file"]).exists()
    assert not pathlib.Path(call["file"]).parent.exists()


def _fleet(count, monkeypatch):
    """A broad packet of ``count`` agents, each with five words told and five said. Every agent
    keeps its words here (the broad view's bound is lifted): this is about the size of a prompt."""
    from lib import orchestrator_context
    monkeypatch.setattr(orchestrator_context, "BROAD_DETAIL_AGENTS", 10_000, raising=False)
    for n in range(count):
        session = f"agent{n:03d}"
        agents.create_agent(persona=f"Persona{n:03d}", voice_id="", cwd=f"/srv/p{n:03d}", session=session)
        for role in ("user", "assistant"):
            for _ in range(5):
                orchestrator.record_routing_message(session=session, role=role, text="word " * 60)
    return build_context_packet(utterance="send the report", requested_session="agent000",
                                trace_id="t", hands_free=True, settings=OrchestratorSettings(),
                                context_scope="all")


def test_a_broad_packet_of_forty_agents_still_routes(stub, monkeypatch):
    """Over the kernel's limit for one argument (128 KiB) the CLI could not start (E2BIG)."""
    packet = _fleet(40, monkeypatch)
    assert len(prompt_of(packet).encode()) > routing_input.ARGUMENT_LIMIT
    try:
        decision = ask("claude", packet)
    except OSError as exc:
        pytest.fail(f"the routing CLI could not start: {exc!r}")
    assert decision["target_session"] == "agent000"
    assert stub()[0]["stdin"] == prompt_of(packet)


@pytest.mark.parametrize("backend", ON_ARGV)
def test_a_cli_with_no_other_way_in_refuses_a_prompt_too_large_before_it_starts(stub, monkeypatch, backend):
    packet = _fleet(40, monkeypatch)
    with pytest.raises(routing_input.PromptTooLarge, match=f"prompt too large for {backend}"):
        ask(backend, packet)
    assert stub() == []


@pytest.mark.parametrize("backend", ON_ARGV)
def test_a_cli_with_no_other_way_in_still_takes_a_prompt_of_ordinary_size(stub, backend):
    assert ask(backend)["kind"] == "agent_message"
    [call] = stub()
    assert any("send the report" in value for value in call["argv"])


def test_a_prompt_too_large_is_a_provider_failure_the_fallback_chain_takes():
    error = routing_input.PromptTooLarge("prompt too large for agy: the CLI takes it only as an argument")
    assert model_fallbacks._as_provider_failure(error) is not None


def test_the_fallback_models_isolated_request_takes_its_prompt_off_the_command_line_too(stub):
    schema = {"type": "object", "properties": {"kind": {"type": "string"}}, "required": ["kind"]}
    answer = model_fallbacks.json_call({"backend": "claude", "model": ""}, "the secret plan", schema)
    assert answer["kind"] == "agent_message"
    [call] = stub()
    assert not any("the secret plan" in value for value in call["argv"])
    assert call["stdin"].startswith("the secret plan")


def test_every_caller_of_routing_cmd_goes_through_routing_input():
    """A new caller that runs routing_cmd's argv as it is would put the prompt back on argv."""
    root = pathlib.Path(orchestrator_model.__file__).resolve().parent
    callers = {}
    for path in sorted(root.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if re.search(r"\.routing_cmd\(", text) and path.parent.name != "backend":
            runs = len(re.findall(r"\.routing_cmd\(", text))
            callers[path.name] = (runs <= len(re.findall(r"\bprepared\(", text))
                                  and runs <= len(re.findall(r"\bcall\.argv\b", text)))
    assert callers == {"janitor_autonomy.py": True, "model_fallbacks.py": True,
                       "orchestrator_model.py": True}, callers


def test_the_size_check_stops_exactly_at_the_kernels_limit_for_one_argument():
    routing_input.check_argument("agy", "a" * (routing_input.ARGUMENT_LIMIT - 1))
    with pytest.raises(routing_input.PromptTooLarge, match="prompt too large for agy"):
        routing_input.check_argument("agy", "a" * routing_input.ARGUMENT_LIMIT)


def _janitor_run(monkeypatch, provider, *, isolated=False):
    from lib import janitor_builtins
    monkeypatch.setattr(janitor_builtins, "is_current", lambda run_id: True)
    monkeypatch.setattr(model_fallbacks, "execute",
                        lambda agent_id, request_id, primary, invoke, current=None: invoke(primary))
    return {"agent_id": "a", "run_id": "r", "configuration": {
        "provider": provider, "model": "", "effort": "", "template_id": "t", "isolated": isolated}}


def test_a_janitors_one_question_takes_its_prompt_off_the_command_line(stub, monkeypatch):
    from lib import janitor_autonomy
    answer, _usage, _model = janitor_autonomy.model_json(_janitor_run(monkeypatch, "claude"), "the secret plan")
    assert answer["kind"] == "agent_message"
    [call] = stub()
    assert not any("the secret plan" in value for value in call["argv"])
    assert call["stdin"] == "the secret plan"


def test_an_isolated_antigravity_question_too_large_is_refused_before_it_starts(stub, monkeypatch):
    from lib import backends, janitor_autonomy
    started = []
    monkeypatch.setattr(type(backends.get("agy")), "isolated_oneshot",
                        lambda self, prompt, **kw: started.append(prompt) or {"text": "{}", "usage": {}})
    run = _janitor_run(monkeypatch, "agy", isolated=True)
    with pytest.raises(routing_input.PromptTooLarge, match="prompt too large for agy"):
        janitor_autonomy.model_json(run, "a" * routing_input.ARGUMENT_LIMIT)
    assert started == []
    janitor_autonomy.model_json(run, "a small question")
    assert started == ["a small question"]



# ---- PR 437 review follow-up ----------------------------------------------------------------

def test_a_fallback_model_that_answers_after_the_first_poll_still_answers(stub, monkeypatch):
    """json_call polls every 0.25 s; the stdin it sent with the first poll is not sent again
    (communicate keeps its first input), or every slower stdin-fed fallback call would fail."""
    monkeypatch.setenv("STUB_BEHAVIOUR", "slow")
    schema = {"type": "object", "properties": {"kind": {"type": "string"}}, "required": ["kind"]}
    for backend in ("claude", "codex"):
        try:
            answer = model_fallbacks.json_call({"backend": backend, "model": ""}, "the secret plan", schema)
        except Exception as exc:
            pytest.fail(f"{backend}: a slow answer failed: {exc!r}")
        assert answer["kind"] == "agent_message"
    assert [call["stdin"].startswith("the secret plan") for call in stub()] == [True, True]


def test_the_heartbeat_keepers_question_takes_its_prompt_off_the_command_line(stub, monkeypatch):
    from lib import janitor_autonomy
    answer = janitor_autonomy.decision_model({"agent": "the secret plan"}, _janitor_run(monkeypatch, "claude"))
    assert answer["kind"] == "agent_message"
    [call] = stub()
    assert not any("the secret plan" in value for value in call["argv"])
    assert "the secret plan" in call["stdin"]


SCHEMA = {"type": "object", "properties": {"kind": {"type": "string"}}, "required": ["kind"]}


@pytest.mark.parametrize("backend", ["claude", "codex", "grok", "muse", "agy", "opencode"])
def test_a_byte_kept_as_a_surrogate_reaches_the_cli_as_the_command_line_would_carry_it(stub, backend):
    """A prompt with raw text (here the fallback model's question) may hold a lone surrogate in
    the surrogateescape range: a byte a text read from the system kept. On main's argv it went
    through as that byte; on stdin and in the prompt file it goes through the same way."""
    prompt = "caf\udce9 au lait"
    try:
        model_fallbacks.json_call({"backend": backend, "model": ""}, prompt, SCHEMA)
    except Exception as exc:
        pytest.fail(f"{backend}: {exc!r}")
    [call] = stub()
    carried = call["stdin"] or call.get("file_text") or " ".join(call["argv"])
    assert prompt in carried


def test_a_keepers_question_with_such_a_byte_goes_through_too(stub, monkeypatch):
    from lib import janitor_autonomy
    janitor_autonomy.model_json(_janitor_run(monkeypatch, "claude"), "caf\udce9 au lait")
    [call] = stub()
    assert call["stdin"] == "caf\udce9 au lait"


@pytest.mark.parametrize("backend", ["claude", "grok", "agy"])
def test_a_surrogate_no_byte_stands_for_is_one_content_free_failure(stub, backend):
    """Outside that range no CLI can be handed the prompt, as on main: one failure the fallback
    chain takes as the provider's, its words naming the backend only, before anything runs."""
    with pytest.raises(routing_input.PromptNotEncodable) as refused:
        model_fallbacks.json_call({"backend": backend, "model": ""}, "secret \ud800 words", SCHEMA)
    assert str(refused.value) == f"prompt cannot be encoded for {backend}"
    assert model_fallbacks._as_provider_failure(refused.value) is not None
    assert stub() == []


def test_the_prompt_file_is_the_owners_alone(stub):
    import os
    previous = os.umask(0o022)      # an ordinary umask: 0600 must come from the code, not the umask
    try:
        ask("grok")
    finally:
        os.umask(previous)
    [call] = stub()
    assert call["file_perm"] == "0o600" and call["file_mode"] == "0o700"


def test_the_prompt_file_is_never_written_through_a_name_already_there(tmp_path, monkeypatch):
    """O_EXCL: a prompt.txt already in the directory (here a link to elsewhere) is not opened."""
    import tempfile
    target = tmp_path / "elsewhere.txt"
    target.write_text("untouched")
    planted = tmp_path / "planted"
    planted.mkdir()
    (planted / "prompt.txt").symlink_to(target)
    monkeypatch.setattr(tempfile, "mkdtemp", lambda prefix="": str(planted))
    with pytest.raises(FileExistsError):
        with routing_input.prepared("grok", ["grok", "-p", "the secret plan"], "the secret plan"):
            pass
    assert target.read_text() == "untouched"


def test_a_left_behind_prompt_directory_is_swept_once_no_call_can_still_use_it(tmp_path, monkeypatch):
    import os
    import tempfile
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    old, young, other = tmp_path / "clarp-routing-old", tmp_path / "clarp-routing-young", tmp_path / "keep-me"
    for path in (old, young, other):
        path.mkdir()
        (path / "prompt.txt").write_text("x")
    os.utime(old, (1_000, 1_000))
    os.utime(other, (1_000, 1_000))
    assert routing_input.sweep_stale(now=1_000 + 3601) == 1
    assert not old.exists() and young.exists() and other.exists()
