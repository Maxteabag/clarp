"""``Backend``: the operations the host asks a coding CLI to perform.

Every method here is something the host used to decide per backend with a
flag or an identity branch. A subclass gives each one a real body; where
the contract says most backends have nothing to do, the base carries the
documented no-op so a new CLI only overrides what it actually differs on.

The values that are genuinely data (``context_window``, ``model_family``,
``janitor_default_model``, ``api_providers``, ``login_kind``, ``effort_ui``,
``effort_scope``, ``fallback_models``, ``required_binary``) are attributes,
not getters.

Each subclass declares its catalogue data as class attributes and holds
its runner body; there is no module-level runner API to patch. Tests
replace attributes on the backend instance (``by_id(...)``) instead.
"""
from __future__ import annotations

import importlib
import pathlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

from ..log import log_exception
from ..process_registry import ProcessRegistry


# One live-turn registry per runner, shared by every backend that runs
# through it (DeepSeek on OpenCode), so ``interrupt_any`` and the runner
# module's ``_REGISTRY`` see the same handles a dispatch registered.
_REGISTRIES: dict[str, ProcessRegistry] = {}


class Unsupported(Exception):
    """This backend has no implementation of the requested operation.

    Raised by ``terminal_argv`` when the CLI has no interactive mode and by
    ``compaction`` when the CLI cannot be compacted from outside.
    """


@dataclass(frozen=True)
class CompactionStrategy:
    """How the host drives a CLI to compact one session.

    ``launch`` is the interactive argv prefix (the backend session id is
    appended), ``command`` the slash command typed into it, and
    ``watches_transcript`` says whether the host can watch the transcript
    settle instead of waiting a fixed window.
    """
    launch: tuple[str, ...]
    command: str
    watches_transcript: bool = False


def _mod(name: str):
    return importlib.import_module(f"lib.{name}")


def resolve(module: str, attr: str) -> Any:
    """``lib.<module>.<attr>`` looked up now, not at import.

    The runner modules are heavier than the registry and tests monkeypatch
    their globals, so a backend binds late on every call.
    """
    return getattr(_mod(module), attr)


