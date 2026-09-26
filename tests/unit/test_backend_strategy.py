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
    # get(), adapter_for() and the facade table all hand out the backend
    # object itself; there is no separate catalogue row any more.
    assert backends.get(backend) is b
    assert backends.adapter_for(backend) is b
    a = backends._BY_ID[b.id]
    assert a is b
    for name in ("id", "label", "required_binary", "aliases", "efforts",
                 "context_window", "model_family", "janitor_default_model",
                 "api_providers", "login_kind", "effort_ui", "effort_scope",
                 "fallback_models", "runner", "badge", "detail", "symbol",
                 "brand", "hidden", "effort_help", "resumable", "supports_fork",
                 "supports_steer", "supports_transcript_streaming",
                 "supports_mcp", "supports_usage", "native_tool_explainer",
                 "routing_module", "config_model_field", "config_effort_field",
                 "supports_routing", "supports_auth",
                 "effort_compatibility_unknown", "model_carries_effort"):
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
        assert registry.by_id(a.id).executable() == backends.capabilities(a.id).required_binary
        if a.id != "claude":
            assert registry.by_id(a.id).executable() == a.required_binary
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
            assert b.is_valid_model(model) is backends.is_valid_model(a.id, model), (a.id, model)
            if a.id != "agy":
                assert b.is_valid_model(model) is True, (a.id, model)
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


COMPACTION = {
    "claude": CompactionStrategy(("claude", "--dangerously-skip-permissions", "--resume"), "/compact", True),
    "codex": CompactionStrategy(("codex", "resume"), "/compact", False),
    "agy": CompactionStrategy(("agy", "--dangerously-skip-permissions", "--conversation"), "/compress", False),
    "grok": CompactionStrategy(("grok", "--resume"), "/compact", False),
}


def test_compaction_strategy_per_backend_or_unsupported():
    """The table compaction.py used to carry; OpenCode (and DeepSeek on it)
    has no compaction machinery, so the catalogue flag says so too."""
    for name in IDS:
        b = registry.by_id(name)
        if name not in COMPACTION:
            with pytest.raises(Unsupported, match=f"compaction unsupported for {name}"):
                b.compaction("sess")
            assert backends.supports_compact(name) is False, name
            assert backends.catalogue_fields(name)["supports_compact"] is False, name
            continue
        assert b.compaction("sess") == COMPACTION[name]
        assert backends.supports_compact(name) is True, name
        assert backends.catalogue_fields(name)["supports_compact"] is True, name


def _fake_spawn(calls, module):
    def spawn(**kw):
        calls.setdefault(module, []).append(("spawn", kw))
        return f"{module}-handle"
    return spawn


def test_runner_modules_delegate_to_the_classes(monkeypatch):
    """The ``<runner>_runner`` modules are thin delegators: patching the
    class instance is what a test needs, and the module and the facade both
    land on it. DeepSeek shares OpenCode's runner (and process registry)."""
    spec = {"text": "hi", "cwd": "/x", "stream": None}
    for a in backends.adapters():
        b = registry.by_id(a.id)
        calls: list = []
        monkeypatch.setattr(b, "spawn_turn", lambda **kw: calls.append(("spawn", kw)) or f"{b.id}-handle")
        # The module delegators call the full-signature ``start_turn``; the
        # class's ``spawn_turn`` filters the host's kwargs down to it.
        monkeypatch.setattr(b, "start_turn", lambda **kw: calls.append(("spawn", kw)) or f"{b.id}-handle")
        monkeypatch.setattr(b, "interrupt", lambda aid: calls.append(("interrupt", aid)) or 2)
        monkeypatch.setattr(b, "active_handles", lambda aid: calls.append(("active", aid)) or [f"{b.id}:{aid}"])
        # Codex's module keeps its historical narrower surface: it interrupts
        # and lists only the exec processes, not the app-server threads the
        # class's full interrupt also covers.
        if hasattr(b, "interrupt_exec"):
            monkeypatch.setattr(b, "interrupt_exec", lambda aid: calls.append(("interrupt", aid)) or 2)
            monkeypatch.setattr(b, "active_exec_handles", lambda aid: calls.append(("active", aid)) or [f"{b.id}:{aid}"])
        assert backends.spawn_turn(a.id, **spec) == f"{b.id}-handle"
        assert backends.interrupt(a.id, "a1") == 2
        assert backends.active_handles(a.id, "a1") == [f"{b.id}:a1"]
        if b.id != "deepseek":
            module = backends._mod(b.runner_module)
            assert module.spawn_turn(**spec) == f"{b.id}-handle"
            assert module.interrupt("a1") == 2
            assert module.active_handles("a1") == [f"{b.id}:a1"]
        assert [c[0] for c in calls][:3] == ["spawn", "interrupt", "active"]
    assert registry.by_id("deepseek").runner == registry.by_id("opencode").runner
    assert isinstance(registry.by_id("deepseek"), type(registry.by_id("opencode")))


