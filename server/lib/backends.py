"""AI-CLI backend facade.

Each coding CLI is a ``Backend`` strategy object in ``lib.backend``
(``docs/architecture/backend-strategy.md``); ``by_id()`` / ``for_agent()``
hand them out. This module stays the import path callers use: the id
constants, ``normalize()`` (the one alias normaliser) and, while the
migration runs, the ``BackendAdapter`` rows the strategies still delegate
to for their catalogue metadata, presentation and capability flags.
Clients do not hardcode provider ids — they render
``/agent-model-options``, including the label, brand colours, symbol and
``supports_*`` flags carried here, so a new CLI looks intentional in the
apps without an app release.
"""
from __future__ import annotations

import importlib
import pathlib
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from .protocol import AgentBackend

CLAUDE = AgentBackend.CLAUDE
CODEX = AgentBackend.CODEX
AGY = AgentBackend.AGY
GROK = AgentBackend.GROK
OPENCODE = AgentBackend.OPENCODE
DEEPSEEK = AgentBackend.DEEPSEEK
DEFAULT = CLAUDE
_RUNTIME_CLIENT: Any | None = None

# One runtime ``status`` RPC per short window, shared by every caller in this
# process.  A snapshot asks active_handles() once per agent, so a 120-agent
# fleet opened 120 runtime connections per poll from dozens of HTTP threads
# and overran the runtime socket's listen backlog (2026-09-12).  The window is
# short enough that the spawn/finish race it adds is no wider than the one a
# point-in-time RPC already had, and dispatch invalidates it explicitly.
RUNTIME_STATUS_TTL = 0.25
_STATUS_LOCK = threading.Lock()
# (client the result came from, monotonic time, result, error)
_STATUS_CACHE: tuple[Any | None, float, dict | None, BaseException | None] = (
    None, 0.0, None, None)
_clock = time.monotonic


def configure_runtime_client(client: Any | None) -> None:
    """Route process ownership calls to the external runtime when configured."""
    global _RUNTIME_CLIENT
    _RUNTIME_CLIENT = client
    invalidate_runtime_status()


def invalidate_runtime_status() -> None:
    """Forget the shared status window, e.g. right after this process dispatched."""
    global _STATUS_CACHE
    with _STATUS_LOCK:
        _STATUS_CACHE = (None, 0.0, None, None)


def runtime_status(*, max_age: float | None = None) -> dict:
    """The runtime's ``status`` result, at most ``max_age`` seconds old.

    Concurrent callers share one in-flight RPC.  A failure is remembered for
    the same window and logged once, so a runtime outage costs one connection
    and one journal line per window instead of one per agent.
    """
    global _STATUS_CACHE
    if _RUNTIME_CLIENT is None:
        raise RuntimeError("no external runtime client configured")
    ttl = RUNTIME_STATUS_TTL if max_age is None else max_age
    with _STATUS_LOCK:
        client, fetched_at, result, error = _STATUS_CACHE
        if client is not _RUNTIME_CLIENT or _clock() - fetched_at >= ttl:
            client = _RUNTIME_CLIENT
            try:
                result, error = client.status(), None
            except Exception as exc:  # noqa: BLE001 - remembered for the window
                result, error = None, exc
                from .log import log_exception
                log_exception("runtimeStatusUnavailable", exc, detail="shared-status")
            _STATUS_CACHE = (client, _clock(), result, error)
        if error is not None:
            raise error
        return result if isinstance(result, dict) else {}


@dataclass(frozen=True)
class _RemoteHandle:
    trace_id: str

    def is_alive(self) -> bool:
        return True


@dataclass(frozen=True)
class BackendCapabilities:
    supports_fork: bool
    supports_transcript_streaming: bool
    required_binary: str


@dataclass(frozen=True)
class BackendBrand:
    """Chooser colours as ``#rrggbb`` strings.

    Served on the catalogue so a client never has to ship a palette per CLI:
    a new adapter picks its own field and tint and every app renders it.
    """
    field_top: str
    field_bottom: str
    tint_dark: str
    tint_light: str

    def as_dict(self) -> dict[str, str]:
        return {
            "field_top": self.field_top,
            "field_bottom": self.field_bottom,
            "tint_dark": self.tint_dark,
            "tint_light": self.tint_light,
        }


# Neutral treatment for an adapter that declares no brand of its own.
DEFAULT_BRAND = BackendBrand("#2a3142", "#151820", "#a8b4c8", "#4a5568")
DEFAULT_SYMBOL = "cpu"

# How a client obtains credentials for the CLI. "none" hides the sign-in row.
LOGIN_KINDS = ("none", "device_code", "cli", "api_key")
# How a client offers the effort control. "folded_into_model" means the model
# id already encodes the effort (AGY), so the picker is replaced by a note.
EFFORT_UIS = ("picker", "hidden", "folded_into_model")
# Where effort evidence lives: per model ("model"), one list for the whole
# provider ("provider"), or a provider flag whose compatibility with a chosen
# model is unknown ("provider_flag").
EFFORT_SCOPES = ("model", "provider", "provider_flag")
# What the CLI's ``--resume`` style flag points at: a transcript file the
# Host can check exists on disk before resuming ("transcript_file"), or an
# opaque session id the CLI resolves itself ("session_id").
RESUME_TARGETS = ("transcript_file", "session_id")


