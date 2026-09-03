"""AI-CLI backend registry.

Each coding CLI is a ``BackendAdapter``: dispatch, history, catalogue
metadata, presentation, capability flags, and (optional) compaction,
routing and sign-in. Clients do not hardcode provider ids — they render
``/agent-model-options``, including the label, brand colours, symbol and
``supports_*`` flags carried here, so a new CLI looks intentional in the
apps without an app release. Adding a provider is a server adapter plus a
registry line.
"""
from __future__ import annotations

import importlib
import pathlib
from dataclasses import dataclass
from typing import Any, Callable

from .protocol import AgentBackend

CLAUDE = AgentBackend.CLAUDE
CODEX = AgentBackend.CODEX
AGY = AgentBackend.AGY
GROK = AgentBackend.GROK
OPENCODE = AgentBackend.OPENCODE
DEFAULT = CLAUDE


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
    owner_gate: bool = False
    uses_claude_kwargs: bool = False
    uses_codex_app_server: bool = False
    claude_session_catalog: bool = False
    resumable: bool = True

    def __post_init__(self) -> None:
        if self.login_kind not in LOGIN_KINDS:
            raise ValueError(f"{self.id}: unknown login_kind {self.login_kind!r}")
        if self.effort_ui not in EFFORT_UIS:
            raise ValueError(f"{self.id}: unknown effort_ui {self.effort_ui!r}")
        if self.effort_scope not in EFFORT_SCOPES:
            raise ValueError(f"{self.id}: unknown effort_scope {self.effort_scope!r}")

    @property
    def supports_compact(self) -> bool:
        return bool(self.compact_launch and self.compact_command)

    @property
    def supports_routing(self) -> bool:
        return bool(self.routing_module)

    @property
    def supports_auth(self) -> bool:
        return self.login_kind != "none"

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
        binary = self.required_binary
        if self.id == CLAUDE:
            from . import clarp_runner
            binary = clarp_runner.configured_claude_bin()
        return BackendCapabilities(
            supports_fork=self.supports_fork,
            supports_transcript_streaming=self.supports_transcript_streaming,
            required_binary=binary,
        )


