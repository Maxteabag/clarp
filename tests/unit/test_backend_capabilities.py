"""Backends are polymorphic: behaviour a CLI differs on is declared on its
``BackendAdapter`` and looked up, never branched on by identity.

Two halves: a source guard that fails when a new ``backend == X`` branch
appears outside the registry and the per-CLI runner modules, and unit tests
that every adapter declares every capability plus a few of the decisions the
adapters replaced (terminal argv, resume-target checks, spawn kwargs).
"""
from __future__ import annotations

import pathlib
import re
import sys
from types import SimpleNamespace

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))
from lib import backends  # noqa: E402

IDS = ("claude", "codex", "agy", "grok", "opencode", "deepseek")

# --- (4) source guard --------------------------------------------------------

_IDS = r"(?:CLAUDE|CODEX|AGY|GROK|OPENCODE|DEEPSEEK)"
_LIT = r"""['"](?:claude|codex|agy|antigravity|grok|opencode|deepseek)['"]"""
_REF = rf"(?:self\.)?(?:backends|AgentBackend)\.{_IDS}\b"
BRANCH = re.compile(
    rf"(?:==|!=)\s*{_REF}"          # x == backends.CODEX
    rf"|{_REF}\s*(?:==|!=)"          # backends.CODEX == x
    rf"|\bin\s*[\{{\(\[]\s*{_REF}"   # x in {backends.CLAUDE, ...}
    rf"|(?:==|!=)\s*{_LIT}"          # x == "codex"
    rf"|{_LIT}\s*(?:==|!=)"          # "codex" == x
)
# The registry itself, the model catalogue and the per-CLI runner/transcript
# modules are allowed to know which CLI they are. transcript_streamer.py and
# reconcile.py are being refactored separately.
EXCLUDED = {"backends.py", "provider_capabilities.py",
            "transcript_streamer.py", "reconcile.py"}
EXCLUDED_SUFFIXES = ("_runner.py", "_transcript.py")

# Branches that remain, with why. ``cfg.oracle_router_backend`` chooses the
# oracle's router transport ("api" = OpenAI HTTP, "codex" = the Codex CLI as
# a router); it is not an agent backend and no adapter runs it.
ALLOWLIST: dict[tuple[str, str], str] = {
    ("oracle_router.py", 'if backend == "codex":'):
        "router transport switch (api|codex), not an agent backend",
    ("oracle_router.py", '"billing": "chatgpt_subscription" if backend == "codex" else "openai_api",'):
        "billing label for the router transport, not an agent backend",
    ("oracle_live.py", 'router_backend == "codex" and router_reuse'):
        "reuses a Codex router session only for the codex router transport",
    ("oracle_realtime.py", '"oracle_router_backend", "api") == "codex"'):
        "live availability depends on the router transport, not an agent backend",
}


def _guarded_files() -> list[pathlib.Path]:
    lib = ROOT / "server" / "lib"
    files = [p for p in lib.glob("*.py")
             if p.name not in EXCLUDED and not p.name.endswith(EXCLUDED_SUFFIXES)]
    return sorted(files) + [ROOT / "server" / "server.py"]


def test_no_backend_identity_branches_outside_the_registry():
    offenders = []
    seen_allowed: set[tuple[str, str]] = set()
    for path in _guarded_files():
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if not BRANCH.search(line):
                continue
            key = next(((name, frag) for (name, frag) in ALLOWLIST
                        if name == path.name and frag in line), None)
            if key is None:
                offenders.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()}")
            else:
                seen_allowed.add(key)
    assert not offenders, (
        "Backend identity branch found; declare the capability on "
        "BackendAdapter in server/lib/backends.py and look it up instead:\n"
        + "\n".join(offenders))
    stale = set(ALLOWLIST) - seen_allowed
    assert not stale, f"allowlist entries no longer match any line: {sorted(stale)}"


# --- (5) every adapter declares every capability -----------------------------

REQUIRED_STR = ("resume_target", "account_pool", "goal_module", "api_providers",
                "janitor_default_model", "model_family")
REQUIRED_BOOL = ("preassigns_session_id", "transcript_dir_encodes_cwd",
                 "transcript_reader_injected", "hook_source_marker",
                 "locks_turn_callbacks", "records_classified_usage_limit",
                 "restarts_runner_on_credential_change",
                 "compaction_watches_transcript", "quota_reset_jittered",
                 "terminal_loads_plugin", "native_tool_explainer")
REQUIRED_CALLABLE = ("spawn_kwargs", "executable_resolver",
                     "resume_transcript_finder", "session_catalog_reader")
OPTIONAL = ("model_validator", "usage_limit_recovery", "context_window",
            "terminal_resume_argv", "terminal_fresh_argv")


@pytest.mark.parametrize("backend", IDS)
def test_every_adapter_declares_every_capability(backend):
    adapter = backends.get(backend)
    assert adapter is not None
    for name in REQUIRED_STR:
        assert isinstance(getattr(adapter, name), (str, tuple)), name
    for name in REQUIRED_BOOL:
        assert isinstance(getattr(adapter, name), bool), name
    for name in REQUIRED_CALLABLE:
        assert callable(getattr(adapter, name)), name
    assert isinstance(adapter.recorded_model_source, backends.RecordedModelSource)
    assert adapter.resume_target in backends.RESUME_TARGETS
    for name in OPTIONAL:
        assert hasattr(adapter, name), name