def _mod(name: str):
    return importlib.import_module(f"lib.{name}")


def _lazy(module: str, attr: str) -> Callable[..., Any]:
    """A callable that resolves ``lib.<module>.<attr>`` at call time.

    Adapters are built at import, before the runner modules are loaded, and
    tests monkeypatch those module attributes; binding late keeps both
    working.
    """
    def call(*args: Any, **kwargs: Any) -> Any:
        return getattr(_mod(module), attr)(*args, **kwargs)
    call.__name__ = call.__qualname__ = f"{module}.{attr}"
    return call


def _claude_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "text", "cwd", "backend_session_id", "is_new_session", "session",
        "agent_id", "on_session_init", "on_result", "on_error", "trace_id",
        "model", "effort", "stream", "isolated", "hook_session",
    }
    return {k: v for k, v in kwargs.items() if k in allowed}


def _stream_kwargs(kwargs: dict[str, Any], *, owner_gate: bool = False) -> dict[str, Any]:
    return {
        k: v for k, v in kwargs.items()
        if k not in ({"synthesize_audio", "hook_session"}
                     | (set() if owner_gate else {"run_if_owned"}))
    }


def _owner_gated_stream_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Stream kwargs for a runner that admits ``run_if_owned`` (AGY)."""
    return _stream_kwargs(kwargs, owner_gate=True)


def _declared_binary(adapter: "BackendAdapter") -> str:
    return adapter.required_binary


def _configured_claude_binary(adapter: "BackendAdapter") -> str:
    """Claude's executable is a Host setting (official CLI or clarp wrapper)."""
    return _mod("clarp_runner").configured_claude_bin()


def _claude_resume_transcript(backend: str, session_id: str, cwd: str,
                              projects_root: pathlib.Path | None):
    from .resume import find_session_jsonl as find_claude_session_jsonl
    root = projects_root or (pathlib.Path.home() / ".claude" / "projects")
    return find_claude_session_jsonl(session_id, cwd, root)


def _catalogued_resume_transcript(backend: str, session_id: str, cwd: str,
                                  projects_root: pathlib.Path | None):
    return find_session_jsonl(backend, session_id)


def _claude_session_catalog(adapter: "BackendAdapter", cwd: str, *,
                            limit: int, all_projects: bool) -> list[dict]:
    from . import session_catalog
    return session_catalog.list_claude_sessions(
        cwd, all_projects=all_projects, limit=limit)


def _transcript_session_catalog(adapter: "BackendAdapter", cwd: str, *,
                                limit: int, all_projects: bool) -> list[dict]:
    list_fn = getattr(_mod(adapter.transcript_module), "list_sessions")
    try:
        return list_fn(cwd if not all_projects else "", limit=limit,
                       all_projects=all_projects)
    except TypeError:
        return list_fn("" if all_projects else cwd, limit=limit)


@dataclass(frozen=True)
class RecordedModelSource:
    """Where ``session_models`` reads the model a session actually ran.

    ``transcript`` locates the session's transcript (the model is read from
    its last turn), ``indexed_model`` consults the CLI's own session index,
    and ``cli_default_model`` reads the model the CLI would launch with when
    the Host pins none. ``None`` means the CLI offers no such source.
    """
    transcript: Callable[[str], pathlib.Path | None] | None = None
    indexed_model: Callable[[str], str] | None = None
    cli_default_model: Callable[[], str] | None = None


NO_RECORDED_MODEL = RecordedModelSource()