def test_routing_modules_delegate_to_the_classes(monkeypatch):
    for a in backends.adapters():
        if a.id == "deepseek":
            continue
        b = registry.by_id(a.id)
        monkeypatch.setattr(b, "routing_cmd", lambda prompt, model="", effort="": [b.id, prompt, model, effort])
        monkeypatch.setattr(b, "routing_text", lambda stdout: f"{b.id}:{stdout}")
        module = backends._mod(b.runner_module)
        assert module.routing_cmd("p", model="m", effort="e") == [b.id, "p", "m", "e"]
        assert module.routing_text("out") == f"{b.id}:out"


TRANSCRIPT_MODULES = {
    # The parser module each backend reads its sessions through.
    "claude": "transcript_log", "codex": "codex_transcript", "agy": "agy_transcript",
    "grok": "grok_transcript", "opencode": "opencode_transcript",
    "deepseek": "opencode_transcript",
}


def test_transcript_access_matches_the_facade(monkeypatch):
    for m in set(TRANSCRIPT_MODULES.values()):
        module = backends._mod(m)
        monkeypatch.setattr(module, "find_latest_jsonl",
                            lambda sid, projects_root=None, _m=m: pathlib.Path(f"/{_m}/{sid}.jsonl"))
        monkeypatch.setattr(module, "parse_turns", lambda path, _m=m: [{"module": _m, "path": str(path)}])
    for a in backends.adapters():
        b, m = registry.by_id(a.id), TRANSCRIPT_MODULES[a.id]
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
    for m in set(TRANSCRIPT_MODULES.values()) - {"transcript_log"}:
        module = backends._mod(m)
        if m == "agy_transcript":
            # agy's catalogue takes no scope flag; "all" is an empty cwd.
            monkeypatch.setattr(module, "list_sessions",
                                lambda cwd, limit=20, _m=m: [{"id": f"{_m}:{cwd}:{limit}"}])
        else:
            monkeypatch.setattr(module, "list_sessions",
                                lambda cwd, *, limit, all_projects=False, _m=m: [{"id": f"{_m}:{cwd}:{limit}:{all_projects}"}])
    for a in backends.adapters():
        b, m = registry.by_id(a.id), TRANSCRIPT_MODULES[a.id]
        for all_projects in (False, True):
            got = b.list_sessions("/w", limit=5, all_projects=all_projects)
            assert got == backends.list_sessions(a.id, "/w", limit=5, all_projects=all_projects)
            cwd = "" if all_projects else "/w"
            if a.id == "claude":
                assert got == [{"id": f"claude:/w:5:{all_projects}"}]
            elif a.id == "agy":
                assert got == [{"id": f"agy_transcript:{cwd}:5"}]
            else:
                assert got == [{"id": f"{m}:{cwd}:5:{all_projects}"}]


def test_resume_target_is_the_transcript_for_claude_and_the_id_for_the_rest(monkeypatch):
    from lib import resume
    monkeypatch.setattr(resume, "find_session_jsonl",
                        lambda sid, cwd, root: pathlib.Path(f"{root}/{sid}.jsonl") if sid == "real" else None)
    claude = registry.by_id("claude")
    assert claude.resume_target("real", "/w", home=pathlib.Path("/h")) == pathlib.Path("/h/.claude/projects/real.jsonl")
    assert claude.resume_target("ghost", "/w", home=pathlib.Path("/h")) is None
    assert claude.resume_target("", "/w") is None
    # The same lookup, cwd hint and all, is find_transcript's when asked with a cwd.
    assert claude.find_transcript("real", pathlib.Path("/h"), cwd="/w") == pathlib.Path("/h/.claude/projects/real.jsonl")
    for name in IDS[1:]:
        b = registry.by_id(name)
        assert b.resume_target("sid-1", "/w") == "sid-1", name
        assert b.resume_target("", "/w") is None, name


