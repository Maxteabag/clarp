"""The example-eu Codex provider profile: one Codex agent's turns go to Azure
OpenAI EU only, and every other Codex agent behaves exactly as before.

A fake ``secret-tool`` supplies a sentinel key and a fake stdio app-server
stands in for Codex; nothing reaches the network or the real keyring.
"""
from __future__ import annotations

import json
import os
import stat
import sys
import threading
import time
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from lib import agents as agents_db
from lib import codex_app_server, codex_profiles, eventlog, snapshot
from lib import turn_dispatch as _td
from lib.protocol import AgentState
from lib.turn_dispatch import TurnDispatchService

pytestmark = pytest.mark.usefixtures("fake_codex_profile")

SENTINEL = "sk-SENTINEL-azure-0f1e2d3c4b5a69788796a5b4c3d2e1f0"

_FAKE_APP_SERVER = r'''
import json, os, sys, uuid
key = os.environ.get("CLARP_CODEX_PROFILE_API_KEY", "")
mode = os.environ.get("FAKE_MODE", "ok")
def send(message):
    print(json.dumps(message), flush=True)
for line in sys.stdin:
    request = json.loads(line)
    method, rid = request.get("method"), request.get("id")
    params = request.get("params") or {}
    if rid is None:
        continue
    if method == "initialize":
        send({"id": rid, "result": {}})
    elif method in ("thread/start", "thread/resume"):
        send({"id": rid, "result": {"thread": {"id": params.get("threadId") or str(uuid.uuid4())}}})
    elif method == "thread/fork":
        send({"id": rid, "result": {"thread": {"id": "forked-" + params["threadId"]}}})
    elif method == "turn/start":
        thread = params["threadId"]
        send({"id": rid, "result": {"turn": {"id": "turn-1"}}})
        send({"method": "turn/started", "params": {"threadId": thread, "turn": {"id": "turn-1"}}})
        if mode == "usage":
            send({"method": "turn/completed", "params": {"threadId": thread, "turn": {
                "id": "turn-1", "status": "failed",
                "error": {"message": "You've hit your usage limit. Try again later."}}}})
        elif mode == "drop":
            sys.exit(1)
        elif mode == "leak":
            sys.stderr.write("upstream said: bad api-key " + key + "\n")
            sys.stderr.flush()
            send({"method": "turn/completed", "params": {"threadId": thread, "turn": {
                "id": "turn-1", "status": "failed",
                "error": {"message": "401 Unauthorized for api-key " + key}}}})
        else:
            send({"method": "item/completed", "params": {"threadId": thread, "turnId": "turn-1",
                  "item": {"type": "agentMessage", "id": "m1", "text": "OK"}}})
            send({"method": "turn/completed", "params": {"threadId": thread, "turn": {
                "id": "turn-1", "status": "completed"}}})
    else:
        send({"id": rid, "error": {"code": -32601, "message": "nope " + key}})
'''


@pytest.fixture
def profile_env(tmp_path, monkeypatch, fake_codex_profile):
    """The fake profile and keyring (tests/conftest.py) holding the sentinel
    key, the fake app-server and an empty pool."""
    assert fake_codex_profile.key == SENTINEL
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "default-codex-home"))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-must-not-reach-azure")
    server = tmp_path / "fake_app_server.py"
    server.write_text(_FAKE_APP_SERVER)
    monkeypatch.setattr(codex_app_server, "_codex_argv",
                        lambda: [sys.executable, str(server), "app-server", "--stdio"])
    spawned: list[dict] = []
    real_popen = codex_app_server.subprocess.Popen

    def recording_popen(argv, *args, **kwargs):
        if "app-server" in list(argv):
            spawned.append({"argv": list(argv), "env": dict(kwargs.get("env") or {})})
        return real_popen(argv, *args, **kwargs)

    monkeypatch.setattr(codex_app_server.subprocess, "Popen", recording_popen)
    eventlog.reset_for_tests(tmp_path / "logs")
    codex_app_server.recycle_clients()
    codex_app_server._CLIENTS.clear()
    codex_profiles.clear_cache()
    yield SimpleNamespace(tmp=tmp_path, spawned=spawned,
                          secret_tool=fake_codex_profile.secret_tool)
    for client in list(codex_app_server._CLIENTS.values()):
        client.shutdown()
    codex_app_server._CLIENTS.clear()
    eventlog.reset_for_tests()


def _agent(session, *, profile="", cwd="/tmp"):
    agent_id = agents_db.create_agent(
        persona=session.title(), voice_id="V", cwd=cwd, session=session,
        backend="codex", **({"provider_profile": profile} if profile else {}))
    agents_db.start_runtime(agent_id, session)
    return agent_id


def _run_turn(agent_id, session, *, profile="", text="hi"):
    done = threading.Event()
    outcome: dict = {}

    def on_result(result):
        outcome["result"] = result
        done.set()

    def on_error(message):
        outcome["error"] = message
        done.set()

    codex_app_server.spawn_turn(
        text=text, cwd=Path("/tmp"), session=session, agent_id=agent_id,
        is_new_session=True, on_result=on_result, on_error=on_error,
        trace_id=f"trace-{session}",
        **({"provider_profile": profile} if profile else {}))
    assert done.wait(10), "fake app-server turn never finished"
    return outcome


# --- 1. no profile: exactly today's process, pool and env ---------------------

def test_a_codex_agent_without_a_profile_keeps_todays_env_and_shared_pool(profile_env):
    agent = _agent("plain")

    assert "result" in _run_turn(agent, "plain")

    [spawn] = profile_env.spawned
    expected = {**os.environ, "CLAUDE_PWA_SESSION": "plain"}
    expected.pop("CLARP_PROVIDER_TURN", None)
    assert spawn["env"] == expected
    assert list(codex_app_server._CLIENTS) == [codex_app_server._SHARED_KEY]
    assert not (profile_env.tmp / "profiles").exists()