@dataclass(frozen=True)
class BackendAdapter:
    """One coding CLI the Host can run as an agent backend."""
    id: str
    label: str
    required_binary: str
    supports_fork: bool = False
    supports_steer: bool = False
    supports_transcript_streaming: bool = False
    efforts: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    badge: str = ""
    # Presentation the chooser needs; nothing here requires a client build.
    detail: str = ""
    symbol: str = DEFAULT_SYMBOL
    brand: BackendBrand = DEFAULT_BRAND
    hidden: bool = False
    # Capability flags advertised on the catalogue. Compact, routing and auth
    # are derived from the machinery below rather than declared twice.
    supports_mcp: bool = False
    supports_usage: bool = False
    login_kind: str = "none"
    effort_ui: str = "picker"
    effort_help: str = ""
    effort_scope: str = "provider"
    # Module exposing routing_cmd()/routing_text() for one isolated
    # orchestrator request. Empty means the CLI cannot route.
    routing_module: str = ""
    runner_module: str = ""
    transcript_module: str = ""
    extra_interrupt_modules: tuple[str, ...] = ()
    config_model_field: str = ""
    config_effort_field: str = ""
    compact_launch: tuple[str, ...] | None = None
    compact_command: str | None = None
    fallback_models: tuple[tuple[str, str], ...] = ()
    resumable: bool = True
    # --- Dispatch strategy -------------------------------------------------
    # Which of the Host's spawn kwargs the runner's spawn_turn accepts.
    spawn_kwargs: Callable[[dict[str, Any]], dict[str, Any]] = _stream_kwargs
    # Module exposing goal(); empty means the CLI has no goal protocol.
    goal_module: str = ""
    # Resolves the executable to launch; Claude's is a Host setting.
    executable_resolver: Callable[["BackendAdapter"], str] = _declared_binary
    # Validates a pinned model id; None accepts anything non-empty.
    model_validator: Callable[[str], bool] | None = None
    # --- Session and transcript access -----------------------------------
    resume_target: str = "session_id"
    # The Host allocates the session id before the first spawn (--session-id).
    preassigns_session_id: bool = False
    # Locates the transcript to resume from, given (backend, session_id, cwd,
    # projects_root).
    resume_transcript_finder: Callable[..., Any] = _catalogued_resume_transcript
    # Lists past sessions for the adopt picker, given (adapter, cwd, ...).
    session_catalog_reader: Callable[..., list[dict]] = _transcript_session_catalog
    # The transcript's directory name encodes the cwd (Claude's project dirs).
    transcript_dir_encodes_cwd: bool = False
    # The Host injects Claude's transcript finder/parser into
    # conversation.load_conversation; other CLIs read through the registry.
    transcript_reader_injected: bool = False
    recorded_model_source: RecordedModelSource = NO_RECORDED_MODEL
    # --- Turn plumbing -----------------------------------------------------
    # The runner reports source/state through Clarp's hook plugin, armed by a
    # per-turn marker file.
    hook_source_marker: bool = False
    # Runner callbacks arrive on the runner's own threads; serialise them
    # under the dispatch lock.
    locks_turn_callbacks: bool = False
    # Account pool the usage-limit failover coordinator manages; empty means
    # no account switching for this CLI.
    account_pool: str = ""
    # Attempts to recover a usage-limit failure in-process before failing
    # over accounts; None means the CLI has no such protocol.
    usage_limit_recovery: Callable[[str], bool] | None = None
    # A classified usage-limit failure is recorded as a provider limit event.
    records_classified_usage_limit: bool = False
    # A sign-in/sign-out rewrites credentials another process holds open, so
    # the long-lived runner is recycled to re-read them.
    restarts_runner_on_credential_change: bool = False
    # Context gauge: the window (tokens) the CLI's transcript fills, or None
    # when the CLI auto-compacts and shows no gauge.
    context_window: int | None = None
    # Compaction waits for the transcript to settle rather than a fixed window.
    compaction_watches_transcript: bool = False
    # The CLI reports quota resets with fractional-second jitter, so the
    # notification identity rounds them.
    quota_reset_jittered: bool = False
    # --- Interactive terminal --------------------------------------------
    # argv prefix for /terminal: resume (session id appended) and fresh. None
    # means the Host cannot open an interactive terminal for this CLI.
    terminal_resume_argv: tuple[str, ...] | None = None
    terminal_fresh_argv: tuple[str, ...] | None = None
    terminal_loads_plugin: bool = False
    # --- Routing and Janitors --------------------------------------------
    # API-key providers whose model catalogue this CLI fronts ("openai").
    api_providers: tuple[str, ...] = ()
    # The Janitor tool explainer can run natively through this CLI.
    native_tool_explainer: bool = False
    # Model a new Janitor gets when none is chosen; empty means CLI default.
    janitor_default_model: str = ""
    # Model family the CLI stands for when the Agent pins no model (avatars).
    model_family: str = ""

    def __post_init__(self) -> None:
        if self.login_kind not in LOGIN_KINDS:
            raise ValueError(f"{self.id}: unknown login_kind {self.login_kind!r}")
        if self.effort_ui not in EFFORT_UIS:
            raise ValueError(f"{self.id}: unknown effort_ui {self.effort_ui!r}")
        if self.effort_scope not in EFFORT_SCOPES:
            raise ValueError(f"{self.id}: unknown effort_scope {self.effort_scope!r}")
        if self.resume_target not in RESUME_TARGETS:
            raise ValueError(f"{self.id}: unknown resume_target {self.resume_target!r}")

    @property
    def supports_compact(self) -> bool:
        return bool(self.compact_launch and self.compact_command)

    @property
    def supports_routing(self) -> bool:
        return bool(self.routing_module)

    @property
    def supports_auth(self) -> bool:
        return self.login_kind != "none"

    @property
    def supports_goal(self) -> bool:
        return bool(self.goal_module)

    @property
    def supports_account_failover(self) -> bool:
        return bool(self.account_pool)

    @property
    def effort_compatibility_unknown(self) -> bool:
        """Effort is a provider flag whose fit with a pinned model is unknown."""
        return self.effort_scope == "provider_flag"

    @property
    def model_carries_effort(self) -> bool:
        return self.effort_ui == "folded_into_model"

    @property
    def resumes_by_transcript_file(self) -> bool:
        return self.resume_target == "transcript_file"

    def executable(self) -> str:
        """The binary to launch, after any Host-level override."""
        return self.executable_resolver(self)

    def is_valid_model(self, model: str | None) -> bool:
        value = (model or "").strip()
        if not value or self.model_validator is None:
            return True
        return bool(self.model_validator(value))

    def catalogue_fields(self, sort_index: int) -> dict[str, Any]:
        """The presentation and capability block of one catalogue row."""
        return {
            "label": self.label,
            "detail": self.detail or f"Runs on {self.label}.",
            "badge": self.badge,
            "symbol": self.symbol or DEFAULT_SYMBOL,
            "brand": self.brand.as_dict(),
            "sort_index": sort_index,
            "hidden": self.hidden,
            "supports_fork": bool(self.supports_fork),
            "resumable": bool(self.resumable),
            "supports_resume": bool(self.resumable),
            "supports_steer": bool(self.supports_steer),
            "supports_compact": self.supports_compact,
            "supports_mcp": bool(self.supports_mcp),
            "supports_routing": self.supports_routing,
            "supports_auth": self.supports_auth,
            "supports_usage": bool(self.supports_usage),
            "login_kind": self.login_kind,
            "effort_ui": self.effort_ui,
            "effort_help": self.effort_help,
        }

    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            supports_fork=self.supports_fork,
            supports_transcript_streaming=self.supports_transcript_streaming,
            required_binary=self.executable(),
        )


