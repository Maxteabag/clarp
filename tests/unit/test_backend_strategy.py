"""The ``lib.backend`` strategy package (slices 1 to 3 of the contract in
docs/architecture/backend-strategy.md).

Every backend is a ``Backend``, ids and aliases round-trip through the
registry, and each method answers exactly what the host used to decide with
an adapter flag — or raises ``Unsupported`` where the CLI has no such mode.
"""
from __future__ import annotations

import pathlib
import sys
from types import SimpleNamespace

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))
from lib import backends  # noqa: E402
from lib.backend import registry  # noqa: E402
from lib.backend.base import Backend, CompactionStrategy, Unsupported  # noqa: E402
from lib.backend.claude import ClaudeBackend  # noqa: E402
from lib.backend.codex import CodexBackend  # noqa: E402
from lib.backend.deepseek import DeepSeekBackend  # noqa: E402
from lib.backend.opencode import OpenCodeBackend  # noqa: E402
from lib.backend.stream_json import StreamJsonBackend  # noqa: E402

IDS = ("claude", "codex", "agy", "grok", "opencode", "deepseek")


# --- registry ---------------------------------------------------------------

def test_every_backend_is_a_backend_in_registry_order():
    every = registry.all()
    assert all(isinstance(b, Backend) for b in every)
    assert tuple(b.id for b in every) == backends.ids() == IDS
    assert backends.all_backends() == every
    assert backends.by_id is registry.by_id and backends.for_agent is registry.for_agent


def test_singletons_are_shared():
    assert registry.by_id("codex") is registry.by_id("CODEX") is backends.by_id("codex")


def test_ids_and_aliases_round_trip_through_by_id_and_for_agent():
    for adapter in backends.adapters():
        for spelling in (adapter.id, adapter.id.upper(), f" {adapter.id} ", *adapter.aliases):
            assert registry.by_id(spelling).id == adapter.id, spelling
            assert registry.for_agent({"backend": spelling}).id == adapter.id, spelling
            assert registry.by_id(spelling).id == backends.normalize(spelling)
    assert registry.by_id("antigravity").id == "agy"
    assert registry.by_id("claude-code").id == backends.normalize("claude-code")
    # Unknown, empty and missing fall back the way normalize() does.
    for garbage in ("nonsense", "", None):
        assert registry.by_id(garbage).id == backends.DEFAULT
    assert registry.for_agent({}).id == backends.DEFAULT
    assert registry.for_agent(None).id == backends.DEFAULT


def test_class_shapes():
    assert isinstance(registry.by_id("claude"), ClaudeBackend)
    assert not isinstance(registry.by_id("claude"), StreamJsonBackend)
    for name in ("codex", "agy", "grok", "opencode", "deepseek"):
        assert isinstance(registry.by_id(name), StreamJsonBackend), name
    deepseek = registry.by_id("deepseek")
    assert isinstance(deepseek, DeepSeekBackend) and isinstance(deepseek, OpenCodeBackend)
    assert isinstance(registry.by_id("codex"), CodexBackend)


@pytest.mark.parametrize("backend", IDS)
def test_data_attributes_copy_the_adapter_row(backend):
    b = registry.by_id(backend)
    a = backends.get(backend)
    assert b.adapter is a
    for name in ("id", "label", "required_binary", "aliases", "efforts",
                 "context_window", "model_family", "janitor_default_model",
                 "api_providers", "login_kind", "effort_ui", "effort_scope",
                 "fallback_models", "runner"):
        assert getattr(b, name) == getattr(a, name), name


# --- slice 2: decisions that are methods, not flags --------------------------

TERMINAL_ARGV = {
    "claude": (["claude", "--dangerously-skip-permissions", "--resume", "s-1"],
               ["claude", "--dangerously-skip-permissions"]),
    "codex": (["codex", "resume", "s-1"], ["codex"]),
    "agy": (["agy", "--dangerously-skip-permissions", "--conversation", "s-1"],
            ["agy", "--dangerously-skip-permissions"]),
}