# --- 2. the key lives only in the profile app-server's environment --------------

def test_the_profile_app_server_gets_its_own_home_and_the_key_only_in_env(profile_env):
    agent = _agent("eu", profile="example-eu")

    assert "result" in _run_turn(agent, "eu", profile="example-eu")

    [spawn] = profile_env.spawned
    home = profile_env.tmp / "profiles" / "example-eu"
    assert spawn["env"]["CODEX_HOME"] == str(home)
    assert spawn["env"]["CLARP_CODEX_PROFILE_API_KEY"] == SENTINEL
    assert "OPENAI_API_KEY" not in spawn["env"]
    assert SENTINEL not in " ".join(spawn["argv"])
    assert stat.S_IMODE(home.stat().st_mode) == 0o700
    config = tomllib.loads((home / "config.toml").read_text())
    assert config["model"] == "eu-model"
    assert config["model_provider"] == "example-eu"
    assert config["web_search"] == "disabled"
    assert config["check_for_update_on_startup"] is False
    assert config["analytics"] == {"enabled": False}
    assert config["feedback"] == {"enabled": False}
    provider = config["model_providers"]["example-eu"]
    assert provider["base_url"] == (
        "https://example.invalid/openai/v1")
    assert provider["wire_api"] == "responses"
    assert provider["env_http_headers"] == {"api-key": "CLARP_CODEX_PROFILE_API_KEY"}
    assert provider["requires_openai_auth"] is False
    assert provider["supports_websockets"] is False


def test_the_key_never_reaches_logs_eventlog_db_status_or_error_text(
        profile_env, monkeypatch, capsys):
    """The provider echoes the key on stderr and in its error; the real
    dispatcher records the failure everywhere it records failures."""
    monkeypatch.setattr(_td, "_RUNTIME_CLIENT", None)
    monkeypatch.setenv("FAKE_MODE", "leak")
    agent = _agent("eu", profile="example-eu")
    service = TurnDispatchService(
        SimpleNamespace(default_session="eu", agents_path=profile_env.tmp / "x.json",
                        stream=SimpleNamespace(broadcast=lambda _e: None)),
        home=profile_env.tmp, retry_scheduler=lambda _d, _fn: None)

    service.dispatch(text="hi", requested_session="eu", trace_id="t-eu",
                     synthesize_audio=False)
    deadline = time.monotonic() + 10
    while agents_db.latest_state(agent)["kind"] != AgentState.INTERRUPTED:
        assert time.monotonic() < deadline, "the failed turn never ended"
        time.sleep(0.05)
    time.sleep(0.3)  # let the stderr drain log what the fake printed
    with pytest.raises(RuntimeError) as refused:
        codex_app_server._client(agent, "eu", profile="example-eu").request(
            "unknown/method", {})

    state = agents_db.latest_state(agent)
    surfaces = {
        "state detail": json.dumps(state["detail"]),
        "request error": str(refused.value),
        "stderr+stdout": "".join(capsys.readouterr()),
        "eventlog": "".join(p.read_text() for p in (profile_env.tmp / "logs").glob("*")),
        "db": "\n".join(agents_db.conn().iterdump()),
        "snapshot": json.dumps(snapshot.build_agent_snapshot(None), default=str),
        "profile home": "".join(
            p.read_text(errors="replace") for p in
            (profile_env.tmp / "profiles").rglob("*") if p.is_file()),
    }
    leaked = [name for name, text in surfaces.items() if SENTINEL in text]
    assert leaked == []
    assert "[redacted]" in surfaces["state detail"]
    assert "[redacted]" in surfaces["eventlog"]


def test_a_missing_key_fails_the_turn_visibly_without_any_openai_fallback(
        profile_env, monkeypatch):
    profile_env.secret_tool.write_text("#!/bin/sh\nexit 1\n")
    codex_profiles.clear_cache()
    agent = _agent("eu", profile="example-eu")

    with pytest.raises(codex_profiles.ProfileUnavailable) as failure:
        codex_app_server.spawn_turn(
            text="hi", cwd=Path("/tmp"), session="eu", agent_id=agent,
            is_new_session=True, provider_profile="example-eu")

    assert "keyring" in str(failure.value)
    assert profile_env.spawned == []          # never started a process at all


# --- 3. pools and thread locks are per profile; cross-profile is refused --------

def test_profiles_get_separate_app_servers_and_each_agent_finds_its_own(profile_env):
    plain = _agent("plain")
    eu = _agent("eu", profile="example-eu")

    _run_turn(plain, "plain")
    _run_turn(eu, "eu", profile="example-eu")

    assert set(codex_app_server._CLIENTS) == {
        codex_app_server._SHARED_KEY, "profile:example-eu"}
    shared = codex_app_server._CLIENTS[codex_app_server._SHARED_KEY]
    profiled = codex_app_server._CLIENTS["profile:example-eu"]
    assert shared.proc.pid != profiled.proc.pid
    assert codex_app_server._client_for_agent(eu) is profiled
    assert codex_app_server._client_for_agent(plain) is shared


@pytest.mark.parametrize("stored,requested", [("example-eu", ""), ("", "example-eu")])
def test_a_turn_for_another_profile_than_the_agents_is_refused(
        profile_env, stored, requested):
    agent = _agent("eu", profile=stored)

    with pytest.raises(codex_profiles.ProfileMismatch):
        codex_app_server.spawn_turn(
            text="hi", cwd=Path("/tmp"), session="eu", agent_id=agent,
            is_new_session=False, backend_session_id="thread-x",
            **({"provider_profile": requested} if requested else {}))
    assert profile_env.spawned == []