@dataclass(frozen=True)
class BackendBrand:
    """Chooser colours as ``#rrggbb`` strings.

    Served on the catalogue so a client never has to ship a palette per CLI:
    a new backend picks its own field and tint and every app renders it.
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


# Neutral treatment for a backend that declares no brand of its own.
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


class Backend:
    """One coding CLI the host can run as an agent backend."""

    # --- catalogue data: every subclass overrides what differs -------------
    id: str = ""
    label: str = ""
    required_binary: str = ""
    supports_fork: bool = False
    supports_steer: bool = False
    supports_transcript_streaming: bool = False
    supports_routing: bool = True
    efforts: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    badge: str = ""
    # Presentation the chooser needs; nothing here requires a client build.
    detail: str = ""
    symbol: str = DEFAULT_SYMBOL
    brand: BackendBrand = DEFAULT_BRAND
    hidden: bool = False
    # Capability flags advertised on the catalogue. Compact, routing and auth
    # are derived from behaviour rather than declared twice.
    supports_mcp: bool = False
    supports_usage: bool = False
    login_kind: str = "none"
    effort_ui: str = "picker"
    effort_help: str = ""
    effort_scope: str = "provider"
    # The runner's short name: prefix of its log events and drain threads,
    # the ``dispatch`` tag on its state rows and the key of its process
    # registry. DeepSeek runs through OpenCode's.
    runner: str = ""
    config_model_field: str = ""
    config_effort_field: str = ""
    # Config field naming the account switch command of this CLI's pool
    # (``account_pool()``); "" when the CLI has no account switching.
    config_account_switch_field: str = ""
    fallback_models: tuple[tuple[str, str], ...] = ()
    resumable: bool = True
    # Context gauge: the window (tokens) the CLI's transcript fills, or None
    # when the CLI auto-compacts and shows no gauge.
    context_window: int | None = None
    # API-key providers whose model catalogue this CLI fronts ("openai").
    api_providers: tuple[str, ...] = ()
    # The Janitor tool explainer can run natively through this CLI.
    native_tool_explainer: bool = False
    # Model a new Janitor gets when none is chosen; empty means CLI default.
    janitor_default_model: str = ""
    # Model family the CLI stands for when the Agent pins no model (avatars).
    model_family: str = ""

    def __init__(self) -> None:
        if self.login_kind not in LOGIN_KINDS:
            raise ValueError(f"{self.id}: unknown login_kind {self.login_kind!r}")
        if self.effort_ui not in EFFORT_UIS:
            raise ValueError(f"{self.id}: unknown effort_ui {self.effort_ui!r}")
        if self.effort_scope not in EFFORT_SCOPES:
            raise ValueError(f"{self.id}: unknown effort_scope {self.effort_scope!r}")
        # Live turn processes, agent_id -> handles; one per runner.
        self._registry: ProcessRegistry | None = None
        if self.runner:
            if self.runner not in _REGISTRIES:
                _REGISTRIES[self.runner] = ProcessRegistry(log_exception=log_exception)
            self._registry = _REGISTRIES[self.runner]

    @property
    def supports_auth(self) -> bool:
        return self.login_kind != "none"

    @property
    def effort_compatibility_unknown(self) -> bool:
        """Effort is a provider flag whose fit with a pinned model is unknown."""
        return self.effort_scope == "provider_flag"

    @property
    def model_carries_effort(self) -> bool:
        return self.effort_ui == "folded_into_model"

    @property
    def supports_compact(self) -> bool:
        """Whether the host can drive a compaction (``compaction`` has one)."""
        try:
            self.compaction("")
        except Unsupported:
            return False
        return True

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

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.id}>"

    # --- the runner -------------------------------------------------------

    def spawn_turn(self, **spec: Any):
        """Start one turn and return its ``TurnHandle``.

        ``spec`` is the host's full spawn vocabulary; a subclass keeps the
        keywords its runner accepts and drops the rest before ``start_turn``.
        """
        return self.start_turn(**spec)

    def start_turn(self, **kwargs: Any):
        """The runner body: spawn the CLI for one turn with explicit keywords."""
        raise NotImplementedError(f"{self.id}: start_turn")

    def interrupt(self, agent_id: str) -> int:
        """Stop the agent's in-flight turn; the number of processes signalled.

        SIGTERM every live turn of the agent. Idempotent: finished handles
        are skipped.
        """
        if self._registry is None:
            return 0
        return self._registry.interrupt(agent_id, event=f"{self.runner}InterruptFail")

    def interrupt_all(self, agent_id: str) -> int:
        """``interrupt`` plus any other process path the runner owns.

        Used when the host stops an agent without knowing which backend ran
        it; a runner with a second process path (Codex's isolated ``exec``
        turns beside the app-server) stops both.
        """
        return self.interrupt(agent_id)

    def active_handles(self, agent_id: str) -> list:
        """Handles of the agent's live turn processes (empty when idle)."""
        if self._registry is None:
            return []
        return self._registry.active_handles(agent_id)

    def register_handle(self, agent_id: str, handle) -> None:
        """Register a live turn unless the run is isolated (empty agent id)."""
        if agent_id and self._registry is not None:
            self._registry.register(agent_id, handle)

    def unregister_handle(self, agent_id: str, handle) -> None:
        if self._registry is not None:
            self._registry.unregister(agent_id, handle)

    def routing_cmd(self, prompt: str, *, model: str = "", effort: str = "") -> list[str]:
        """argv for one isolated, tool-less orchestrator request."""
        raise NotImplementedError(f"{self.id}: routing_cmd")

    def routing_text(self, stdout: str) -> str:
        """The answer text from a ``routing_cmd`` run's stdout."""
        raise NotImplementedError(f"{self.id}: routing_text")

    # --- sessions and transcripts -----------------------------------------

    def resume_target(self, session_id: str, cwd: str,
                      home: pathlib.Path | None = None):
        """What ``--resume`` points at for a bound session.

        The transcript path for a CLI that resumes by file, the session id
        for one that resolves it itself, and ``None`` when the bound session
        has nothing to resume (a ghost session or no binding at all).
        """
        raise NotImplementedError(f"{self.id}: resume_target")

    def bind_new_session(self, agent_id: str, session: str, *,
                         uuid_factory: Callable[[], str] | None = None) -> str:
        """Pre-mint and bind the backend session id before the first spawn.

        Only a CLI that accepts ``--session-id`` does this (minting with
        ``uuid_factory`` when given); the rest return "" and let the runner
        bind the id the CLI reports.
        """
        return ""

    def find_transcript(self, session_id: str,
                        home: pathlib.Path | None = None, *,
                        cwd: str = "") -> pathlib.Path | None:
        """The transcript file of a session, or ``None`` when none exists.

        ``cwd`` is the directory the session was bound with; a CLI whose
        transcript layout encodes it prefers that directory, the rest ignore
        the hint.
        """
        raise NotImplementedError(f"{self.id}: find_transcript")

    def transcript_cwd(self, transcript: Any) -> str:
        """The working directory a transcript's location encodes, or "" when
        the CLI's layout says nothing about where the session ran.
        """
        return ""

    def parse_transcript(self, path) -> list[dict]:
        """The conversation turns recorded in one transcript file."""
        raise NotImplementedError(f"{self.id}: parse_transcript")

    def list_sessions(self, cwd: str, *, limit: int = 20,
                      all_projects: bool = False) -> list[dict]:
        """Past sessions for the adopt picker, newest first."""
        raise NotImplementedError(f"{self.id}: list_sessions")

    # --- interactive terminal ---------------------------------------------

    def terminal_argv(self, session_id: str) -> list[str]:
        """argv for an interactive terminal on the session (fresh when "").

        Raises ``Unsupported`` when the CLI has no interactive mode the host
        can open.
        """
        raise Unsupported(f"no interactive terminal for the {self.id} backend")

    # --- credentials and quota --------------------------------------------

    def on_credential_change(self) -> None:
        """React to a sign-in or sign-out that rewrote the CLI's credentials.

        Most CLIs read credentials per process and have nothing to do.
        """
        return None

    def recover_usage_limit(self, message: str) -> bool:
        """Try to recover a usage-limit failure in-process; True when the
        turn may be retried without switching accounts. Most CLIs cannot.
        """
        return False

    def account_pool(self) -> str:
        """Name of the account pool the failover coordinator manages for this
        CLI, or "" when it has no account switching.
        """
        return ""

    def classify_usage_limit(self, message: str, *,
                             quota_confirmed: bool | None = None) -> dict | None:
        """Record a failure already classified as a usage limit as a provider
        limit event and return it. CLIs whose quota windows the host does not
        track return ``None``.
        """
        return None

    def quota_identity(self, window: dict) -> Any:
        """The identity a quota-window notification is de-duplicated by.

        The window id as reported; a CLI that jitters its reset timestamps
        overrides this to round them.
        """
        return window["window_id"]

    # --- model policy -----------------------------------------------------

    def default_model_effort(self, cfg) -> tuple[str, str]:
        """The Host-configured default model and effort for this CLI."""
        model = ""
        effort = ""
        if self.config_model_field:
            model = str(getattr(cfg, self.config_model_field, "") or "")
        if self.config_effort_field:
            effort = str(getattr(cfg, self.config_effort_field, "") or "")
        return model.strip(), self.clean_effort(effort)

    def clean_effort(self, effort: str | None) -> str:
        """``effort`` lower-cased when this CLI offers it, else ""."""
        e = (effort or "").strip().lower()
        return e if e in self.efforts else ""

    def is_valid_model(self, model: str | None) -> bool:
        """Whether a pinned model id can be dispatched through this CLI.

        Most CLIs accept any id and fail the turn themselves; a CLI with a
        catalogue the host can check overrides this. "" is always valid
        (it means the CLI default).
        """
        return True

    def recorded_model(self, session_id: str) -> str:
        """The model the session actually ran, or "" when the CLI records
        none anywhere the Host can read.
        """
        return ""

    def model_transcript(self, session_id: str) -> pathlib.Path | None:
        """The transcript ``recorded_model`` reads the model from: the
        session's transcript unless the CLI keeps a better index of it.
        """
        return self.find_transcript(session_id)

    def cli_default_model(self) -> str:
        """The model the CLI launches with when the Host pins none, read
        from the CLI's own configuration; "" when it offers no such source.
        """
        return ""

    # --- compaction -------------------------------------------------------

    def compaction(self, session: str) -> CompactionStrategy:
        """How to compact ``session``; raises ``Unsupported`` when the CLI
        cannot be compacted from outside (the default).
        """
        raise Unsupported(f"compaction unsupported for {self.id}")

    # --- turn plumbing ----------------------------------------------------

    def wrap_turn_callback(self, fn: Callable[..., Any],
                           lock: Any) -> Callable[..., Any]:
        """Adapt a runner callback for the thread it will fire on.

        ``lock`` is the dispatcher's turn lock. Most runners already
        serialise their callbacks; ``fn`` is returned as is.
        """
        return fn

    def arm_source_marker(self, session: str, trace_id: str,
                          synthesize_audio: bool, *,
                          home: pathlib.Path | None = None,
                          now: Callable[[], float] | None = None) -> None:
        """Arm whatever the runner needs to attribute the next turn's source.

        Only a CLI that reports through the hook plugin writes anything.
        """
        return None

    # --- executable -------------------------------------------------------

    def executable(self) -> str:
        """The binary to launch, after any Host-level override."""
        return self.required_binary

    # --- goal and steer ---------------------------------------------------

    def goal(self, agent_id: str, action: str, *, objective: str = "",
             stream: Any = None) -> dict | None:
        """Start, pause, resume, clear or read the agent's goal; the goal as
        ``agent_goals.public`` shapes it, or ``None`` when there is none.
        Raises ``Unsupported`` when the CLI has no goal protocol.
        """
        raise Unsupported(f"{self.id} has no goal control Clarp can drive yet.")

    def steer(self, agent_id: str, text: str, *, client_msg_id: str = "",
              synthesize_audio: bool = False) -> bool:
        """Inject ``text`` into the agent's in-flight turn; True when the
        runner accepted it. Raises ``Unsupported`` when the CLI cannot be
        steered mid-turn.
        """
        raise Unsupported(f"{self.id} cannot be steered mid-turn")