@pytest.mark.parametrize("backend", IDS)
def test_terminal_argv_per_backend_or_unsupported(monkeypatch, backend):
    from lib import deployment
    monkeypatch.setattr(deployment, "plugin_dir", lambda: None)
    b = registry.by_id(backend)
    if backend not in TERMINAL_ARGV:
        for sid in ("s-1", ""):
            with pytest.raises(Unsupported, match=f"no interactive terminal for the {backend} backend"):
                b.terminal_argv(sid)
        return
    resume, fresh = TERMINAL_ARGV[backend]
    assert b.terminal_argv("s-1") == resume
    assert b.terminal_argv("") == fresh


def test_claude_terminal_argv_appends_the_plugin_dir_itself(monkeypatch):
    from lib import deployment
    monkeypatch.setattr(deployment, "plugin_dir", lambda: pathlib.Path("/plug"))
    claude = registry.by_id("claude")
    assert claude.terminal_argv("s-1") == TERMINAL_ARGV["claude"][0] + ["--plugin-dir", "/plug"]
    assert claude.terminal_argv("") == TERMINAL_ARGV["claude"][1] + ["--plugin-dir", "/plug"]
    for name in ("codex", "agy"):
        assert "--plugin-dir" not in registry.by_id(name).terminal_argv("s-1"), name


def test_account_pool_per_backend():
    assert {b.id: b.account_pool() for b in registry.all()} == {
        "claude": "claude", "codex": "codex", "agy": "", "grok": "",
        "opencode": "", "deepseek": ""}


def test_recover_usage_limit_reconnects_codex_only(monkeypatch):
    from lib import codex_app_server
    seen = []
    monkeypatch.setattr(codex_app_server, "recover_usage_failure",
                        lambda message: seen.append(message) or True)
    for name in IDS:
        b = registry.by_id(name)
        if name == "codex":
            assert b.recover_usage_limit("limit") is True
        else:
            assert b.recover_usage_limit("limit") is False, name
    assert seen == ["limit"]
    monkeypatch.setattr(codex_app_server, "recover_usage_failure", lambda message: None)
    assert registry.by_id("codex").recover_usage_limit("limit") is False


def test_on_credential_change_recycles_codex_app_servers_only(monkeypatch):
    from lib import codex_app_server
    calls = []
    monkeypatch.setattr(codex_app_server, "recycle_clients", lambda: calls.append("recycled") or 0)
    for name in IDS:
        assert registry.by_id(name).on_credential_change() is None, name
    assert calls == ["recycled"]


def test_on_credential_change_logs_a_failed_recycle(monkeypatch):
    from lib import codex_app_server
    from lib.backend import codex as codex_module
    logged = []

    def boom():
        raise RuntimeError("no app-server")
    monkeypatch.setattr(codex_app_server, "recycle_clients", boom)
    import lib.log
    monkeypatch.setattr(lib.log, "log_exception",
                        lambda event, exc, **kw: logged.append((event, str(exc))))
    assert registry.by_id("codex").on_credential_change() is None
    assert logged == [("codexAppServerRecycleFail", "no app-server")]


def test_executable_matches_the_adapter(monkeypatch):
    from lib import clarp_runner
    monkeypatch.setattr(clarp_runner, "configured_claude_bin", lambda: "clarp")
    for a in backends.adapters():
        assert registry.by_id(a.id).executable() == a.executable() == backends.capabilities(a.id).required_binary
    assert registry.by_id("claude").executable() == "clarp"
    assert registry.by_id("deepseek").executable() == "opencode"


def test_is_valid_model_matches_the_adapter(monkeypatch):
    from lib import provider_capabilities
    monkeypatch.setattr(provider_capabilities, "is_dispatchable_agy_model",
                        lambda model: model.startswith("gemini-"))
    samples = ("gemini-3.7-flash-high", "gpt-5.4", "", None, "  ")
    for a in backends.adapters():
        b = registry.by_id(a.id)
        for model in samples:
            assert b.is_valid_model(model) is a.is_valid_model(model) is backends.is_valid_model(a.id, model), (a.id, model)
    assert registry.by_id("agy").is_valid_model("gpt-5.4") is False
    assert registry.by_id("codex").is_valid_model("gpt-5.4") is True