def test_a_native_fork_runs_in_the_profiles_own_app_server(profile_env):
    forked = codex_app_server.fork_thread("thread-1", cwd="/tmp",
                                          provider_profile="example-eu")

    assert forked == "forked-thread-1"
    [spawn] = profile_env.spawned
    assert spawn["env"]["CODEX_HOME"].endswith("example-eu")


def test_usage_recovery_and_rate_limits_never_treat_the_profile_as_an_openai_account(
        profile_env, monkeypatch):
    agent = _agent("eu", profile="example-eu")
    client = codex_app_server._client(agent, "eu", profile="example-eu")
    captured = []
    monkeypatch.setattr(codex_app_server.backend_usage, "capture_codex_rate_limits",
                        lambda params: captured.append(params) or {})

    client._notification("account/rateLimits/updated", {"rateLimits": {}})
    failure = codex_app_server.CodexTurnFailure("usage limit", client, False)

    assert captured == []
    assert codex_app_server.recover_usage_failure(failure) is False
    assert codex_app_server._CLIENTS["profile:example-eu"] is client  # not recycled


def _codex_dispatch(tmp_path, monkeypatch, *, profile, synthesize_audio=False):
    from dataclasses import replace
    from lib.account_failover import AccountFailover
    from unittest.mock import Mock
    cfg = replace(_td.config.load(), codex_account_switch_command=("codex-selector",))
    monkeypatch.setattr(_td.config, "load", lambda *args, **kwargs: cfg)
    coordinator = AccountFailover(
        _td._TURN_LOCK, switch=Mock(return_value=True),
        schedule=lambda _d, _f: None, now=lambda: 100)
    monkeypatch.setitem(_td._FAILOVERS, "codex", coordinator)
    recorded = []
    monkeypatch.setattr(_td.backend_usage, "record_classified_usage_limit",
                        lambda provider: recorded.append(provider) or None)
    agent_id = _agent("eu", profile=profile, cwd=str(tmp_path))

    class Registry:
        CODEX = "codex"

        def __init__(self):
            self.spawned = []

        def normalize(self, backend):
            return backend or "claude"

        def interrupt(self, backend, agent_id):
            return 0

        def active_handles(self, backend, agent_id):
            return ["handle"]

        def spawn_turn(self, backend, **kwargs):
            self.spawned.append(kwargs)

    registry = Registry()
    service = TurnDispatchService(
        SimpleNamespace(default_session="eu", agents_path=tmp_path / "x.json",
                        stream=SimpleNamespace(broadcast=lambda _e: None)),
        backend_registry=registry, home=tmp_path,
        uuid_factory=lambda: "codex-thread", now=lambda: 12.5,
        retry_scheduler=lambda _d, fn: fn())
    service.dispatch(text="Continue", requested_session="eu", trace_id="t",
                     synthesize_audio=synthesize_audio)
    return agent_id, registry, coordinator, recorded


def test_a_profile_turn_skips_account_switching_recovery_and_openai_usage(
        tmp_path, monkeypatch):
    monkeypatch.setattr(_td, "_RUNTIME_CLIENT", None)
    agent_id, registry, coordinator, recorded = _codex_dispatch(
        tmp_path, monkeypatch, profile="example-eu")
    recovered, remote = [], []
    monkeypatch.setattr(codex_app_server, "recover_usage_failure",
                        lambda message: recovered.append(message) or True)
    monkeypatch.setattr(_td.judgment_sites, "classify_error_or_unknown",
                        lambda text, local: remote.append(text) or local)

    [call] = registry.spawned
    assert call["provider_profile"] == "example-eu"
    assert call["model"] == "eu-model"
    call["on_error"]("You've hit your usage limit. Try again at 3:29 PM.")

    assert recovered == []
    assert remote == []  # the error text never went to a remote classifier
    assert recorded == []
    assert not coordinator.recovering
    assert len(registry.spawned) == 1
    assert agents_db.latest_state(agent_id)["kind"] == AgentState.INTERRUPTED


def test_a_default_codex_turn_still_carries_no_profile_and_still_switches_accounts(
        tmp_path, monkeypatch):
    monkeypatch.setattr(_td, "_RUNTIME_CLIENT", None)
    _agent_id, registry, coordinator, _recorded = _codex_dispatch(
        tmp_path, monkeypatch, profile="")
    monkeypatch.setattr(codex_app_server, "recover_usage_failure", lambda _m: False)

    [call] = registry.spawned
    assert "provider_profile" not in call
    call["on_error"]("You've hit your usage limit. Try again at 3:29 PM.")

    assert coordinator.recovering


# --- 4. the profile persists on the agent ------------------------------------

def test_the_profile_is_written_with_the_agent_row_and_is_immutable(profile_env):
    agent = _agent("eu", profile="example-eu")
    codex_profiles.clear_cache()
    assert codex_profiles.for_agent(agent) == "example-eu"
    assert codex_profiles.for_agent(_agent("plain")) == ""

    agents_db.soft_delete(agent)
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):  # same session, no profile: trigger refuses
        agents_db.create_agent(persona="Eu", voice_id="V", cwd="/tmp", session="eu",
                               backend="codex")
    assert codex_profiles.for_agent(agent) == "example-eu"


def test_an_unconfigured_profile_never_creates_an_agent(profile_env):
    with pytest.raises(codex_profiles.ProfileUnavailable):
        agents_db.create_agent(persona="Nope", voice_id="V", cwd="/tmp", session="nope",
                               backend="codex", provider_profile="not-configured")
    assert agents_db.get_by_session("nope") is None  # the row rolled back with it


# --- transcripts, sessions and skills against the profile home -----------------

def _rollout(home, thread):
    path = home / "sessions" / "2026" / "10" / "11" / (
        f"rollout-2026-10-11T10-00-00-{thread}.jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"type": "session_meta", "payload": {
        "id": thread, "cwd": "/tmp"}}) + "\n" + json.dumps({"type": "event_msg", "payload": {
            "type": "user_message", "message": "hello"}}) + "\n")
    return path


