"""Backends are polymorphic: behaviour a CLI differs on lives on its
``Backend`` class and is looked up, never branched on by identity.

Two halves: a source guard that fails when a new ``backend == X`` branch
appears outside the registry and the per-CLI transcript parsers, and unit
tests that every backend declares every capability plus a few of the
decisions the old adapter rows replaced (terminal argv, resume-target
checks, spawn kwargs).
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
from lib.backend import registry  # noqa: E402

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
# The facade, the model catalogue and the per-CLI transcript parsers are
# allowed to know which CLI they are. The runner bodies live on the classes
# in ``lib.backend``, which are guarded below like any other caller.
EXCLUDED = {"backends.py", "provider_capabilities.py"}
EXCLUDED_SUFFIXES = ("_transcript.py",)

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


# The strategy package (docs/architecture/backend-strategy.md): only its
# registry may list ids; the base and the per-CLI classes are guarded like
# any other caller.
PACKAGE = ROOT / "server" / "lib" / "backend"
PACKAGE_ID_LISTER = "registry.py"


def _guarded_files() -> list[pathlib.Path]:
    lib = ROOT / "server" / "lib"
    files = [p for p in lib.glob("*.py")
             if p.name not in EXCLUDED and not p.name.endswith(EXCLUDED_SUFFIXES)]
    package = [p for p in PACKAGE.glob("*.py") if p.name != PACKAGE_ID_LISTER]
    return sorted(files) + sorted(package) + [ROOT / "server" / "server.py"]


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
        "Backend identity branch found; give the Backend class in "
        "server/lib/backend/ a method or attribute and call it instead:\n"
        + "\n".join(offenders))
    stale = set(ALLOWLIST) - seen_allowed
    assert not stale, f"allowlist entries no longer match any line: {sorted(stale)}"


_ID_LISTING = re.compile(
    rf"\bAgentBackend\.{_IDS}\b|\bbackends\.{_IDS}\b|{_LIT}")


def test_registry_is_the_only_package_file_that_lists_ids():
    """A backend class declares its own id in its catalogue data block;
    naming a sibling by id anywhere else inside the package would be the
    identity branch the contract forbids."""
    offenders = []
    for path in sorted(PACKAGE.glob("*.py")):
        if path.name == PACKAGE_ID_LISTER:
            continue
        in_data_block = False
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if line.strip().startswith("# --- catalogue data"):
                in_data_block = True
                continue
            if in_data_block and not line.strip():
                in_data_block = False
            if in_data_block:
                continue
            if _ID_LISTING.search(line):
                offenders.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()}")
    assert not offenders, (
        "Backend ids belong in server/lib/backend/registry.py only:\n"
        + "\n".join(offenders))
    listed = _ID_LISTING.findall((PACKAGE / PACKAGE_ID_LISTER).read_text())
    assert len(listed) >= len(IDS)


# --- (5) every adapter declares every capability -----------------------------

REQUIRED_STR = ("api_providers", "janitor_default_model", "model_family")
REQUIRED_BOOL = ("native_tool_explainer",)
REQUIRED_CALLABLE = ()
OPTIONAL = ("context_window",)
# Decisions that are methods on the Backend classes now (slices 2 and 4 of
# the contract), so no class may declare them as flags any more.
# ``supports_compact`` is not among them: it is a property derived from
# ``compaction()``, never declared.
DELETED_FLAGS = ("preassigns_session_id", "hook_source_marker", "account_pool",
                 "usage_limit_recovery", "restarts_runner_on_credential_change",
                 "terminal_resume_argv", "terminal_fresh_argv",
                 "terminal_loads_plugin", "supports_account_failover",
                 # slice 4
                 "resume_target", "resume_transcript_finder",
                 "transcript_dir_encodes_cwd", "resumes_by_transcript_file",
                 "locks_turn_callbacks", "records_classified_usage_limit",
                 "quota_reset_jittered", "compact_launch", "compact_command",
                 "compaction_watches_transcript",
                 "recorded_model_source", "transcript_module",
                 "session_catalog_reader", "transcript_reader_injected",
                 "spawn_kwargs", "goal_module", "supports_goal",
                 "executable_resolver", "model_validator")


@pytest.mark.parametrize("backend", IDS)
def test_every_adapter_declares_every_capability(backend):
    # Behaviour lives on the Backend object as methods; the names that used
    # to be flags may exist only as callables now, never as data.
    adapter = backends._BY_ID[backends.normalize(backend)]
    assert adapter is not None
    for name in REQUIRED_STR:
        assert isinstance(getattr(adapter, name), (str, tuple)), name
    for name in REQUIRED_BOOL:
        assert isinstance(getattr(adapter, name), bool), name
    for name in REQUIRED_CALLABLE:
        assert callable(getattr(adapter, name)), name
    for name in OPTIONAL:
        assert hasattr(adapter, name), name
    for name in DELETED_FLAGS:
        value = getattr(adapter, name, None)
        assert value is None or callable(value), f"{name} is data, not behaviour"


def test_declared_values_match_the_behaviour_they_replaced():
    by = {a.id: a for a in registry.all()}
    assert {a.id for a in registry.all()} == set(IDS)

    # Claude is the only CLI with a context gauge.
    claude = by["claude"]
    assert claude.context_window == 1_000_000
    for other in IDS[1:]:
        assert by[other].context_window is None, other

    # Codex owns the goal protocol, the classified-limit event, the OpenAI
    # routing provider and the native tool explainer.
    codex = by["codex"]
    assert codex.api_providers == ("openai",) and codex.native_tool_explainer
    assert codex.janitor_default_model == "gpt-5.3-codex-spark"
    for other in ("claude", "agy", "grok", "opencode", "deepseek"):
        a = by[other]
        assert a.api_providers == () and not a.native_tool_explainer, other
        assert a.janitor_default_model == "", other

    # Account failover pools: Claude and Codex only (a method now).
    assert {b.id: b.account_pool() for b in registry.all()} == {
        "claude": "claude", "codex": "codex", "agy": "", "grok": "",
        "opencode": "", "deepseek": ""}
    assert [b.id for b in registry.all() if b.account_pool()] == ["claude", "codex"]

    # AGY folds effort into the model id and validates model ids.
    assert by["agy"].effort_compatibility_unknown and by["agy"].model_carries_effort
    for other in ("claude", "codex", "grok", "opencode", "deepseek"):
        assert not by[other].effort_compatibility_unknown, other

    # Model families for avatars: Claude and OpenCode front several families.
    assert {a.id: a.model_family for a in registry.all()} == {
        "claude": "", "codex": "codex", "agy": "gemini", "grok": "grok",
        "opencode": "", "deepseek": "deepseek"}



# --- (5) replaced decisions -------------------------------------------------

def test_interactive_terminal_argv_per_backend(monkeypatch):
    """The table terminal_ws.py used to carry, now each backend's terminal_argv."""
    from lib.backend.base import Unsupported
    monkeypatch.setattr("lib.deployment.plugin_dir", lambda: None)
    expected = {
        "claude": (["claude", "--dangerously-skip-permissions", "--resume", "s"],
                   ["claude", "--dangerously-skip-permissions"]),
        "codex": (["codex", "resume", "s"], ["codex"]),
        "agy": (["agy", "--dangerously-skip-permissions", "--conversation", "s"],
                ["agy", "--dangerously-skip-permissions"]),
        # No interactive terminal launch is defined for these yet; today the
        # /terminal route fails for them, and still does.
        "grok": None, "opencode": None, "deepseek": None,
    }
    for backend in registry.all():
        if expected[backend.id] is None:
            for sid in ("s", ""):
                with pytest.raises(Unsupported):
                    backend.terminal_argv(sid)
            continue
        assert (backend.terminal_argv("s"), backend.terminal_argv("")) == expected[backend.id]


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