def test_default_model_effort_and_clean_effort_match_the_facade():
    cfg = SimpleNamespace(claude_model=" opus ", claude_effort="HIGH",
                          codex_model="gpt-5.4", codex_reasoning_effort="ultra",
                          agy_model="gemini-3.7-flash-high",
                          grok_model="", grok_effort="max",
                          opencode_model="opencode/gpt-5.4", opencode_effort="max",
                          deepseek_model="", deepseek_effort="")
    for a in backends.adapters():
        assert registry.by_id(a.id).default_model_effort(cfg) == backends.default_model_effort(a.id, cfg)
    assert registry.by_id("claude").default_model_effort(cfg) == ("opus", "high")
    assert registry.by_id("grok").default_model_effort(cfg) == ("", "")  # grok has no "max"
    assert registry.by_id("codex").clean_effort("Ultra") == backends.clean_effort("codex", "Ultra") == "ultra"


def test_compaction_matches_the_compaction_table():
    from lib import compaction
    for a in backends.adapters():
        b = registry.by_id(a.id)
        if a.id not in compaction._COMPACT:
            with pytest.raises(Unsupported):
                b.compaction("sess")
            continue
        launch, command = compaction._COMPACT[a.id]
        assert b.compaction("sess") == CompactionStrategy(
            tuple(launch), command, a.compaction_watches_transcript)
    assert registry.by_id("claude").compaction("s").watches_transcript is True
    assert registry.by_id("codex").compaction("s").watches_transcript is False


def _fake_spawn(calls, module):
    def spawn(**kw):
        calls.setdefault(module, []).append(("spawn", kw))
        return f"{module}-handle"
    return spawn


def test_runner_methods_resolve_the_runner_module_late(monkeypatch):
    # Fakes are keyed by module: DeepSeek runs through OpenCode's runner.
    calls: dict[str, list] = {}
    for a in backends.adapters():
        runner, m = backends._mod(a.runner_module), a.runner_module
        monkeypatch.setattr(runner, "spawn_turn", _fake_spawn(calls, m))
        monkeypatch.setattr(runner, "interrupt", lambda aid, _m=m: calls[_m].append(("interrupt", aid)) or 2)
        monkeypatch.setattr(runner, "active_handles", lambda aid, _m=m: calls[_m].append(("active", aid)) or [f"{_m}:{aid}"])
    spec = {"text": "hi", "cwd": "/x", "stream": None, "synthesize_audio": True,
            "hook_session": "h", "run_if_owned": "gate", "voice_preamble": True}
    for a in backends.adapters():
        b, m = registry.by_id(a.id), a.runner_module
        calls[m] = []
        assert b.spawn_turn(**spec) == backends.spawn_turn(a.id, **spec) == f"{m}-handle"
        assert calls[m][0][1] == calls[m][1][1] == a.spawn_kwargs(spec)
        assert b.interrupt("a1") == 2
        assert backends.interrupt(a.id, "a1") == 2
        assert b.active_handles("a1") == backends.active_handles(a.id, "a1") == [f"{m}:a1"]
    assert registry.by_id("deepseek").adapter.runner_module == registry.by_id("opencode").adapter.runner_module


def test_routing_methods_resolve_the_routing_module_late(monkeypatch):
    for a in backends.adapters():
        routing, m = backends._mod(a.routing_module), a.routing_module
        monkeypatch.setattr(routing, "routing_cmd",
                            lambda prompt, model="", effort="", _m=m: [_m, prompt, model, effort])
        monkeypatch.setattr(routing, "routing_text", lambda stdout, _m=m: f"{_m}:{stdout}")
    for a in backends.adapters():
        b = registry.by_id(a.id)
        assert b.routing_cmd("p", model="m", effort="e") == [a.routing_module, "p", "m", "e"]
        assert b.routing_text("out") == f"{a.routing_module}:out"


def test_transcript_access_matches_the_facade(monkeypatch):
    for a in backends.adapters():
        module, m = backends._mod(a.transcript_module), a.transcript_module
        monkeypatch.setattr(module, "find_latest_jsonl",
                            lambda sid, projects_root=None, _m=m: pathlib.Path(f"/{_m}/{sid}.jsonl"))
        monkeypatch.setattr(module, "parse_turns", lambda path, _m=m: [{"module": _m, "path": str(path)}])
    for a in backends.adapters():
        b, m = registry.by_id(a.id), a.transcript_module
        assert b.find_transcript("s1") == backends.find_session_jsonl(a.id, "s1") == pathlib.Path(f"/{m}/s1.jsonl")
        assert b.parse_transcript("/p") == backends.parse_turns(a.id, "/p") == [{"module": m, "path": "/p"}]
    # Claude threads a home into its projects root; the others own their roots.
    seen = {}
    from lib import transcript_log
    monkeypatch.setattr(transcript_log, "find_latest_jsonl",
                        lambda sid, projects_root=None: seen.setdefault("root", projects_root))
    registry.by_id("claude").find_transcript("s1", home=pathlib.Path("/h"))
    assert seen["root"] == pathlib.Path("/h/.claude/projects")