def test_each_agent_finds_its_rollouts_only_in_its_own_home(profile_env):
    from lib import codex_transcript
    from lib.backend.registry import by_id
    eu_thread = "0b5e0a8c-1111-4222-8333-944445555666"
    plain_thread = "1c6f1b9d-1111-4222-8333-944445555666"
    eu, plain = _agent("eu", profile="example-eu"), _agent("plain")
    eu_rollout = _rollout(codex_profiles.prepare_home("example-eu"), eu_thread)
    plain_rollout = _rollout(profile_env.tmp / "default-codex-home", plain_thread)
    # Each home also holds a copy of the other's thread: neither may see it.
    _rollout(profile_env.tmp / "default-codex-home", eu_thread)
    _rollout(codex_profiles.home("example-eu"), plain_thread)
    agents_db.bind_backend_session(eu, eu_thread)
    agents_db.bind_backend_session(plain, plain_thread)

    assert codex_transcript.find_latest_jsonl(eu_thread) == eu_rollout
    assert codex_transcript.find_latest_jsonl(plain_thread) == plain_rollout
    assert by_id("codex").model_transcript(eu_thread) == eu_rollout
    assert by_id("codex").model_transcript(plain_thread) == plain_rollout
    # An unbound thread is the default account's: a profile home is never searched.
    loose = "2d7a2cae-1111-4222-8333-944445555666"
    _rollout(codex_profiles.home("example-eu"), loose)
    assert codex_transcript.find_latest_jsonl(loose) is None


def test_the_profile_home_links_clarps_managed_codex_skills(profile_env, monkeypatch):
    skills = profile_env.tmp / "managed-codex-skills"
    skills.mkdir()
    monkeypatch.setattr(codex_profiles.managed_skills, "CODEX_SKILLS", skills)

    home = codex_profiles.prepare_home("example-eu")

    assert (home / "skills").resolve() == skills.resolve()


# --- selectable creation (contract 63) -----------------------------------------

def test_model_options_offer_the_profile_with_its_own_models(profile_env, monkeypatch):
    from lib import provider_capabilities
    monkeypatch.setattr(provider_capabilities, "capability_catalog",
                        lambda **_kw: {"providers": {"codex": {"models": []},
                                                     "claude": {"models": []}}})

    response = provider_capabilities.endpoint_response()

    [profile] = response["providers"]["codex"]["provider_profiles"]
    assert profile["id"] == "example-eu"
    assert profile["label"] == "Codex (EU, example)"
    assert codex_profiles.OPERATING_RULE in profile["detail"]
    assert profile["models"][0]["supported_efforts"] == ["low", "medium", "high", "xhigh"]
    assert profile["available"] is True
    assert [m["id"] for m in profile["models"]] == ["eu-model"]
    assert "provider_profiles" not in response["providers"]["claude"]


def _lifecycle_ctx(tmp_path):
    return SimpleNamespace(
        agents_path=tmp_path / "unused.json",
        stream=SimpleNamespace(broadcast=lambda _event: None),
        speak_announcement=lambda *_a, **_kw: None)


def _create(tmp_path, **body):
    from lib.agent_lifecycle import AgentLifecycleService
    return AgentLifecycleService(_lifecycle_ctx(tmp_path)).create(
        {"name": "Eu", "cwd": str(tmp_path), "voice_id": "V", "backend": "codex",
         **body})


def test_creating_a_profile_agent_stores_the_profile_and_its_model(profile_env):
    result = _create(profile_env.tmp, session="eu", provider_profile="example-eu")

    row = agents_db.get_by_session(result.session)
    assert codex_profiles.for_agent(row["agent_id"]) == "example-eu"
    assert row["model"] == "eu-model"
    [shown] = [a for a in snapshot.build_agent_snapshot(None)["agents"]
               if a["session"] == "eu"]
    assert shown["provider_profile"] == "example-eu"


def test_a_plain_codex_agent_is_created_exactly_as_before(profile_env):
    result = _create(profile_env.tmp, session="plain")

    row = agents_db.get_by_session(result.session)
    assert codex_profiles.for_agent(row["agent_id"]) == ""
    [shown] = [a for a in snapshot.build_agent_snapshot(None)["agents"]
               if a["session"] == "plain"]
    assert shown["provider_profile"] == ""


@pytest.mark.parametrize("body,code", [
    ({"backend": "claude", "provider_profile": "example-eu"},
     "provider_profile requires the codex backend"),
    ({"provider_profile": "azure-us"}, "unknown provider_profile"),
    ({"provider_profile": "example-eu", "model": "gpt-6.1-sol"},
     "invalid model for provider profile"),
    ({"provider_profile": "example-eu", "effort": "ultra"},
     "invalid effort for provider profile"),
])
def test_creation_refuses_what_the_profile_cannot_run(profile_env, body, code):
    from lib.agent_lifecycle import AgentLifecycleError
    with pytest.raises(AgentLifecycleError) as refused:
        _create(profile_env.tmp, session="eu", **body)
    assert refused.value.code == code
    assert agents_db.get_by_session("eu") is None


def test_a_relaunch_keeps_the_profile_and_cannot_change_it_or_the_backend(profile_env):
    from lib.agent_lifecycle import AgentLifecycleError
    _create(profile_env.tmp, session="eu", provider_profile="example-eu")
    _create(profile_env.tmp, session="plain", name="Plain")

    for session, body in (("eu", {"backend": "claude"}),
                          ("eu", {"provider_profile": "nope"}),
                          ("plain", {"provider_profile": "example-eu", "name": "Plain"})):
        with pytest.raises(AgentLifecycleError) as refused:
            _create(profile_env.tmp, replace_sid=session, **body)
        assert refused.value.code == "provider_profile_locked"
    _create(profile_env.tmp, replace_sid="eu")
    assert codex_profiles.for_agent(agents_db.get_by_session("eu")["agent_id"]) == "example-eu"