_ADAPTERS: tuple[BackendAdapter, ...] = (
    BackendAdapter(
        id=CLAUDE, label="Claude", required_binary="claude",
        supports_fork=True, supports_transcript_streaming=True,
        efforts=("low", "medium", "high", "xhigh", "max"),
        badge="BackendClaude",
        detail="Runs on Claude Code.",
        symbol="sparkles",
        brand=BackendBrand("#e08b6a", "#c9603d", "#d97757", "#b85433"),
        supports_mcp=True,
        supports_usage=True,
        login_kind="cli",
        effort_scope="model",
        routing_module="clarp_runner",
        runner_module="clarp_runner",
        transcript_module="transcript_log",
        config_model_field="claude_model",
        config_effort_field="claude_effort",
        compact_launch=("claude", "--dangerously-skip-permissions", "--resume"),
        compact_command="/compact",
        spawn_kwargs=_claude_kwargs,
        goal_module="",
        executable_resolver=_configured_claude_binary,
        model_validator=None,
        resume_target="transcript_file",
        preassigns_session_id=True,
        resume_transcript_finder=_claude_resume_transcript,
        session_catalog_reader=_claude_session_catalog,
        transcript_dir_encodes_cwd=True,
        transcript_reader_injected=True,
        recorded_model_source=RecordedModelSource(
            transcript=_lazy("transcript_log", "find_latest_jsonl"),
            indexed_model=None,
            cli_default_model=_lazy("session_models", "claude_cli_default_model"),
        ),
        hook_source_marker=True,
        locks_turn_callbacks=True,
        account_pool="claude",
        usage_limit_recovery=None,
        records_classified_usage_limit=False,
        restarts_runner_on_credential_change=False,
        # This deployment runs opus-*[1m] (the 1M context beta, per
        # ~/.claude.json); the native gauge divides tokens by this.
        context_window=1_000_000,
        compaction_watches_transcript=True,
        quota_reset_jittered=True,
        terminal_resume_argv=("claude", "--dangerously-skip-permissions", "--resume"),
        terminal_fresh_argv=("claude", "--dangerously-skip-permissions"),
        terminal_loads_plugin=True,
        api_providers=(),
        native_tool_explainer=False,
        janitor_default_model="",
        # Claude fronts several model families, so the backend alone is no
        # evidence of which model is answering.
        model_family="",
        fallback_models=(
            ("fable", "Fable"),
            ("opus", "Opus"),
            ("sonnet", "Sonnet"),
            ("haiku", "Haiku"),
            ("claude-fable-5-1", "Claude Fable 5.1"),
            ("claude-opus-5-5", "Claude Opus 5.5"),
            ("claude-opus-5", "Claude Opus 5"),
            ("claude-sonnet-5", "Claude Sonnet 5"),
            ("claude-haiku-4-5", "Claude Haiku 4.5"),
            ("claude-opus-4-8", "Claude Opus 4.8"),
            ("claude-sonnet-4-6", "Claude Sonnet 4.6"),
        ),
    ),
    BackendAdapter(
        id=CODEX, label="Codex", required_binary="codex",
        supports_steer=True,
        # The live catalogue (`codex debug models`) reports xhigh/max on every
        # currently listed model and ultra on the 5.6 family; the old
        # three-level tuple silently dropped anything above `high`, so a pinned
        # xhigh agent ran at the CLI default instead.
        efforts=("low", "medium", "high", "xhigh", "max", "ultra"),
        badge="BackendCodex",
        detail="Runs on the Codex CLI.",
        symbol="terminal",
        brand=BackendBrand("#2b2f3c", "#14161d", "#c0caf5", "#3c4257"),
        supports_usage=True,
        login_kind="device_code",
        effort_scope="model",
        routing_module="codex_runner",
        runner_module="codex_app_server",
        transcript_module="codex_transcript",
        extra_interrupt_modules=("codex_runner",),
        config_model_field="codex_model",
        config_effort_field="codex_reasoning_effort",
        compact_launch=("codex", "resume"),
        compact_command="/compact",
        spawn_kwargs=_stream_kwargs,
        goal_module="codex_app_server",
        executable_resolver=_declared_binary,
        model_validator=None,
        resume_target="session_id",
        preassigns_session_id=False,
        resume_transcript_finder=_catalogued_resume_transcript,
        session_catalog_reader=_transcript_session_catalog,
        transcript_dir_encodes_cwd=False,
        transcript_reader_injected=False,
        recorded_model_source=RecordedModelSource(
            transcript=_lazy("session_models", "codex_transcript_path"),
            indexed_model=_lazy("session_models", "codex_indexed_model"),
            cli_default_model=_lazy("session_models", "codex_cli_default_model"),
        ),
        hook_source_marker=False,
        locks_turn_callbacks=False,
        account_pool="codex",
        usage_limit_recovery=_lazy("codex_app_server", "recover_usage_failure"),
        records_classified_usage_limit=True,
        restarts_runner_on_credential_change=True,
        context_window=None,
        compaction_watches_transcript=False,
        quota_reset_jittered=False,
        terminal_resume_argv=("codex", "resume"),
        terminal_fresh_argv=("codex",),
        terminal_loads_plugin=False,
        # Codex is the catalogue that lists GPT models; the OpenAI API
        # routing provider executes through it.
        api_providers=("openai",),
        native_tool_explainer=True,
        janitor_default_model="gpt-5.3-codex-spark",
        model_family="codex",
        fallback_models=(
            ("gpt-5.4", "GPT-5.4"),
            ("gpt-5.4-mini", "GPT-5.4 Mini"),
            ("gpt-5.2-codex", "GPT-5.2 Codex"),
            ("gpt-5.1-codex-max", "GPT-5.1 Codex Max"),
            ("gpt-5.1-codex", "GPT-5.1 Codex"),
            ("gpt-5-codex", "GPT-5 Codex"),
        ),
    ),
    BackendAdapter(
        id=AGY, label="Antigravity", required_binary="agy",
        efforts=("low", "medium", "high"),
        aliases=("antigravity",),
        badge="BackendAntigravity",
        detail="Runs on Antigravity.",
        symbol="circle.hexagongrid",
        brand=BackendBrand("#1d2742", "#0e1424", "#4c8ef7", "#2b6ed6"),
        # AGY model ids carry their own effort suffix ("...-flash-high"), so
        # the effort picker is folded into the model choice.
        effort_ui="folded_into_model",
        effort_help="Included in model choice",
        effort_scope="provider_flag",
        routing_module="agy_runner",
        runner_module="agy_runner",
        transcript_module="agy_transcript",
        config_model_field="agy_model",
        compact_launch=("agy", "--dangerously-skip-permissions", "--conversation"),
        compact_command="/compress",
        spawn_kwargs=_owner_gated_stream_kwargs,
        goal_module="",
        executable_resolver=_declared_binary,
        model_validator=_lazy("provider_capabilities", "is_dispatchable_agy_model"),
        resume_target="session_id",
        preassigns_session_id=False,
        resume_transcript_finder=_catalogued_resume_transcript,
        session_catalog_reader=_transcript_session_catalog,
        transcript_dir_encodes_cwd=False,
        transcript_reader_injected=False,
        recorded_model_source=NO_RECORDED_MODEL,
        hook_source_marker=False,
        locks_turn_callbacks=False,
        account_pool="",
        usage_limit_recovery=None,
        records_classified_usage_limit=False,
        restarts_runner_on_credential_change=False,
        context_window=None,
        compaction_watches_transcript=False,
        quota_reset_jittered=False,
        terminal_resume_argv=("agy", "--dangerously-skip-permissions", "--conversation"),
        terminal_fresh_argv=("agy", "--dangerously-skip-permissions"),
        terminal_loads_plugin=False,
        api_providers=(),
        native_tool_explainer=False,
        janitor_default_model="",
        model_family="gemini",
        fallback_models=(
            ("gemini-3.7-flash-high", "Gemini 3.7 Flash (High)"),
            ("gemini-3.7-flash-medium", "Gemini 3.7 Flash (Medium)"),
            ("gemini-3.7-flash-low", "Gemini 3.7 Flash (Low)"),
            ("gemini-3.6-flash-high", "Gemini 3.6 Flash (High)"),
            ("gemini-3.6-flash-medium", "Gemini 3.6 Flash (Medium)"),
            ("gemini-3.6-flash-low", "Gemini 3.6 Flash (Low)"),
            ("gemini-3.5-flash-medium", "Gemini 3.5 Flash (Medium)"),
            ("gemini-3.5-flash-high", "Gemini 3.5 Flash (High)"),
            ("gemini-3.5-flash-low", "Gemini 3.5 Flash (Low)"),
            ("gemini-3.1-pro-high", "Gemini 3.1 Pro (High)"),
            ("gemini-3.1-pro-low", "Gemini 3.1 Pro (Low)"),
            ("claude-sonnet-4-6", "Claude Sonnet 4.6 (Thinking)"),
            ("claude-opus-4-6-thinking", "Claude Opus 4.6 (Thinking)"),
            ("gpt-oss-120b-medium", "GPT-OSS 120B (Medium)"),
        ),
    ),
    BackendAdapter(
        id=GROK, label="Grok", required_binary="grok",
        efforts=("low", "medium", "high"),
        badge="BackendGrok",
        detail="Runs on Grok Build.",
        # The letterform "x", not `xmark.circle` — that one is the system
        # dismiss/error glyph, so a Grok contact wearing it reads as a broken
        # or failed avatar rather than a brand mark.
        symbol="x.circle",
        brand=BackendBrand("#1a1a1a", "#0a0a0a", "#e8e8e8", "#222222"),
        routing_module="grok_runner",
        runner_module="grok_runner",
        transcript_module="grok_transcript",
        config_model_field="grok_model",
        config_effort_field="grok_effort",
        compact_launch=("grok", "--resume"),
        compact_command="/compact",
        spawn_kwargs=_stream_kwargs,
        goal_module="",
        executable_resolver=_declared_binary,
        model_validator=None,
        resume_target="session_id",
        preassigns_session_id=False,
        resume_transcript_finder=_catalogued_resume_transcript,
        session_catalog_reader=_transcript_session_catalog,
        transcript_dir_encodes_cwd=False,
        transcript_reader_injected=False,
        recorded_model_source=NO_RECORDED_MODEL,
        hook_source_marker=False,
        locks_turn_callbacks=False,
        account_pool="",
        usage_limit_recovery=None,
        records_classified_usage_limit=False,
        restarts_runner_on_credential_change=False,
        context_window=None,
        compaction_watches_transcript=False,
        quota_reset_jittered=False,
        # No interactive terminal launch is defined for this CLI yet.
        terminal_resume_argv=None,
        terminal_fresh_argv=None,
        terminal_loads_plugin=False,
        api_providers=(),
        native_tool_explainer=False,
        janitor_default_model="",
        model_family="grok",
        fallback_models=(
            ("grok-4.6", "Grok 4.6"),
            ("grok-4.5", "Grok 4.5"),
        ),
    ),
    BackendAdapter(
        id=OPENCODE, label="OpenCode", required_binary="opencode",
        efforts=("low", "medium", "high", "max"),
        aliases=("open-code", "opencode-ai"),
        badge="BackendOpenCode",
        detail="Runs on OpenCode.",
        symbol="chevron.left.forwardslash.chevron.right",
        brand=BackendBrand("#16352b", "#0b1c16", "#5ee4b5", "#1f8a65"),
        routing_module="opencode_runner",
        runner_module="opencode_runner",
        transcript_module="opencode_transcript",
        config_model_field="opencode_model",
        config_effort_field="opencode_effort",
        spawn_kwargs=_stream_kwargs,
        goal_module="",
        executable_resolver=_declared_binary,
        model_validator=None,
        resume_target="session_id",
        preassigns_session_id=False,
        resume_transcript_finder=_catalogued_resume_transcript,
        session_catalog_reader=_transcript_session_catalog,
        transcript_dir_encodes_cwd=False,
        transcript_reader_injected=False,
        recorded_model_source=NO_RECORDED_MODEL,
        hook_source_marker=False,
        locks_turn_callbacks=False,
        account_pool="",
        usage_limit_recovery=None,
        records_classified_usage_limit=False,
        restarts_runner_on_credential_change=False,
        context_window=None,
        compaction_watches_transcript=False,
        quota_reset_jittered=False,
        # No interactive terminal launch is defined for this CLI yet.
        terminal_resume_argv=None,
        terminal_fresh_argv=None,
        terminal_loads_plugin=False,
        api_providers=(),
        native_tool_explainer=False,
        janitor_default_model="",
        # OpenCode fronts several families; see Claude.
        model_family="",
        fallback_models=(
            ("opencode/gpt-5.4", "GPT-5.4"),
            ("anthropic/claude-sonnet-4-5", "Claude Sonnet 4.5"),
            ("openai/gpt-5.4", "GPT-5.4 (OpenAI)"),
        ),
    ),
    # DeepSeek is a model family, not a CLI: the card runs through the
    # OpenCode binary but its catalogue is only the DeepSeek models OpenCode
    # exposes (Fireworks, Hugging Face, ...), so the chooser reads
    # "DeepSeek -> model" instead of "OpenCode -> provider -> model".
    BackendAdapter(
        id=DEEPSEEK, label="DeepSeek", required_binary="opencode",
        efforts=("low", "medium", "high", "max"),
        aliases=("deep-seek",),
        badge="BackendDeepSeek",
        detail="Runs DeepSeek models through OpenCode.",
        symbol="water.waves",
        brand=BackendBrand("#4d6bfe", "#2b47d6", "#6f88ff", "#3554e6"),
        routing_module="opencode_runner",
        runner_module="opencode_runner",
        transcript_module="opencode_transcript",
        config_model_field="deepseek_model",
        config_effort_field="deepseek_effort",
        spawn_kwargs=_stream_kwargs,
        goal_module="",
        executable_resolver=_declared_binary,
        model_validator=None,
        resume_target="session_id",
        preassigns_session_id=False,
        resume_transcript_finder=_catalogued_resume_transcript,
        session_catalog_reader=_transcript_session_catalog,
        transcript_dir_encodes_cwd=False,
        transcript_reader_injected=False,
        recorded_model_source=NO_RECORDED_MODEL,
        hook_source_marker=False,
        locks_turn_callbacks=False,
        account_pool="",
        usage_limit_recovery=None,
        records_classified_usage_limit=False,
        restarts_runner_on_credential_change=False,
        context_window=None,
        compaction_watches_transcript=False,
        quota_reset_jittered=False,
        # No interactive terminal launch is defined for this CLI yet.
        terminal_resume_argv=None,
        terminal_fresh_argv=None,
        terminal_loads_plugin=False,
        api_providers=(),
        native_tool_explainer=False,
        janitor_default_model="",
        model_family="deepseek",
        fallback_models=(
            ("fireworks-ai/accounts/fireworks/routers/deepseek-pro-latest", "DeepSeek Pro (latest, Fireworks)"),
            ("fireworks-ai/accounts/fireworks/routers/deepseek-flash-latest", "DeepSeek Flash (latest, Fireworks)"),
            ("fireworks-ai/accounts/fireworks/models/deepseek-v4-pro", "DeepSeek V4 Pro (Fireworks)"),
            ("fireworks-ai/accounts/fireworks/models/deepseek-v4p1-flash", "DeepSeek V4.1 Flash (Fireworks)"),
            ("huggingface/deepseek-ai/DeepSeek-V4-Pro", "DeepSeek V4 Pro (Hugging Face)"),
            ("huggingface/deepseek-ai/DeepSeek-V4.1-Flash", "DeepSeek V4.1 Flash (Hugging Face)"),
        ),
    ),
)

