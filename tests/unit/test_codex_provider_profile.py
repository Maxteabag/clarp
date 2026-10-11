"""The azure-eu Codex provider profile: one Codex agent's turns go to Azure
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

SENTINEL = "sk-SENTINEL-azure-0f1e2d3c4b5a69788796a5b4c3d2e1f0"

_FAKE_APP_SERVER = r'''
import json, os, sys, uuid
key = os.environ.get("AZURE_OPENAI_API_KEY", "")
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
        if mode == "leak":
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
def profile_env(tmp_path, monkeypatch):
    """A fake keyring with the sentinel key, a private profile root, the fake
    app-server and an empty pool."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    secret_tool = bindir / "secret-tool"
    secret_tool.write_text(
        "#!/bin/sh\n"
        "[ \"$*\" = \"lookup service psa-reverify key azure-openai\" ] || exit 1\n"
        f"printf '%s\\n' '{SENTINEL}'\n")
    secret_tool.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("CLARP_CODEX_PROFILES_DIR", str(tmp_path / "profiles"))
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
    yield SimpleNamespace(tmp=tmp_path, spawned=spawned)
    for client in list(codex_app_server._CLIENTS.values()):
        client.shutdown()
    codex_app_server._CLIENTS.clear()
    eventlog.reset_for_tests()


def _agent(session, *, profile="", cwd="/tmp"):
    agent_id = agents_db.create_agent(
        persona=session.title(), voice_id="V", cwd=cwd, session=session,
        backend="codex")
    agents_db.start_runtime(agent_id, session)
    if profile:
        codex_profiles.assign(agent_id, profile)
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
    agent = _agent("eu", profile="azure-eu")

    assert "result" in _run_turn(agent, "eu", profile="azure-eu")

    [spawn] = profile_env.spawned
    home = profile_env.tmp / "profiles" / "azure-eu"
    assert spawn["env"]["CODEX_HOME"] == str(home)
    assert spawn["env"]["AZURE_OPENAI_API_KEY"] == SENTINEL
    assert "OPENAI_API_KEY" not in spawn["env"]
    assert SENTINEL not in " ".join(spawn["argv"])
    assert stat.S_IMODE(home.stat().st_mode) == 0o700
    config = tomllib.loads((home / "config.toml").read_text())
    assert config["model"] == "sol61-eu"
    assert config["model_provider"] == "azure-eu"
    assert config["web_search"] == "disabled"
    assert config["check_for_update_on_startup"] is False
    assert config["analytics"] == {"enabled": False}
    assert config["feedback"] == {"enabled": False}
    provider = config["model_providers"]["azure-eu"]
    assert provider["base_url"] == (
        "https://irisbot-ticketmaster-ai-sc.openai.azure.com/openai/v1")
    assert provider["wire_api"] == "responses"
    assert provider["env_http_headers"] == {"api-key": "AZURE_OPENAI_API_KEY"}
    assert provider["requires_openai_auth"] is False
    assert provider["supports_websockets"] is False


def test_the_key_never_reaches_logs_eventlog_db_status_or_error_text(
        profile_env, monkeypatch, capsys):
    """The provider echoes the key on stderr and in its error; the real
    dispatcher records the failure everywhere it records failures."""
    monkeypatch.setattr(_td, "_RUNTIME_CLIENT", None)
    monkeypatch.setenv("FAKE_MODE", "leak")
    agent = _agent("eu", profile="azure-eu")
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
        codex_app_server._client(agent, "eu", profile="azure-eu").request(
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
    (profile_env.tmp / "bin" / "secret-tool").write_text("#!/bin/sh\nexit 1\n")
    codex_profiles.clear_cache()
    agent = _agent("eu", profile="azure-eu")

    with pytest.raises(codex_profiles.ProfileUnavailable) as failure:
        codex_app_server.spawn_turn(
            text="hi", cwd=Path("/tmp"), session="eu", agent_id=agent,
            is_new_session=True, provider_profile="azure-eu")

    assert "keyring" in str(failure.value)
    assert profile_env.spawned == []          # never started a process at all


# --- 3. pools and thread locks are per profile; cross-profile is refused --------

def test_profiles_get_separate_app_servers_and_each_agent_finds_its_own(profile_env):
    plain = _agent("plain")
    eu = _agent("eu", profile="azure-eu")

    _run_turn(plain, "plain")
    _run_turn(eu, "eu", profile="azure-eu")

    assert set(codex_app_server._CLIENTS) == {
        codex_app_server._SHARED_KEY, "profile:azure-eu"}
    shared = codex_app_server._CLIENTS[codex_app_server._SHARED_KEY]
    profiled = codex_app_server._CLIENTS["profile:azure-eu"]
    assert shared.proc.pid != profiled.proc.pid
    assert codex_app_server._client_for_agent(eu) is profiled
    assert codex_app_server._client_for_agent(plain) is shared


@pytest.mark.parametrize("stored,requested", [("azure-eu", ""), ("", "azure-eu")])
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
                                          provider_profile="azure-eu")

    assert forked == "forked-thread-1"
    [spawn] = profile_env.spawned
    assert spawn["env"]["CODEX_HOME"].endswith("azure-eu")