def test_a_conversation_is_only_resumed_inside_the_profile_that_owns_it(profile_env):
    from lib.agent_lifecycle import AgentLifecycleError
    owned = "0b5e0a8c-1111-4222-8333-944445555666"
    _rollout(codex_profiles.prepare_home("example-eu"), owned)
    _create(profile_env.tmp, session="eu", provider_profile="example-eu",
            resume_session_id=owned)          # its own home holds it

    with pytest.raises(AgentLifecycleError) as into_default:
        _create(profile_env.tmp, session="plain", name="Plain", resume_session_id=owned)
    with pytest.raises(AgentLifecycleError) as into_profile:
        _create(profile_env.tmp, session="eu2", name="Eu2", provider_profile="example-eu",
                resume_session_id="aaaaaaaa-1111-4222-8333-944445555666")

    assert into_default.value.code == "provider_profile_mismatch"  # a profile agent bound it
    assert into_profile.value.code == "provider_profile_mismatch"  # not in its own home


def test_a_fork_stays_on_its_parents_profile(profile_env, monkeypatch):
    from lib import agent_fork
    from lib.backend import codex as codex_backend
    parent = _create(profile_env.tmp, session="eu", provider_profile="example-eu")
    parent_id = agents_db.get_by_session("eu")["agent_id"]
    agents_db.bind_backend_session(parent_id, "parent-thread")
    forks = []

    def fake_fork(self, session_id, *, source_cwd, cwd, provider_profile=""):
        forks.append(provider_profile)
        home = codex_profiles.prepare_home(provider_profile)
        (home / "sessions").mkdir(exist_ok=True)
        (home / "sessions" / "rollout-x-child-thread.jsonl").write_text("{}\n")
        return "child-thread"

    monkeypatch.setattr(codex_backend.CodexBackend, "fork_conversation", fake_fork)
    sent = []
    ctx = _lifecycle_ctx(profile_env.tmp)

    result = agent_fork.fork(ctx, parent.session, {"children": [
        {"name": "same", "task": "t", "cwd": str(profile_env.tmp)},
        {"name": "claude", "task": "t", "cwd": str(profile_env.tmp), "backend": "claude"},
        {"name": "openai", "task": "t", "cwd": str(profile_env.tmp),
         "provider_profile": "other"},
    ]}, dispatch=sent.append)

    same, to_claude, to_other = result["children"]
    assert forks == ["example-eu"]
    assert same["fork"] == "native"
    child = agents_db.get_by_session(same["session"])
    assert codex_profiles.for_agent(child["agent_id"]) == "example-eu"
    assert to_claude["error"] == to_other["error"] == "provider_profile_mismatch"
    assert len(sent) == 1


def test_a_default_parent_cannot_fork_into_the_profile(profile_env):
    from lib import agent_fork
    parent = _create(profile_env.tmp, session="plain")
    agents_db.bind_backend_session(
        agents_db.get_by_session("plain")["agent_id"], "plain-thread")

    result = agent_fork.fork(_lifecycle_ctx(profile_env.tmp), parent.session, {
        "children": [{"name": "eu", "task": "t", "cwd": str(profile_env.tmp),
                      "provider_profile": "example-eu"}]}, dispatch=lambda _c: None)

    assert result["children"][0]["error"] == "provider_profile_mismatch"


# --- the configuration -----------------------------------------------------------

def _valid(**changes):
    from tests.unit.codex_profile_fakes import FAKE_CODEX_PROFILE
    table = dict(FAKE_CODEX_PROFILE)
    table.update(changes)
    return {key: value for key, value in table.items() if value is not None}


@pytest.mark.parametrize("table", [
    _valid(api_key="0123456789abcdef0123456789abcdef"),          # a literal secret field
    _valid(token="anything"),
    _valid(password="hunter2"),
    _valid(base_url="http://example.invalid/openai/v1"),           # not https
    _valid(base_url="https://example.invalid/v1?api-key=x"),       # credentials in the URL
    _valid(base_url="https://user:pw@example.invalid/v1"),
    _valid(label="sk-proj-abcdefghijklmnop"),                      # secret-looking literal
    _valid(keyring_key="0123456789abcdef0123456789abcdef"),
    _valid(wire_api="chat"),
    _valid(key_header="Authorization"),
    _valid(models=[{"id": "eu-model", "label": "EU", "efforts": ["turbo"]}]),
    _valid(models=[{"id": "eu-model", "label": "EU", "efforts": [], "secret": "x"}]),
    _valid(default_model="not-listed"),
    _valid(models=None),                                           # missing
])
def test_a_profile_table_is_validated_strictly(table, monkeypatch, capsys):
    from dataclasses import replace
    from lib import config
    real = config.load
    monkeypatch.setattr(config, "load", lambda *a, **k: replace(
        real(*a, **k), codex_profiles={"bad-eu": table}))
    codex_profiles.clear_cache()

    assert codex_profiles.profiles() == {}
    assert codex_profiles.catalog() == []
    for value in table.values():
        if isinstance(value, str) and len(value) > 12:
            assert value not in capsys.readouterr().err  # never logs a value


def test_a_valid_profile_is_offered_only_while_its_key_is_in_the_keyring(profile_env):
    assert codex_profiles.available("example-eu") is True
    profile_env.secret_tool.write_text("#!/bin/sh\nexit 1\n")
    codex_profiles.clear_cache()
    assert codex_profiles.available("example-eu") is False
    assert codex_profiles.available("not-configured") is False