_BY_ID: dict[str, BackendAdapter] = {a.id: a for a in _ADAPTERS}
_ALIASES: dict[str, str] = {
    alias: a.id for a in _ADAPTERS for alias in a.aliases
}

VALID: set[str] = set(_BY_ID)
LABELS: dict[str, str] = {a.id: a.label for a in _ADAPTERS}
EFFORTS: dict[str, tuple[str, ...]] = {a.id: a.efforts for a in _ADAPTERS}
CAPABILITIES: dict[str, BackendCapabilities] = {
    a.id: BackendCapabilities(
        supports_fork=a.supports_fork,
        supports_transcript_streaming=a.supports_transcript_streaming,
        required_binary=a.required_binary,
    )
    for a in _ADAPTERS
}


def adapters() -> tuple[BackendAdapter, ...]:
    return _ADAPTERS


def ids() -> tuple[str, ...]:
    return tuple(a.id for a in _ADAPTERS)


def catalogue_fields(backend: str | None) -> dict[str, Any]:
    """Presentation + capability flags for one ``/agent-model-options`` row.

    An unregistered id gets the neutral defaults so a catalogue row is
    always complete; ``sort_index`` follows registry order.
    """
    adapter = get(backend)
    if adapter is None:
        return BackendAdapter(
            id=str(backend or ""), label=str(backend or ""), required_binary="",
        ).catalogue_fields(len(_ADAPTERS))
    return adapter.catalogue_fields(_ADAPTERS.index(adapter))