def test_list_sessions_matches_the_facade(monkeypatch):
    from lib import session_catalog
    monkeypatch.setattr(session_catalog, "list_claude_sessions",
                        lambda cwd, *, all_projects, limit: [{"id": f"claude:{cwd}:{limit}:{all_projects}"}])
    for a in backends.adapters():
        if a.id == "claude":
            continue
        module = backends._mod(a.transcript_module)
        monkeypatch.setattr(module, "list_sessions",
                            lambda cwd, *, limit, all_projects=False, _id=a.id: [{"id": f"{_id}:{cwd}:{limit}:{all_projects}"}])
    for a in backends.adapters():
        b = registry.by_id(a.id)
        for all_projects in (False, True):
            assert (b.list_sessions("/w", limit=5, all_projects=all_projects)
                    == backends.list_sessions(a.id, "/w", limit=5, all_projects=all_projects))


def test_resume_target_is_the_transcript_for_claude_and_the_id_for_the_rest(monkeypatch):
    from lib import resume
    monkeypatch.setattr(resume, "find_session_jsonl",
                        lambda sid, cwd, root: pathlib.Path(f"{root}/{sid}.jsonl") if sid == "real" else None)
    claude = registry.by_id("claude")
    assert claude.resume_target("real", "/w", home=pathlib.Path("/h")) == pathlib.Path("/h/.claude/projects/real.jsonl")
    assert claude.resume_target("ghost", "/w", home=pathlib.Path("/h")) is None
    assert claude.resume_target("", "/w") is None
    assert claude.resume_target("real", "/w", home=pathlib.Path("/h")) == backends.find_resume_transcript(
        "claude", "real", cwd="/w", projects_root=pathlib.Path("/h/.claude/projects"))
    for name in IDS[1:]:
        b = registry.by_id(name)
        assert b.resume_target("sid-1", "/w") == "sid-1", name
        assert b.resume_target("", "/w") is None, name


def test_bind_new_session_pre_mints_for_claude_only(monkeypatch):
    from lib import agents as agents_db
    bound = []
    monkeypatch.setattr(agents_db, "bind_backend_session", lambda aid, bsid: bound.append((aid, bsid)))
    minted = iter(["u-1", "u-2"])
    assert registry.by_id("claude").bind_new_session("a1", "sess", uuid_factory=lambda: next(minted)) == "u-1"
    assert bound == [("a1", "u-1")]
    for name in IDS[1:]:
        assert registry.by_id(name).bind_new_session("a1", "sess") == "", name
    assert bound == [("a1", "u-1")]


def test_bind_new_session_retries_one_collision(monkeypatch):
    from lib import agents as agents_db
    bound = []

    def bind(aid, bsid):
        if bsid == "dup":
            raise agents_db.SessionAlreadyBound(aid, "dup", "other-agent")
        bound.append(bsid)
    monkeypatch.setattr(agents_db, "bind_backend_session", bind)
    minted = iter(["dup", "fresh"])
    assert registry.by_id("claude").bind_new_session("a1", "s", uuid_factory=lambda: next(minted)) == "fresh"
    assert bound == ["fresh"]


def test_wrap_turn_callback_serialises_claude_under_the_dispatch_lock():
    from lib import turn_dispatch

    def probe(*args):
        return (args, turn_dispatch._TURN_LOCK._is_owned())
    wrapped = registry.by_id("claude").wrap_turn_callback(probe)
    assert wrapped is not probe
    assert wrapped(1, 2) == ((1, 2), True)
    assert turn_dispatch._TURN_LOCK._is_owned() is False
    for name in IDS[1:]:
        assert registry.by_id(name).wrap_turn_callback(probe) is probe, name