# --- no OpenAI credential ever in the profile home ---------------------------------

def test_the_profile_home_never_holds_or_inherits_an_openai_credential(profile_env, monkeypatch):
    default_home = profile_env.tmp / "default-codex-home"
    default_home.mkdir()
    (default_home / "auth.json").write_text('{"tokens": "openai"}')
    monkeypatch.setenv("CODEX_API_KEY", "sk-codex-must-not-pass")
    agent = _agent("eu", profile="example-eu")

    _run_turn(agent, "eu", profile="example-eu")

    home = codex_profiles.home("example-eu")
    assert not (home / "auth.json").exists()
    assert not any(p.resolve() == (default_home / "auth.json").resolve()
                   for p in home.rglob("*") if p.is_symlink())
    [spawn] = profile_env.spawned
    assert not [name for name in spawn["env"]
                if name.startswith(("OPENAI_", "CODEX_")) and name != "CODEX_HOME"]

    (home / "auth.json").write_text("{}")   # someone copied one in
    codex_app_server.recycle_clients()
    for client in list(codex_app_server._CLIENTS.values()):
        client.shutdown()
    codex_app_server._CLIENTS.clear()
    with pytest.raises(codex_profiles.ProfileUnavailable):
        codex_app_server.spawn_turn(text="hi", cwd=Path("/tmp"), session="eu",
                                    agent_id=agent, is_new_session=True,
                                    provider_profile="example-eu")


# --- no fallback of any kind ------------------------------------------------------

@pytest.mark.parametrize("mode", ["usage", "leak", "drop"])
def test_a_failing_profile_turn_never_falls_back_to_another_account_or_backend(
        profile_env, monkeypatch, mode):
    """Usage limit, auth failure and a dropped connection: every attempt runs
    on the profile's own app-server; nothing reaches the default Codex
    account, another backend or the account switcher."""
    from lib import backends
    monkeypatch.setattr(_td, "_RUNTIME_CLIENT", None)
    monkeypatch.setenv("FAKE_MODE", mode)
    for other in backends.ids():
        if other != "codex":
            monkeypatch.setattr(backends.by_id(other), "spawn_turn",
                                lambda **_k: pytest.fail("another backend got the turn"))
    from dataclasses import replace
    from unittest.mock import Mock
    from lib.account_failover import AccountFailover
    cfg = replace(_td.config.load(), codex_account_switch_command=("codex-selector",))
    monkeypatch.setattr(_td.config, "load", lambda *a, **k: cfg)
    coordinator = AccountFailover(_td._TURN_LOCK, switch=Mock(return_value=True),
                                  schedule=lambda _d, fn: fn(), now=lambda: 100)
    coordinator.register = Mock(side_effect=AssertionError("registered a profile turn"))
    coordinator.request = Mock(side_effect=AssertionError("asked to switch accounts"))
    monkeypatch.setitem(_td._FAILOVERS, "codex", coordinator)
    switched = []
    monkeypatch.setattr(_td.backend_usage, "record_classified_usage_limit",
                        lambda provider: switched.append(provider))
    agent = _agent("eu", profile="example-eu")
    service = TurnDispatchService(
        SimpleNamespace(default_session="eu", agents_path=profile_env.tmp / "x.json",
                        stream=SimpleNamespace(broadcast=lambda _e: None)),
        home=profile_env.tmp, retry_scheduler=lambda _d, fn: fn())

    service.dispatch(text=f"sentinel turn {SENTINEL[:12]}", requested_session="eu",
                     trace_id="t-eu", synthesize_audio=False)
    deadline = time.monotonic() + 20
    while agents_db.latest_state(agent)["kind"] != AgentState.INTERRUPTED:
        assert time.monotonic() < deadline, "the failed turn never ended"
        time.sleep(0.05)

    assert profile_env.spawned
    assert all(spawn["env"].get("CODEX_HOME", "").endswith("example-eu")
               for spawn in profile_env.spawned)
    assert codex_app_server._SHARED_KEY not in codex_app_server._CLIENTS
    assert switched == []
    coordinator.register.assert_not_called()
    coordinator.request.assert_not_called()
    coordinator.switch.assert_not_called()


# --- B. transcript lookup per agent ------------------------------------------------

def test_a_default_agent_never_finds_a_thread_that_only_a_profile_home_holds(profile_env):
    from lib import codex_transcript
    from lib.backend.registry import by_id
    plain, eu = _agent("plain"), _agent("eu", profile="example-eu")
    only_there = "3e8b2dbf-1111-4222-8333-944445555666"
    _rollout(codex_profiles.prepare_home("example-eu"), only_there)
    plain_only = "4f9c3ec0-1111-4222-8333-944445555666"
    _rollout(profile_env.tmp / "default-codex-home", plain_only)

    assert codex_transcript.find_latest_jsonl(only_there, agent=plain) is None
    assert codex_transcript.find_latest_jsonl(only_there, agent=eu) is not None
    assert codex_transcript.find_latest_jsonl(plain_only, agent=eu) is None
    assert by_id("codex").model_transcript(only_there, agent=plain) is None
    assert by_id("codex").recorded_model(only_there, agent=plain) == ""


# --- C. text-only and the provider-profile boundary ------------------------------

def test_a_profile_agents_turn_never_asks_for_or_synthesizes_audio(tmp_path, monkeypatch):
    from lib import tts_queue
    monkeypatch.setattr(_td, "_RUNTIME_CLIENT", None)
    monkeypatch.setattr(tts_queue, "conn", lambda: pytest.fail("a TTS request was queued"))
    agent_id, registry, _c, _r = _codex_dispatch(tmp_path, monkeypatch, profile="example-eu",
                                                 synthesize_audio=True)

    [call] = registry.spawned
    assert call["synthesize_audio"] is False and call["voice_preamble"] is False
    # Its <speak> text, should a reply still carry one, reaches no TTS vendor.
    assert tts_queue.enqueue(agent_id=agent_id, text="<speak>hi</speak>", voice_id="V",
                             session="eu", source="test", synthesize_audio=True) == 0