def routing_adapters() -> tuple[BackendAdapter, ...]:
    """Adapters that can answer one isolated orchestrator request."""
    return tuple(a for a in _ADAPTERS if a.supports_routing)


def auth_adapters() -> tuple[BackendAdapter, ...]:
    """Adapters whose CLI has a sign-in the Host can drive."""
    return tuple(a for a in _ADAPTERS if a.supports_auth)


def get(backend: str | None) -> BackendAdapter | None:
    b = (backend or "").strip().lower()
    b = _ALIASES.get(b, b)
    return _BY_ID.get(b)


def adapter_for(backend: str | None) -> BackendAdapter:
    """The adapter that runs ``backend``, normalised like ``normalize``."""
    return get(normalize(backend)) or _BY_ID[DEFAULT]


def for_provider(provider: str) -> str:
    """The backend id that executes a routing provider.

    A registered backend id passes through; an API-key provider maps to the
    CLI whose adapter fronts it (``openai`` runs through Codex).
    """
    for a in _ADAPTERS:
        if provider in a.api_providers:
            return a.id
    return provider


def valid_efforts(backend: str) -> tuple[str, ...]:
    adapter = get(normalize(backend))
    return adapter.efforts if adapter else ()


def clean_effort(backend: str, effort: str | None) -> str:
    return by_id(backend).clean_effort(effort)