def test_declared_values_match_the_behaviour_they_replaced():
    by = {a.id: a for a in backends.adapters()}
    assert {a.id for a in backends.adapters()} == set(IDS)

    # Claude is the transcript-file CLI: it pre-binds --session-id, its
    # transcript dir encodes the cwd, the Host injects its reader and it is the
    # only CLI with a context gauge and a hook source marker.
    claude = by["claude"]
    assert claude.resumes_by_transcript_file and claude.preassigns_session_id
    assert claude.transcript_dir_encodes_cwd and claude.transcript_reader_injected
    assert claude.hook_source_marker and claude.locks_turn_callbacks
    assert claude.context_window == 1_000_000 and claude.compaction_watches_transcript
    assert claude.quota_reset_jittered and claude.terminal_loads_plugin
    assert claude.spawn_kwargs is backends._claude_kwargs
    assert claude.executable_resolver is backends._configured_claude_binary
    for other in IDS[1:]:
        a = by[other]
        assert not a.resumes_by_transcript_file and not a.preassigns_session_id, other
        assert not a.transcript_dir_encodes_cwd and not a.transcript_reader_injected, other
        assert not a.hook_source_marker and not a.locks_turn_callbacks, other
        assert a.context_window is None and not a.compaction_watches_transcript, other
        assert not a.quota_reset_jittered and not a.terminal_loads_plugin, other
        assert a.executable_resolver is backends._declared_binary, other

    # Codex owns the goal protocol, in-process usage-limit recovery, the
    # classified-limit event, app-server recycling after login, the OpenAI
    # routing provider and the native tool explainer.
    codex = by["codex"]
    assert codex.supports_goal and codex.goal_module == "codex_app_server"
    assert codex.usage_limit_recovery is not None
    assert codex.records_classified_usage_limit
    assert codex.restarts_runner_on_credential_change
    assert codex.api_providers == ("openai",) and codex.native_tool_explainer
    assert codex.janitor_default_model == "gpt-5.3-codex-spark"
    for other in ("claude", "agy", "grok", "opencode", "deepseek"):
        a = by[other]
        assert not a.supports_goal and a.usage_limit_recovery is None, other
        assert not a.records_classified_usage_limit, other
        assert not a.restarts_runner_on_credential_change, other
        assert a.api_providers == () and not a.native_tool_explainer, other
        assert a.janitor_default_model == "", other

    # Account failover pools: Claude and Codex only.
    assert {a.id: a.account_pool for a in backends.adapters()} == {
        "claude": "claude", "codex": "codex", "agy": "", "grok": "",
        "opencode": "", "deepseek": ""}
    assert [a.id for a in backends.adapters() if a.supports_account_failover] == ["claude", "codex"]

    # AGY folds effort into the model id and validates model ids.
    assert by["agy"].effort_compatibility_unknown and by["agy"].model_carries_effort
    assert by["agy"].model_validator is not None
    assert by["agy"].spawn_kwargs is backends._owner_gated_stream_kwargs
    for other in ("claude", "codex", "grok", "opencode", "deepseek"):
        assert not by[other].effort_compatibility_unknown, other
        assert by[other].model_validator is None, other

    # Model families for avatars: Claude and OpenCode front several families.
    assert {a.id: a.model_family for a in backends.adapters()} == {
        "claude": "", "codex": "codex", "agy": "gemini", "grok": "grok",
        "opencode": "", "deepseek": "deepseek"}

    # Session-model sources: only Claude and Codex record a model anywhere.
    for name in ("agy", "grok", "opencode", "deepseek"):
        assert by[name].recorded_model_source is backends.NO_RECORDED_MODEL, name
    assert by["codex"].recorded_model_source.indexed_model is not None
    assert by["claude"].recorded_model_source.indexed_model is None
    assert by["claude"].recorded_model_source.cli_default_model is not None


def test_adapter_rejects_unknown_resume_target():
    with pytest.raises(ValueError):
        backends.BackendAdapter(id="x", label="X", required_binary="x",
                                resume_target="magic")


# --- (5) replaced decisions -------------------------------------------------

def test_interactive_terminal_argv_per_backend():
    """The table terminal_ws.py used to carry, now declared per adapter."""
    expected = {
        "claude": (("claude", "--dangerously-skip-permissions", "--resume"),
                   ("claude", "--dangerously-skip-permissions")),
        "codex": (("codex", "resume"), ("codex",)),
        "agy": (("agy", "--dangerously-skip-permissions", "--conversation"),
                ("agy", "--dangerously-skip-permissions")),
        # No interactive terminal launch is defined for these yet; today the
        # /terminal route fails for them, and still does.
        "grok": (None, None), "opencode": (None, None), "deepseek": (None, None),
    }
    assert {a.id: (a.terminal_resume_argv, a.terminal_fresh_argv)
            for a in backends.adapters()} == expected