def _peer(session, profile=""):
    agent_id = agents_db.create_agent(persona=session.title(), voice_id="V", cwd="/tmp",
                                      session=session, backend="codex",
                                      **({"provider_profile": profile} if profile else {}))
    agents_db.start_runtime(agent_id, session)
    return agent_id


def test_a_profile_agent_cannot_post_to_an_agent_outside_its_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(_td, "_RUNTIME_CLIENT", None)
    eu, eu_peer, plain = _peer("eu", "example-eu"), _peer("euan", "example-eu"), _peer("plain")
    spawned = []
    registry = SimpleNamespace(
        CODEX="codex", normalize=lambda b: b or "claude",
        interrupt=lambda *a: 0, active_handles=lambda *a: [],
        spawn_turn=lambda backend, **kw: spawned.append(kw["agent_id"]))
    service = TurnDispatchService(
        SimpleNamespace(default_session="plain", agents_path=tmp_path / "x.json",
                        stream=SimpleNamespace(broadcast=lambda _e: None)),
        backend_registry=registry, home=tmp_path, retry_scheduler=lambda _d, fn: fn())

    def post(sender, to, cid):
        return service.dispatch(text=f"note {cid}", requested_session=to, trace_id=cid,
                                client_msg_id=cid, forced_session=to, origin="agent",
                                sender_agent_id=sender, synthesize_audio=False)

    with pytest.raises(_td.DispatchError) as refused:
        post(eu, "plain", "out")
    assert refused.value.status == 403 and "provider_profile_boundary" in str(refused.value)
    post(plain, "eu", "in")            # inbound to the EU agent stays allowed
    post(eu, "euan", "same")           # same profile is allowed
    assert spawned == [eu, eu_peer]


def test_a_profile_agent_cannot_reply_to_a_request_from_outside_its_profile(monkeypatch):
    from lib import peer_requests
    eu, plain = _peer("eu", "example-eu"), _peer("plain")
    monkeypatch.setattr(peer_requests, "find_request",
                        lambda replier, rid: agents_db.get_by_agent_id(plain))

    with pytest.raises(peer_requests.RequestError, match="provider_profile_boundary"):
        peer_requests.plan_reply(agents_db.get_by_agent_id(eu), "r0123456789ab",
                                 "result", "the EU answer")


# --- C. direct posts: the turn's own identity or the claimed sender ------------------

def _send_request(session, *, caller="", sender=""):
    from lib.send_request import SendRequest
    return SendRequest.from_payload(
        {"text": "status: done", "session": session,
         **({"sender": sender} if sender else {})},
        default_session="plain", trace_id_factory=lambda: "t", authenticated=True,
        resolve_sender=lambda raw: (agents_db.get_by_session(raw) or {}).get("agent_id", ""),
        caller_agent_id=caller)


def test_an_eu_agents_turn_cannot_send_to_an_agent_outside_its_profile():
    from lib.send_request import SendRequestError
    eu, eu_peer, plain = _peer("eu", "example-eu"), _peer("euan", "example-eu"), _peer("plain")
    plain_peer = _peer("pia")

    for caller, sender in ((eu, ""), (eu, "eu"), ("", "eu")):  # turn identity or claim
        with pytest.raises(SendRequestError) as refused:
            _send_request("plain", caller=caller, sender=sender)
        assert refused.value.status == 403
        assert str(refused.value) == "provider_profile_boundary"

    allowed = _send_request("euan", caller=eu)                 # same profile
    assert allowed.session == "euan" and allowed.force_session  # never re-routed
    assert _send_request("eu", caller=plain).session == "eu"   # inbound stays allowed
    assert _send_request("pia", caller=plain, sender="plain").session == "pia"  # control
    del eu_peer, plain_peer


def test_a_group_call_cannot_carry_an_eu_agents_message_outside_its_profile():
    from dataclasses import replace
    from lib.send_request import SendRequestError, boundary_refusal
    eu, plain = _peer("eu", "example-eu"), _peer("plain")
    _peer("euan", "example-eu")
    request = _send_request("euan", caller=eu)

    assert boundary_refusal(request, caller_agent_id=eu) is None
    refused = boundary_refusal(replace(request, session="plain"), caller_agent_id=eu)
    assert isinstance(refused, SendRequestError) and refused.status == 403
    assert boundary_refusal(replace(request, session="plain"), caller_agent_id=plain) is None


def test_the_parent_still_sees_an_eu_agents_status_in_the_snapshot(profile_env, monkeypatch):
    import importlib.util
    from lib import snapshot
    eu, plain = _peer("eu", "example-eu"), _peer("plain")
    from lib import provider_background_jobs as jobs
    token, _ = jobs.identity_env(plain)
    jobs.record_identity(token, agent_id=plain, provider="codex", pid=1)
    monkeypatch.setattr(snapshot.SHARED, "body", lambda _ctx: json.dumps({"agents": [
        {"agent_id": eu, "session": "eu", "busy": True, "latest_state": "tool",
         "helper_state": "running", "queued_turn_count": 2,
         "background_jobs": {"count": 1, "sub_agents": 0},
         "goal": {"status": "active", "objective": SENTINEL},
         "last_message": SENTINEL}]}).encode())
    spec = importlib.util.spec_from_file_location(
        "clarp_server_status", Path(__file__).resolve().parents[2] / "server/server.py")
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)
    handler = object.__new__(server.Handler)
    handler.path, handler.headers = "/agents/snapshot", {"X-Clarp-Turn": token}
    handler.server = SimpleNamespace(ctx=SimpleNamespace())
    sent = []
    handler._send = lambda status, body, *_a, **_k: sent.append(json.loads(body))

    handler._handle_snapshot()

    [row] = sent[0]["agents"]
    assert SENTINEL not in json.dumps(row)
    assert (row["busy"], row["latest_state"], row["helper_state"], row["queued_turn_count"],
            row["background_jobs"], row["goal"]) == (
        True, "tool", "running", 2, {"count": 1, "sub_agents": 0}, {"status": "active"})