def test_usage_recovery_and_rate_limits_never_treat_the_profile_as_an_openai_account(
        profile_env, monkeypatch):
    agent = _agent("eu", profile="azure-eu")
    client = codex_app_server._client(agent, "eu", profile="azure-eu")
    captured = []
    monkeypatch.setattr(codex_app_server.backend_usage, "capture_codex_rate_limits",
                        lambda params: captured.append(params) or {})

    client._notification("account/rateLimits/updated", {"rateLimits": {}})
    failure = codex_app_server.CodexTurnFailure("usage limit", client, False)

    assert captured == []
    assert codex_app_server.recover_usage_failure(failure) is False
    assert codex_app_server._CLIENTS["profile:azure-eu"] is client  # not recycled


def _codex_dispatch(tmp_path, monkeypatch, *, profile):
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
                     synthesize_audio=False)
    return agent_id, registry, coordinator, recorded


def test_a_profile_turn_skips_account_switching_recovery_and_openai_usage(
        tmp_path, monkeypatch):
    monkeypatch.setattr(_td, "_RUNTIME_CLIENT", None)
    agent_id, registry, coordinator, recorded = _codex_dispatch(
        tmp_path, monkeypatch, profile="azure-eu")
    recovered, remote = [], []
    monkeypatch.setattr(codex_app_server, "recover_usage_failure",
                        lambda message: recovered.append(message) or True)
    monkeypatch.setattr(_td.judgment_sites, "classify_error_or_unknown",
                        lambda text, local: remote.append(text) or local)

    [call] = registry.spawned
    assert call["provider_profile"] == "azure-eu"
    assert call["model"] == "sol61-eu"
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

def test_the_profile_survives_a_runtime_restart_and_is_immutable(profile_env):
    agent = _agent("eu", profile="azure-eu")
    codex_profiles.clear_cache()

    assert codex_profiles.for_agent(agent) == "azure-eu"
    with pytest.raises(codex_profiles.ProfileMismatch):
        codex_profiles.assign(agent, "")
    assert codex_profiles.for_agent(_agent("plain")) == ""


# --- transcripts, sessions and skills against the profile home -----------------

def test_profile_rollouts_are_found_but_never_offered_for_adoption(profile_env):
    from lib import codex_transcript
    home = codex_profiles.prepare_home("azure-eu")
    rollout = home / "sessions" / "2026" / "10" / "11" / (
        "rollout-2026-10-11T10-00-00-0b5e0a8c-1111-4222-8333-944445555666.jsonl")
    rollout.parent.mkdir(parents=True)
    rollout.write_text(json.dumps({"type": "session_meta", "payload": {
        "id": "0b5e0a8c-1111-4222-8333-944445555666", "cwd": "/tmp"}}) + "\n"
        + json.dumps({"type": "event_msg", "payload": {
            "type": "user_message", "message": "hello"}}) + "\n")

    assert codex_transcript.find_latest_jsonl(
        "0b5e0a8c-1111-4222-8333-944445555666") == rollout
    assert codex_transcript.list_sessions("/tmp", all_projects=True) == []
    assert codex_profiles.owner_of_thread("0b5e0a8c-1111-4222-8333-944445555666") == "azure-eu"


def test_the_profile_home_links_clarps_managed_codex_skills(profile_env, monkeypatch):
    skills = profile_env.tmp / "managed-codex-skills"
    skills.mkdir()
    monkeypatch.setattr(codex_profiles.managed_skills, "CODEX_SKILLS", skills)

    home = codex_profiles.prepare_home("azure-eu")

    assert (home / "skills").resolve() == skills.resolve()


# --- selectable creation (contract 63) -----------------------------------------