def is_valid_model(backend: str, model: str | None) -> bool:
    return adapter_for(backend).is_valid_model(model)


def normalize(backend: str | None) -> str:
    """Coerce an arbitrary string to a registered backend, else Claude.

    Unknown / empty values fall back to Claude so a malformed agent row can
    never strand a user with a backend that has no runner. Registered ids
    (including ones a client has never seen) pass through.
    """
    b = (backend or "").strip().lower()
    b = _ALIASES.get(b, b)
    return b if b in _BY_ID else DEFAULT


def is_valid(backend: str | None) -> bool:
    b = (backend or "").strip().lower()
    return _ALIASES.get(b, b) in _BY_ID


def label(backend: str | None) -> str:
    adapter = get(normalize(backend))
    return adapter.label if adapter else LABELS[DEFAULT]


def capabilities(backend: str | None) -> BackendCapabilities:
    adapter = adapter_for(backend)
    return adapter.capabilities()


def spawn_turn(backend: str, **kwargs: Any):
    return by_id(backend).spawn_turn(**kwargs)


def interrupt(backend: str, agent_id: str) -> int:
    if _RUNTIME_CLIENT is not None:
        return int(_RUNTIME_CLIENT.interrupt(normalize(backend), agent_id))
    from .turn_model_fallback import REGISTRY
    return by_id(backend).interrupt(agent_id) + REGISTRY.interrupt(agent_id, event="fallbackInterruptFail")