# --- M1. a slow keyring never stalls the other Codex agents ------------------------

def test_a_slow_keyring_read_never_holds_the_app_server_lock(profile_env):
    """The keyring read (up to 15 s) happens before the pool's global lock:
    while a profile's process waits for it, default agents still steer and
    interrupt at once."""
    tool = profile_env.secret_tool
    tool.write_text("#!/bin/sh\nsleep 2\n" + "\n".join(tool.read_text().splitlines()[1:]) + "\n")
    eu, plain = _agent("eu", profile="example-eu"), _agent("plain")
    started = threading.Thread(target=lambda: _run_turn(eu, "eu", profile="example-eu"),
                               daemon=True)
    started.start()
    time.sleep(0.4)  # the profile spawn is now inside the keyring read

    began = time.monotonic()
    codex_app_server.interrupt(plain)
    codex_app_server.active_handles(plain)
    waited = time.monotonic() - began
    started.join(15)

    assert waited < 0.5, f"a default agent waited {waited:.1f}s for the keyring"
    assert profile_env.spawned and profile_env.spawned[0]["env"]["CODEX_HOME"].endswith("example-eu")


# --- L2. the profile process inherits no other vendor's credentials or proxy ----------

_VENDOR_ENV = {
    "ANTHROPIC_API_KEY": "s1", "CLAUDE_CODE_OAUTH_TOKEN": "s2", "GEMINI_API_KEY": "s3",
    "GOOGLE_API_KEY": "s4", "GOOGLE_APPLICATION_CREDENTIALS": "/s5.json",
    "ELEVENLABS_API_KEY": "s6", "ELEVEN_API_KEY": "s7", "CARTESIA_API_KEY": "s8",
    "DEEPGRAM_API_KEY": "s9", "FIREWORKS_API_KEY": "s10", "GROQ_API_KEY": "s11",
    "XAI_API_KEY": "s12", "GROK_API_KEY": "s13", "OPENROUTER_API_KEY": "s14",
    "GITHUB_TOKEN": "s15", "SOME_VENDOR_API_KEY": "s16", "TYPESAFE_TOKEN": "s17",
    "OPENAI_API_KEY": "s18", "CODEX_API_KEY": "s19", "AZURE_OPENAI_API_KEY": "s20",
    "HTTP_PROXY": "http://p", "https_proxy": "http://p", "ALL_PROXY": "socks://p",
    "NO_PROXY": "localhost",
}


def test_the_profile_app_server_inherits_no_other_vendor_credentials_or_proxies(
        profile_env, monkeypatch):
    for name, value in _VENDOR_ENV.items():
        monkeypatch.setenv(name, f"SENTINEL-VENDOR-{value}")
    monkeypatch.setenv("CLARP_HARMLESS_SETTING", "kept")
    plain, eu = _agent("plain"), _agent("eu", profile="example-eu")

    _run_turn(plain, "plain")
    _run_turn(eu, "eu", profile="example-eu")

    default_env, profile_spawn_env = (spawn["env"] for spawn in profile_env.spawned)
    assert not [name for name in profile_spawn_env if "SENTINEL-VENDOR" in profile_spawn_env[name]]
    assert profile_spawn_env["CLARP_CODEX_PROFILE_API_KEY"] == SENTINEL      # its own key
    assert profile_spawn_env["CODEX_HOME"].endswith("example-eu")
    assert profile_spawn_env["CLARP_HARMLESS_SETTING"] == "kept"
    assert profile_spawn_env["PATH"]
    # Control: the default account's process keeps today's environment.
    assert all(default_env[name] == f"SENTINEL-VENDOR-{value}"
               for name, value in _VENDOR_ENV.items())


# --- L3. the status-only snapshot view carries no path ----------------------------

def test_the_status_only_snapshot_view_carries_no_path(profile_env, monkeypatch):
    import importlib.util
    from lib import snapshot
    from lib import provider_background_jobs as jobs
    eu, plain = _peer("eu", "example-eu"), _peer("plain")
    token, _ = jobs.identity_env(plain)
    jobs.record_identity(token, agent_id=plain, provider="codex", pid=1)
    path = f"/home/x/cases/{SENTINEL[:16]}-customer"
    monkeypatch.setattr(snapshot.SHARED, "body", lambda _ctx: json.dumps({"agents": [
        {"agent_id": eu, "session": "eu", "busy": True, "latest_state": "tool", "cwd": path},
        {"agent_id": plain, "session": "plain", "cwd": "/home/x/plainword"}]}).encode())
    spec = importlib.util.spec_from_file_location(
        "clarp_server_l3", Path(__file__).resolve().parents[2] / "server/server.py")
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)
    sent = []
    for turn in (token, ""):
        handler = object.__new__(server.Handler)
        handler.path = "/agents/snapshot"
        handler.headers = {"X-Clarp-Turn": turn} if turn else {}
        handler.server = SimpleNamespace(ctx=SimpleNamespace())
        handler._send = lambda status, body, *_a, **_k: sent.append(body.decode())
        handler._handle_snapshot()

    as_agent, as_user = sent
    assert SENTINEL[:16] not in as_agent and "cwd" not in json.loads(as_agent)["agents"][0]
    assert "/home/x/plainword" in as_agent          # control: an ordinary row is whole
    assert SENTINEL[:16] in as_user                  # the user's device