def test_model_options_offer_the_profile_with_its_own_models(profile_env, monkeypatch):
    from lib import provider_capabilities
    monkeypatch.setattr(provider_capabilities, "capability_catalog",
                        lambda **_kw: {"providers": {"codex": {"models": []},
                                                     "claude": {"models": []}}})

    response = provider_capabilities.endpoint_response()

    [profile] = response["providers"]["codex"]["provider_profiles"]
    assert profile["id"] == "azure-eu"
    assert profile["label"] == "Codex (EU, Azure)"
    assert profile["available"] is True
    assert [m["id"] for m in profile["models"]] == ["sol61-eu"]
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
    result = _create(profile_env.tmp, session="eu", provider_profile="azure-eu")

    row = agents_db.get_by_session(result.session)
    assert codex_profiles.for_agent(row["agent_id"]) == "azure-eu"
    assert row["model"] == "sol61-eu"
    [shown] = [a for a in snapshot.build_agent_snapshot(None)["agents"]
               if a["session"] == "eu"]
    assert shown["provider_profile"] == "azure-eu"


def test_a_plain_codex_agent_is_created_exactly_as_before(profile_env):
    result = _create(profile_env.tmp, session="plain")

    row = agents_db.get_by_session(result.session)
    assert codex_profiles.for_agent(row["agent_id"]) == ""
    [shown] = [a for a in snapshot.build_agent_snapshot(None)["agents"]
               if a["session"] == "plain"]
    assert shown["provider_profile"] == ""


@pytest.mark.parametrize("body,code", [
    ({"backend": "claude", "provider_profile": "azure-eu"},
     "provider_profile requires the codex backend"),
    ({"provider_profile": "azure-us"}, "unknown provider_profile"),
    ({"provider_profile": "azure-eu", "model": "gpt-6.1-sol"},
     "invalid model for provider profile"),
    ({"provider_profile": "azure-eu", "effort": "ultra"},
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
    _create(profile_env.tmp, session="eu", provider_profile="azure-eu")
    _create(profile_env.tmp, session="plain", name="Plain")

    for session, body in (("eu", {"backend": "claude"}),
                          ("eu", {"provider_profile": "nope"}),
                          ("plain", {"provider_profile": "azure-eu", "name": "Plain"})):
        with pytest.raises(AgentLifecycleError) as refused:
            _create(profile_env.tmp, replace_sid=session, **body)
        assert refused.value.code == "provider_profile_locked"
    _create(profile_env.tmp, replace_sid="eu")
    assert codex_profiles.for_agent(agents_db.get_by_session("eu")["agent_id"]) == "azure-eu"


def test_a_conversation_is_only_resumed_inside_the_profile_that_owns_it(profile_env):
    from lib.agent_lifecycle import AgentLifecycleError
    home = codex_profiles.prepare_home("azure-eu")
    owned = "0b5e0a8c-1111-4222-8333-944445555666"
    (home / "sessions").mkdir()
    (home / "sessions" / f"rollout-2026-10-11T10-00-00-{owned}.jsonl").write_text("{}\n")

    with pytest.raises(AgentLifecycleError) as into_default:
        _create(profile_env.tmp, session="plain", resume_session_id=owned)
    with pytest.raises(AgentLifecycleError) as into_profile:
        _create(profile_env.tmp, session="eu", provider_profile="azure-eu",
                resume_session_id="aaaaaaaa-1111-4222-8333-944445555666")

    assert into_default.value.code == "provider_profile_mismatch"
    assert into_profile.value.code == "provider_profile_mismatch"
    _create(profile_env.tmp, session="eu", provider_profile="azure-eu",
            resume_session_id=owned)


def test_a_fork_stays_on_its_parents_profile(profile_env, monkeypatch):
    from lib import agent_fork
    from lib.backend import codex as codex_backend
    parent = _create(profile_env.tmp, session="eu", provider_profile="azure-eu")
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
    assert forks == ["azure-eu"]
    assert same["fork"] == "native"
    child = agents_db.get_by_session(same["session"])
    assert codex_profiles.for_agent(child["agent_id"]) == "azure-eu"
    assert to_claude["error"] == to_other["error"] == "provider_profile_mismatch"
    assert len(sent) == 1


def test_a_default_parent_cannot_fork_into_the_profile(profile_env):
    from lib import agent_fork
    parent = _create(profile_env.tmp, session="plain")
    agents_db.bind_backend_session(
        agents_db.get_by_session("plain")["agent_id"], "plain-thread")

    result = agent_fork.fork(_lifecycle_ctx(profile_env.tmp), parent.session, {
        "children": [{"name": "eu", "task": "t", "cwd": str(profile_env.tmp),
                      "provider_profile": "azure-eu"}]}, dispatch=lambda _c: None)

    assert result["children"][0]["error"] == "provider_profile_mismatch"