def test_find_transcript_with_a_cwd_hint_prefers_the_encoded_project_dir(tmp_path):
    """Boot-time resume (lib.resume) asks with the saved cwd: Claude prefers
    the project dir that encodes it, falls back to a scan of the root, and
    the other CLIs ignore the hint because their layouts carry no cwd."""
    home = tmp_path
    projects = home / ".claude" / "projects"
    for encoded in ("-home-x", "-home-x-GIT-app"):
        (projects / encoded).mkdir(parents=True)
        (projects / encoded / "abc.jsonl").write_text("")
    (projects / "-home-x-GIT-app" / "xyz.jsonl").write_text("")
    claude = registry.by_id("claude")
    assert claude.find_transcript("abc", home, cwd="/home/x") == projects / "-home-x" / "abc.jsonl"
    assert claude.find_transcript("xyz", home, cwd="/home/x") == projects / "-home-x-GIT-app" / "xyz.jsonl"
    assert claude.find_transcript("nope", home, cwd="/home/x") is None
    assert claude.find_transcript("", home, cwd="/home/x") is None


def test_transcript_cwd_is_decoded_from_claude_project_dirs_only():
    claude = registry.by_id("claude")
    assert claude.transcript_cwd(pathlib.Path("/h/.claude/projects/-home-user-GIT-sqlit/s.jsonl")) == "/home/user/GIT/sqlit"
    assert claude.transcript_cwd(pathlib.Path("/h/.claude/projects/no-prefix/s.jsonl")) == ""
    for name in IDS[1:]:
        b = registry.by_id(name)
        assert b.transcript_cwd(pathlib.Path("/x/-home-user/s.jsonl")) == "", name
        assert b.transcript_cwd("s-1") == "", name


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
    wrapped = registry.by_id("claude").wrap_turn_callback(probe, turn_dispatch._TURN_LOCK)
    assert wrapped is not probe
    assert wrapped(1, 2) == ((1, 2), True)
    assert turn_dispatch._TURN_LOCK._is_owned() is False
    for name in IDS[1:]:
        assert registry.by_id(name).wrap_turn_callback(probe, turn_dispatch._TURN_LOCK) is probe, name


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
    # An API provider is not a backend: its window id is the identity as is.
    assert (janitor_autonomy.quota_window_key("openai", "acct", windows[0])
            == "quota-keeper.window." + janitor_autonomy.digest(["openai", "acct", "w-5h"]))


def test_classify_usage_limit_records_a_provider_event_for_codex_only(monkeypatch):
    from lib import backend_usage
    recorded = []
    monkeypatch.setattr(backend_usage, "record_classified_usage_limit",
                        lambda provider: recorded.append(provider) or {"provider": provider})
    for name in IDS:
        b = registry.by_id(name)
        if name == "codex":
            assert b.classify_usage_limit("limit") == {"provider": "codex"}
            assert b.classify_usage_limit("limit", quota_confirmed=True) == {"provider": "codex"}
            assert b.classify_usage_limit("limit", quota_confirmed=False) is None
        else:
            assert b.classify_usage_limit("limit", quota_confirmed=True) is None, name
    assert recorded == ["codex", "codex"]


def test_recorded_model_reads_claude_transcripts_and_the_codex_index(monkeypatch, tmp_path):
    """session_models' per-backend readers live on the classes: Claude reads
    the transcript's last turn, Codex prefers its thread index and falls back
    to the rollout, and the rest record nothing the Host can read."""
    from lib import session_models
    monkeypatch.setattr(session_models, "transcript_model",
                        lambda backend, session: f"{backend}-transcript/{session}")
    indexed = {}
    monkeypatch.setattr(session_models, "indexed_model",
                        lambda home, session: indexed.get(session, ""))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    assert registry.by_id("claude").recorded_model("s9") == "claude-transcript/s9"
    assert registry.by_id("codex").recorded_model("s9") == "codex-transcript/s9"
    indexed["s9"] = "gpt-indexed"
    assert registry.by_id("codex").recorded_model("s9") == "gpt-indexed"
    for name in IDS:
        assert registry.by_id(name).recorded_model("") == "", name
        if name not in ("claude", "codex"):
            assert registry.by_id(name).recorded_model("s9") == "", name
    # The facade entry point asks the backend.
    assert session_models.recorded_model("claude", "s9") == "claude-transcript/s9"
    assert session_models.recorded_model("grok", "s9") == ""