def test_resume_target_drives_transcript_checks(tmp_path):
    """turn_dispatch's ghost-session guard and account-switch resume check
    both hinge on whether the backend has a resume target for the binding:
    Claude checks the transcript is on disk, the rest resume by id."""
    home = tmp_path
    (home / ".claude" / "projects" / "-w").mkdir(parents=True)
    (home / ".claude" / "projects" / "-w" / "real.jsonl").write_text("")
    claude = backends.by_id("claude")
    assert claude.resume_target("real", "/w", home) == home / ".claude" / "projects" / "-w" / "real.jsonl"
    assert claude.resume_target("ghost", "/w", home) is None
    for backend in ("codex", "agy", "grok", "opencode", "deepseek"):
        assert backends.by_id(backend).resume_target("ghost", "/w", home) == "ghost", backend
    # Aliases and garbage resolve the way normalize() does.
    assert backends.by_id("antigravity").id == "agy"
    assert backends.by_id("nonsense").id == "claude"


def test_account_pool_lookup_matches_the_coordinators(monkeypatch):
    from lib import turn_dispatch as td
    # One coordinator per pool the backends declare, none listed by hand.
    assert set(td._FAILOVERS) == {"claude", "codex"}
    claude_pool, codex_pool = object(), object()
    monkeypatch.setitem(td._FAILOVERS, "claude", claude_pool)
    monkeypatch.setitem(td._FAILOVERS, "codex", codex_pool)
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