def _terminal_handler():
    class Handler:
        headers = {"Upgrade": "websocket", "Connection": "Upgrade",
                   "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ=="}
        wfile = SimpleNamespace(write=lambda *_: None, flush=lambda: None)
        sent: list = []

        def send_response(self, code):
            self.sent.append(code)

        def send_header(self, *_):
            pass

        def end_headers(self):
            pass
    return Handler()


@pytest.mark.parametrize("backend,bsid,argv0", [
    ("claude", "s-1", "claude"), ("claude", "", "claude"),
    ("codex", "s-2", "codex"), ("agy", "", "agy"),
])
def test_terminal_launch_uses_the_adapter_argv(monkeypatch, backend, bsid, argv0):
    from lib import terminal_ws
    seen: dict[str, str] = {}
    monkeypatch.setattr(terminal_ws.agents_db, "get_by_session",
                        lambda s: {"agent_id": "a1", "backend": backend, "cwd": "/"})
    monkeypatch.setattr(terminal_ws.agents_db, "live_backend_session", lambda a: bsid)
    monkeypatch.setattr(terminal_ws.shutil, "which",
                        lambda name: seen.setdefault("binary", name) and None)
    handler = _terminal_handler()
    terminal_ws.serve_terminal(handler, "any")
    assert seen["binary"] == argv0
    assert handler.sent == [500]  # "<argv0> not on PATH": stopped before the PTY


def test_terminal_launch_is_unsupported_for_adapters_without_argv(monkeypatch):
    from lib import terminal_ws
    monkeypatch.setattr(terminal_ws.agents_db, "get_by_session",
                        lambda s: {"agent_id": "a1", "backend": "grok", "cwd": "/"})
    monkeypatch.setattr(terminal_ws.agents_db, "live_backend_session", lambda a: "")
    handler = _terminal_handler()
    terminal_ws.serve_terminal(handler, "any")
    assert handler.sent == [501]


def test_resume_target_drives_transcript_checks():
    """turn_dispatch's ghost-session guard and account-switch resume check
    both hinge on whether --resume points at a transcript file."""
    assert backends.adapter_for("claude").resumes_by_transcript_file
    for backend in ("codex", "agy", "grok", "opencode", "deepseek"):
        assert not backends.adapter_for(backend).resumes_by_transcript_file
    # Aliases and garbage resolve the way normalize() does.
    assert backends.adapter_for("antigravity").id == "agy"
    assert backends.adapter_for("nonsense").id == "claude"


def test_account_pool_lookup_matches_the_coordinators(monkeypatch):
    from lib import turn_dispatch as td
    claude_pool, codex_pool = object(), object()
    monkeypatch.setattr(td, "_CLAUDE_FAILOVER", claude_pool)
    monkeypatch.setattr(td, "_CODEX_FAILOVER", codex_pool)
    monkeypatch.setattr(td.config, "load", lambda: SimpleNamespace(
        claude_account_switch_command=("claude-switch",),
        codex_account_switch_command=("codex-switch",)))
    assert td.account_failover("claude") is claude_pool
    assert td.account_failover("codex") is codex_pool
    assert td.account_selector("claude") == ("claude-switch",)
    assert td.account_selector("codex") == ("codex-switch",)
    # Backends without a pool keep book-keeping in Claude's, as before.
    assert td.account_failover("grok") is claude_pool
    assert td.account_selector("agy") == ("claude-switch",)


def test_spawn_kwargs_per_backend():
    kwargs = {"text": "hi", "cwd": "/x", "stream": None, "synthesize_audio": True,
              "hook_session": "h", "run_if_owned": "gate", "voice_preamble": True}
    claude = backends.adapter_for("claude").spawn_kwargs(kwargs)
    assert claude == {"text": "hi", "cwd": "/x", "stream": None, "hook_session": "h"}
    codex = backends.adapter_for("codex").spawn_kwargs(kwargs)
    assert codex == {"text": "hi", "cwd": "/x", "stream": None, "voice_preamble": True}
    agy = backends.adapter_for("agy").spawn_kwargs(kwargs)
    assert agy == {**codex, "run_if_owned": "gate"}
    for backend in ("grok", "opencode", "deepseek"):
        assert backends.adapter_for(backend).spawn_kwargs(kwargs) == codex


def test_executable_honours_the_claude_override(monkeypatch):
    from lib import clarp_runner
    monkeypatch.setattr(clarp_runner, "configured_claude_bin", lambda: "clarp")
    assert backends.adapter_for("claude").executable() == "clarp"
    assert backends.adapter_for("codex").executable() == "codex"
    assert backends.adapter_for("deepseek").executable() == "opencode"


def test_provider_to_backend_mapping():
    assert backends.for_provider("openai") == "codex"
    for backend in IDS:
        assert backends.for_provider(backend) == backend


def test_goal_and_steer_follow_the_adapter():
    with pytest.raises(backends.GoalUnsupported):
        backends.goal("grok", "a1", "status")
    assert backends.steer_turn("claude", "a1", "hi") is False
    assert backends.steer_turn("agy", "a1", "hi") is False