def test_arm_source_marker_writes_the_hook_marker_for_claude_only(tmp_path, monkeypatch):
    monkeypatch.delenv("CLARP_CACHE_DIR", raising=False)
    from lib import send_service
    home = tmp_path / "home"
    for name in IDS[1:]:
        assert registry.by_id(name).arm_source_marker("sess", "t-1", True, home=home) is None
    assert not home.exists()
    registry.by_id("claude").arm_source_marker("sess", "t-1", False, home=home, now=lambda: 12.5)
    path = send_service.source_marker_path(home, "sess")
    # Byte-identical to what turn_dispatch wrote before: the hook plugin parses it.
    assert path == home / ".cache" / "clarp" / "source-markers" / "sess"
    assert path.read_text() == "pwa-voice sess 12.500 t-1 0\n"
    assert path.read_text() == send_service.source_marker_text(
        session="sess", trace_id="t-1", now=12.5, synthesize_audio=False)
    registry.by_id("claude").arm_source_marker("sess", "t-2", True, home=home, now=lambda: 13.0)
    assert path.read_text() == "pwa-voice sess 13.000 t-2 1\n"


def test_quota_identity_matches_the_janitor_window_key():
    from lib import janitor_autonomy
    windows = [
        {"window_id": "w-5h", "kind": "five_hour", "resets_at": "2026-09-25T10:00:29.700Z"},
        {"window_id": "w-5h", "kind": "five_hour", "resets_at": "2026-09-25T09:59:31.100Z"},
        {"window_id": "w-plain"},
        {"window_id": "w-bad", "kind": "weekly", "resets_at": "not-a-date"},
    ]
    for a in backends.adapters():
        b = registry.by_id(a.id)
        for window in windows:
            expected = janitor_autonomy.quota_window_key(a.id, "acct", window)
            got = "quota-keeper.window." + janitor_autonomy.digest([a.id, "acct", b.quota_identity(window)])
            assert got == expected, (a.id, window)
    claude = registry.by_id("claude")
    # Both jittered resets round to the same minute, so they share an identity.
    assert claude.quota_identity(windows[0]) == claude.quota_identity(windows[1])
    assert claude.quota_identity(windows[2]) == "w-plain"
    assert registry.by_id("codex").quota_identity(windows[0]) == "w-5h"


def test_classify_usage_limit_records_a_provider_event_for_codex_only(monkeypatch):
    from lib import backend_usage
    recorded = []
    monkeypatch.setattr(backend_usage, "record_classified_usage_limit",
                        lambda provider: recorded.append(provider) or {"provider": provider})
    for a in backends.adapters():
        b = registry.by_id(a.id)
        if a.records_classified_usage_limit:
            assert b.classify_usage_limit("limit") == {"provider": a.id}
            assert b.classify_usage_limit("limit", quota_confirmed=True) == {"provider": a.id}
            assert b.classify_usage_limit("limit", quota_confirmed=False) is None
        else:
            assert b.classify_usage_limit("limit", quota_confirmed=True) is None, a.id
    assert recorded == ["codex", "codex"]


def test_recorded_model_delegates_to_session_models(monkeypatch):
    from lib import session_models
    monkeypatch.setattr(session_models, "recorded_model", lambda backend, session: f"{backend}/{session}")
    for name in IDS:
        assert registry.by_id(name).recorded_model("s9") == f"{name}/s9"