def interrupt_any(agent_id: str) -> int:
    if _RUNTIME_CLIENT is not None:
        return int(_RUNTIME_CLIENT.interrupt_any(agent_id))
    from .turn_model_fallback import REGISTRY
    total = REGISTRY.interrupt(agent_id, event="fallbackInterruptFail")
    seen: set[str] = set()
    for adapter in _ADAPTERS:
        modules = (adapter.runner_module,) + adapter.extra_interrupt_modules
        for name in modules:
            if name in seen:
                continue
            seen.add(name)
            total += int(_mod(name).interrupt(agent_id) or 0)
    return total


def active_handles(backend: str, agent_id: str) -> list:
    if _RUNTIME_CLIENT is not None:
        try:
            status = runtime_status()
        except Exception:  # noqa: BLE001 - already logged once per window
            # During the runtime's short idle rollover, persisted busy state is
            # safer than claiming the process vanished and double-spawning.
            from . import agents as agents_db
            return ([_RemoteHandle("runtime-status-unknown")]
                    if agents_db.is_busy(agent_id) else [])
        active = status.get("active") or {}
        if agent_id in active:
            return [_RemoteHandle(str(active[agent_id]))]
        if agent_id in set(status.get("spawning") or ()):
            return [_RemoteHandle("spawning")]
        if agent_id in set(status.get("terminals") or ()):
            return [_RemoteHandle("terminal")]
        return []
    from .turn_model_fallback import REGISTRY
    return by_id(backend).active_handles(agent_id) + REGISTRY.active_handles(agent_id)


class GoalUnsupported(RuntimeError):
    """This backend has no goal control Clarp can drive yet."""


def goal(backend: str, agent_id: str, action: str, *, objective: str = "",
         stream=None) -> dict | None:
    """Start, pause, resume, clear or read the agent's goal.

    Returns the goal as ``agent_goals.public`` shapes it, or None when there is
    none. Only Codex has a protocol for this today; the others raise
    GoalUnsupported so the caller can say so instead of pretending.
    """
    if _RUNTIME_CLIENT is not None:
        return _RUNTIME_CLIENT.goal(agent_id, action, objective=objective)
    adapter = adapter_for(backend)
    if not adapter.supports_goal:
        raise GoalUnsupported(
            f"{label(backend)} has no goal control Clarp can drive yet.")
    return _mod(adapter.goal_module).goal(
        agent_id, action, objective=objective, stream=stream)


def steer_turn(backend: str, agent_id: str, text: str, *,
               client_msg_id: str = "", synthesize_audio: bool = False) -> bool:
    if _RUNTIME_CLIENT is not None:
        return bool(_RUNTIME_CLIENT.steer(
            normalize(backend), agent_id, text,
            client_msg_id=client_msg_id,
            synthesize_audio=synthesize_audio,
        ))
    adapter = adapter_for(backend)
    if not adapter.supports_steer:
        return False
    return _mod(adapter.runner_module).steer(
        agent_id, text, client_msg_id=client_msg_id,
        synthesize_audio=synthesize_audio,
    )


def find_session_jsonl(backend: str, session_id: str):
    return by_id(backend).find_transcript(session_id)


def parse_turns(backend: str, path) -> list[dict]:
    return by_id(backend).parse_transcript(path)


def find_resume_transcript(backend: str, session_id: str, *, cwd: str,
                           projects_root: pathlib.Path | None = None):
    b = normalize(backend)
    adapter = adapter_for(b)
    return adapter.resume_transcript_finder(b, session_id, cwd, projects_root)


def list_sessions(backend: str, cwd: str, *, limit: int = 20,
                  all_projects: bool = False) -> list[dict]:
    return by_id(backend).list_sessions(cwd, limit=limit, all_projects=all_projects)


def default_model_effort(backend: str, cfg) -> tuple[str, str]:
    return by_id(backend).default_model_effort(cfg)


ResultCb = Callable[[dict], None]

# The strategy objects. Imported last: the registry builds its singletons
# from the adapter rows above on first use, so neither import order
# (facade first or package first) sees a half-initialised module.
from .backend.base import Backend, CompactionStrategy, Unsupported  # noqa: E402
from .backend.registry import by_id, for_agent  # noqa: E402
from .backend.registry import all as all_backends  # noqa: E402