def _mod(name: str):
    return importlib.import_module(f"lib.{name}")


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
        uses_claude_kwargs=True,
        claude_session_catalog=True,
        fallback_models=(
            ("fable", "Fable"),
            ("opus", "Opus"),
            ("sonnet", "Sonnet"),
            ("haiku", "Haiku"),
            ("claude-fable-5-1", "Claude Fable 5.1"),
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
        efforts=("low", "medium", "high"),
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
        uses_codex_app_server=True,
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
        owner_gate=True,
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
        symbol="xmark.circle",
        brand=BackendBrand("#1a1a1a", "#0a0a0a", "#e8e8e8", "#222222"),
        routing_module="grok_runner",
        runner_module="grok_runner",
        transcript_module="grok_transcript",
        config_model_field="grok_model",
        config_effort_field="grok_effort",
        compact_launch=("grok", "--resume"),
        compact_command="/compact",
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
        fallback_models=(
            ("opencode/gpt-5.4", "GPT-5.4"),
            ("anthropic/claude-sonnet-4-5", "Claude Sonnet 4.5"),
            ("openai/gpt-5.4", "GPT-5.4 (OpenAI)"),
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


def valid_efforts(backend: str) -> tuple[str, ...]:
    adapter = get(normalize(backend))
    return adapter.efforts if adapter else ()


def clean_effort(backend: str, effort: str | None) -> str:
    e = (effort or "").strip().lower()
    return e if e in valid_efforts(backend) else ""


def is_valid_model(backend: str, model: str | None) -> bool:
    value = (model or "").strip()
    if not value:
        return True
    if normalize(backend) != AGY:
        return True
    from . import provider_capabilities
    return provider_capabilities.is_dispatchable_agy_model(value)


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
    adapter = get(normalize(backend)) or _BY_ID[DEFAULT]
    return adapter.capabilities()


def spawn_turn(backend: str, **kwargs: Any):
    adapter = get(normalize(backend)) or _BY_ID[DEFAULT]
    if adapter.uses_codex_app_server:
        from . import codex_app_server
        return codex_app_server.spawn_turn(**_stream_kwargs(kwargs, owner_gate=False))
    if adapter.uses_claude_kwargs:
        from . import clarp_runner
        return clarp_runner.spawn_turn(**_claude_kwargs(kwargs))
    runner = _mod(adapter.runner_module)
    return runner.spawn_turn(
        **_stream_kwargs(kwargs, owner_gate=adapter.owner_gate))


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


def interrupt(backend: str, agent_id: str) -> int:
    adapter = get(normalize(backend)) or _BY_ID[DEFAULT]
    runner = _mod(adapter.runner_module)
    return int(runner.interrupt(agent_id) or 0)


def interrupt_any(agent_id: str) -> int:
    total = 0
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
    adapter = get(normalize(backend)) or _BY_ID[DEFAULT]
    runner = _mod(adapter.runner_module)
    return list(runner.active_handles(agent_id) or [])


def steer_turn(backend: str, agent_id: str, text: str, *,
               client_msg_id: str = "", synthesize_audio: bool = False) -> bool:
    adapter = get(normalize(backend))
    if adapter is None or not adapter.supports_steer:
        if normalize(backend) != CODEX:
            return False
    from . import codex_app_server
    return codex_app_server.steer(
        agent_id, text, client_msg_id=client_msg_id,
        synthesize_audio=synthesize_audio,
    )


def find_session_jsonl(backend: str, session_id: str):
    adapter = get(normalize(backend)) or _BY_ID[DEFAULT]
    if adapter.claude_session_catalog:
        from .transcript_log import find_latest_jsonl
        return find_latest_jsonl(session_id)
    return _mod(adapter.transcript_module).find_latest_jsonl(session_id)


def parse_turns(backend: str, path) -> list[dict]:
    adapter = get(normalize(backend)) or _BY_ID[DEFAULT]
    if adapter.claude_session_catalog:
        from .transcript_log import parse_turns as _claude_parse
        return _claude_parse(path)
    return _mod(adapter.transcript_module).parse_turns(path)


def find_resume_transcript(backend: str, session_id: str, *, cwd: str,
                           projects_root: pathlib.Path | None = None):
    b = normalize(backend)
    adapter = get(b) or _BY_ID[DEFAULT]
    if adapter.claude_session_catalog:
        from .resume import find_session_jsonl as find_claude_session_jsonl
        root = projects_root or (pathlib.Path.home() / ".claude" / "projects")
        return find_claude_session_jsonl(session_id, cwd, root)
    return find_session_jsonl(b, session_id)


def list_sessions(backend: str, cwd: str, *, limit: int = 20,
                  all_projects: bool = False) -> list[dict]:
    adapter = get(normalize(backend)) or _BY_ID[DEFAULT]
    if adapter.claude_session_catalog:
        from . import session_catalog
        return session_catalog.list_claude_sessions(
            cwd, all_projects=all_projects, limit=limit)
    mod = _mod(adapter.transcript_module)
    list_fn = getattr(mod, "list_sessions")
    try:
        return list_fn(cwd if not all_projects else "", limit=limit,
                       all_projects=all_projects)
    except TypeError:
        return list_fn("" if all_projects else cwd, limit=limit)


def default_model_effort(backend: str, cfg) -> tuple[str, str]:
    adapter = get(normalize(backend)) or _BY_ID[DEFAULT]
    model = ""
    effort = ""
    if adapter.config_model_field:
        model = str(getattr(cfg, adapter.config_model_field, "") or "")
    if adapter.config_effort_field:
        effort = str(getattr(cfg, adapter.config_effort_field, "") or "")
    return model.strip(), clean_effort(adapter.id, effort)


ResultCb = Callable[[dict], None]