def test_stream_json_shared_surface_carries_the_runner_prefix(monkeypatch):
    """Slice 3: the runner_common bodies live on StreamJsonBackend and key
    their log events by the runner name, so DeepSeek logs as OpenCode."""
    from lib import agents as agents_db
    from lib.backend import stream_json
    logged = []
    monkeypatch.setattr(stream_json, "log_exception",
                        lambda name, error, detail=None: logged.append((name, detail)))
    monkeypatch.setattr(stream_json, "log", lambda name, msg: logged.append((name, msg)))

    def boom(*args, **kwargs):
        raise RuntimeError("down")

    class Broken:
        def broadcast(self, event):
            raise RuntimeError("boom")

    monkeypatch.setattr(agents_db, "record_state", boom)
    monkeypatch.setattr(agents_db, "latest_turn_synthesize_audio", lambda agent_id: False)
    for name, prefix in (("grok", "grok"), ("opencode", "opencode"), ("deepseek", "opencode")):
        b = registry.by_id(name)
        logged.clear()
        b.record_state("a1", "thinking", None)
        b.record_state("a1", "thinking", None, event="custom")
        b.broadcast_transcript(Broken(), "a1", "sess")
        st = SimpleNamespace(session_id="", saw_session=False, failed_error="")
        b.bind_session(st, "sid", on_session_init=None, on_error=None, trace_id="t")
        assert b.speak("<speak>hi</speak>", st, agent_id="a1", session="s", trace_id="t", enqueue=None) == 0
        assert logged == [(f"{prefix}RecordStateFail", "a1:thinking"),
                          ("custom", "a1:thinking"),
                          (f"{prefix}BroadcastFail", "a1"),
                          (f"{prefix}SessionInit", "sid=sid trace=t")], name
        assert (st.session_id, st.saw_session) == ("sid", True)
    # Isolated runs (no agent) record and broadcast nothing.
    logged.clear()
    registry.by_id("grok").record_state("", "thinking", None)
    registry.by_id("grok").broadcast_transcript(Broken(), "", "sess")
    assert logged == []


def test_runner_registries_are_shared_per_runner():
    """DeepSeek turns live in OpenCode's registry, as they did when the
    registry was ``opencode_runner._REGISTRY``; every other CLI has its own."""
    from lib import clarp_runner, codex_runner, grok_runner, opencode_runner, agy_runner
    assert registry.by_id("deepseek")._registry is registry.by_id("opencode")._registry
    assert opencode_runner._REGISTRY is registry.by_id("opencode")._registry
    assert grok_runner._REGISTRY is registry.by_id("grok")._registry
    assert agy_runner._REGISTRY is registry.by_id("agy")._registry
    assert clarp_runner._REGISTRY is registry.by_id("claude")._registry
    assert codex_runner._REGISTRY is registry.by_id("codex")._registry
    distinct = {id(registry.by_id(n)._registry) for n in ("claude", "codex", "agy", "grok", "opencode")}
    assert len(distinct) == 5


def test_hook_prefers_a_test_double_over_the_modules_own_delegator(monkeypatch):
    """The slice-3 seam: a monkeypatched runner-module global intercepts
    the class body; the module's own delegator does not recurse into it."""
    from lib import grok_runner
    grok = registry.by_id("grok")
    assert grok._hook("build_cmd") is None
    assert grok._hook("build_cmd", "own") == "own"
    assert grok._hook("GROK_BIN", "x") == "grok"
    monkeypatch.setattr(grok_runner, "GROK_BIN", "fake-grok")
    assert grok.build_cmd("")[0] == "fake-grok"
    monkeypatch.setattr(grok_runner, "build_cmd", lambda *a, **kw: ["patched"])
    assert grok.routing_cmd("p") == ["patched", "-p", "p"]
    monkeypatch.setattr(grok_runner, "routing_text", lambda stdout: f"double:{stdout}")
    assert grok.routing_text("o") == "double:o"
    assert grok_runner.routing_text("o") == "double:o"


def test_base_defaults_are_documented_no_ops_or_not_implemented():
    class Bare(Backend):
        pass
    bare = Bare(backends.get("grok"))
    assert bare.bind_new_session("a", "s") == ""
    assert bare.on_credential_change() is None
    assert bare.recover_usage_limit("m") is False
    assert bare.account_pool() == ""
    assert bare.classify_usage_limit("m") is None
    assert bare.quota_identity({"window_id": "w"}) == "w"
    fn = object()
    assert bare.wrap_turn_callback(fn) is fn
    assert bare.arm_source_marker("s", "t", True) is None
    with pytest.raises(Unsupported):
        bare.terminal_argv("s")
    # interrupt/active_handles are implemented on the base over the runner's
    # process registry (slice 3), so a bare subclass gets them for free.
    assert bare.interrupt("nobody") == 0 and bare.active_handles("nobody") == []
    for method, args in (("spawn_turn", ()),
                         ("resume_target", ("s", "/w")), ("find_transcript", ("s",)),
                         ("parse_transcript", ("/p",)), ("list_sessions", ("/w",)),
                         ("routing_cmd", ("p",)), ("routing_text", ("o",))):
        with pytest.raises(NotImplementedError):
            getattr(bare, method)(*args)