def test_spawn_kwargs_per_backend(monkeypatch):
    """Each backend's spawn_turn keeps the keywords its own runner takes."""
    from lib import codex_app_server
    seen: dict[str, dict] = {}
    for backend in registry.all():
        monkeypatch.setattr(backend, "start_turn",
                            lambda _n=backend.id, **kw: seen.__setitem__(_n, kw))
    monkeypatch.setattr(codex_app_server, "spawn_turn",
                        lambda **kw: seen.__setitem__("codex", kw))
    kwargs = {"text": "hi", "cwd": "/x", "stream": None, "synthesize_audio": True,
              "hook_session": "h", "run_if_owned": "gate", "voice_preamble": True}
    backends.by_id("claude").spawn_turn(**kwargs)
    assert seen["claude"] == {"text": "hi", "cwd": "/x", "stream": None, "hook_session": "h"}
    backends.by_id("codex").spawn_turn(**kwargs)
    codex = {"text": "hi", "cwd": "/x", "stream": None, "voice_preamble": True}
    assert seen["codex"] == codex
    backends.by_id("agy").spawn_turn(**kwargs)
    assert seen["agy"] == {**codex, "run_if_owned": "gate"}
    for backend in ("grok", "opencode", "deepseek"):
        backends.by_id(backend).spawn_turn(**kwargs)
        assert seen[backend] == codex, backend


def test_executable_honours_the_claude_override(monkeypatch):
    from lib import config
    monkeypatch.setattr(config, "load", lambda: SimpleNamespace(claude_cli="clarp"))
    assert backends.by_id("claude").executable() == "clarp"
    assert backends.by_id("codex").executable() == "codex"
    assert backends.by_id("deepseek").executable() == "opencode"
    assert backends.capabilities("claude").required_binary == "clarp"


def test_provider_to_backend_mapping():
    assert backends.for_provider("openai") == "codex"
    for backend in IDS:
        assert backends.for_provider(backend) == backend


def test_goal_and_steer_follow_the_backend(monkeypatch):
    from lib import codex_app_server
    from lib.backend.base import Unsupported
    monkeypatch.setattr(codex_app_server, "goal",
                        lambda agent_id, action, *, objective="", stream=None: {"objective": objective})
    monkeypatch.setattr(codex_app_server, "steer",
                        lambda agent_id, text, *, client_msg_id="", synthesize_audio=False: True)
    assert backends.goal("codex", "a1", "start", objective="ship") == {"objective": "ship"}
    assert backends.steer_turn("codex", "a1", "hi") is True
    with pytest.raises(backends.GoalUnsupported, match="Grok"):
        backends.goal("grok", "a1", "status")
    with pytest.raises(Unsupported):
        backends.by_id("grok").goal("a1", "status")
    with pytest.raises(Unsupported):
        backends.by_id("claude").steer("a1", "hi")
    assert backends.steer_turn("claude", "a1", "hi") is False
    assert backends.steer_turn("agy", "a1", "hi") is False