def test_codex_model_transcript_prefers_the_thread_index(monkeypatch, tmp_path):
    import sqlite3
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text("")
    with sqlite3.connect(tmp_path / "state_5.sqlite") as db:
        db.execute("CREATE TABLE threads (id TEXT, rollout_path TEXT)")
        db.execute("INSERT INTO threads VALUES (?, ?)", ("indexed", str(rollout)))
        db.execute("INSERT INTO threads VALUES (?, ?)", ("stale", str(tmp_path / "gone.jsonl")))
    from lib import codex_transcript
    monkeypatch.setattr(codex_transcript, "find_latest_jsonl",
                        lambda sid: pathlib.Path(f"/scan/{sid}.jsonl"))
    codex = registry.by_id("codex")
    assert codex.model_transcript("indexed") == rollout
    assert codex.model_transcript("stale") == pathlib.Path("/scan/stale.jsonl")
    assert codex.model_transcript("unknown") == pathlib.Path("/scan/unknown.jsonl")
    # Every other backend's model transcript is its session transcript.
    from lib import transcript_log
    monkeypatch.setattr(transcript_log, "find_latest_jsonl",
                        lambda sid, projects_root=None: pathlib.Path(f"/claude/{sid}.jsonl"))
    assert registry.by_id("claude").model_transcript("s1") == pathlib.Path("/claude/s1.jsonl")


def test_cli_default_model_reads_each_clis_own_config(monkeypatch, tmp_path):
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    claude_home = tmp_path / "claude"
    claude_home.mkdir()
    (claude_home / "settings.json").write_text('{"model": "opus"}')
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_home))
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text('model = "gpt-5.4"\nprofile = "fast"\n[profiles.fast]\nmodel = "gpt-5.4-mini"\n')
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    assert registry.by_id("claude").cli_default_model() == "opus"
    monkeypatch.setenv("ANTHROPIC_MODEL", "sonnet")
    assert registry.by_id("claude").cli_default_model() == "sonnet"
    assert registry.by_id("codex").cli_default_model() == "gpt-5.4-mini"
    for name in IDS[2:]:
        assert registry.by_id(name).cli_default_model() == "", name
    # launch_default: the Host pin wins, else the CLI's own default, else "".
    from lib import session_models
    cfg = SimpleNamespace(claude_model="", codex_model="host-pin", grok_model="")
    assert session_models.launch_default("claude", cfg) == "sonnet"
    assert session_models.launch_default("codex", cfg) == "host-pin"
    assert session_models.launch_default("grok", cfg) == ""


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
        id = "bare"
        label = "Bare"
        required_binary = "grok"
    bare = Bare()
    assert bare.bind_new_session("a", "s") == ""
    assert bare.on_credential_change() is None
    assert bare.recover_usage_limit("m") is False
    assert bare.account_pool() == ""
    assert bare.classify_usage_limit("m") is None
    assert bare.quota_identity({"window_id": "w"}) == "w"
    fn = object()
    assert bare.wrap_turn_callback(fn, object()) is fn
    assert bare.arm_source_marker("s", "t", True) is None
    assert bare.recorded_model("s") == "" and bare.cli_default_model() == ""
    with pytest.raises(Unsupported):
        bare.terminal_argv("s")
    with pytest.raises(Unsupported):
        bare.goal("a", "start")
    with pytest.raises(Unsupported):
        bare.steer("a", "hi")
    with pytest.raises(Unsupported):
        bare.compaction("s")
    assert bare.is_valid_model("anything") is True and bare.executable() == "grok"
    assert bare.recorded_model("s") == "" and bare.cli_default_model() == ""
    assert bare.transcript_cwd("/x/-a-b/s.jsonl") == ""
    with pytest.raises(Unsupported):
        bare.compaction("s")
    # interrupt/active_handles are implemented on the base over the runner's
    # process registry (slice 3), so a bare subclass gets them for free.
    assert bare.interrupt("nobody") == 0 and bare.active_handles("nobody") == []
    for method, args in (("spawn_turn", ()),
                         ("resume_target", ("s", "/w")), ("find_transcript", ("s",)),
                         ("parse_transcript", ("/p",)), ("list_sessions", ("/w",)),
                         ("routing_cmd", ("p",)), ("routing_text", ("o",))):
        with pytest.raises(NotImplementedError):
            getattr(bare, method)(*args)
