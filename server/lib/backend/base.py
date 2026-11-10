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


from ..backend_catalogue import (
    BackendBrand, DEFAULT_BRAND, DEFAULT_SYMBOL, LOGIN_KINDS, EFFORT_UIS,
    EFFORT_SCOPES, DEFAULT_DEFINITION,
)


class CatalogueDefault:
    """A declared default with normal instance/class override precedence."""

    def __init__(self, name: str):
        self.name = name

    def __get__(self, instance, owner):
        return getattr(owner._catalogue_definition, self.name)


class Backend:
    """One coding CLI the host can run as an agent backend."""

    _catalogue_definition = DEFAULT_DEFINITION

    # --- catalogue data: every subclass overrides what differs -------------
    id: str = CatalogueDefault('id')
    label: str = CatalogueDefault('label')
    required_binary: str = CatalogueDefault('required_binary')
    supports_fork: bool = CatalogueDefault('supports_fork')
    supports_steer: bool = CatalogueDefault('supports_steer')
    supports_transcript_streaming: bool = CatalogueDefault('supports_transcript_streaming')
    supports_transcript_tail_apply: bool = False
    supports_routing: bool = CatalogueDefault('supports_routing')
    efforts: tuple[str, ...] = CatalogueDefault('efforts')
    aliases: tuple[str, ...] = CatalogueDefault('aliases')
    badge: str = CatalogueDefault('badge')
    # Presentation the chooser needs; nothing here requires a client build.
    detail: str = CatalogueDefault('detail')
    symbol: str = CatalogueDefault('symbol')
    brand: BackendBrand = CatalogueDefault('brand')
    hidden: bool = CatalogueDefault('hidden')
    # Capability flags advertised on the catalogue. Compact, routing and auth
    # are derived from behaviour rather than declared twice.
    supports_mcp: bool = CatalogueDefault('supports_mcp')
    supports_usage: bool = CatalogueDefault('supports_usage')
    login_kind: str = CatalogueDefault('login_kind')
    effort_ui: str = CatalogueDefault('effort_ui')
    effort_help: str = CatalogueDefault('effort_help')
    effort_scope: str = CatalogueDefault('effort_scope')
    # The runner's short name: prefix of its log events and drain threads,
    # the ``dispatch`` tag on its state rows and the key of its process
    # registry. DeepSeek runs through OpenCode's.
    runner: str = CatalogueDefault('runner')
    config_model_field: str = CatalogueDefault('config_model_field')
    config_effort_field: str = CatalogueDefault('config_effort_field')
    # Config field naming the account switch command of this CLI's pool
    # (``account_pool()``); "" when the CLI has no account switching.
    config_account_switch_field: str = CatalogueDefault('config_account_switch_field')
    fallback_models: tuple[tuple[str, str], ...] = CatalogueDefault('fallback_models')
    resumable: bool = CatalogueDefault('resumable')
    # The runner continues a conversation the CLI no longer has in a new one
    # that opens with Clarp's record of it (agy prunes old conversations), so
    # a missing transcript at boot keeps the binding instead of starting fresh.
    continues_lost_conversations: bool = CatalogueDefault('continues_lost_conversations')
    # Context gauge: the window (tokens) the CLI's transcript fills, or None
    # when the CLI auto-compacts and shows no gauge.
    context_window: int | None = CatalogueDefault('context_window')
    # API-key providers whose model catalogue this CLI fronts ("openai").
    api_providers: tuple[str, ...] = CatalogueDefault('api_providers')
    # The Janitor tool explainer can run natively through this CLI.
    native_tool_explainer: bool = CatalogueDefault('native_tool_explainer')
    # Model a new Janitor gets when none is chosen; empty means CLI default.
    janitor_default_model: str = CatalogueDefault('janitor_default_model')
    # Model family the CLI stands for when the Agent pins no model (avatars).
    model_family: str = CatalogueDefault('model_family')
    # The CLI serves models from many providers, each with its own sign-in
    # (``harness_providers``), and names them ``provider/model``.
    model_providers: bool = CatalogueDefault('model_providers')
    # Text a model id must contain to be offered on this card; "" keeps all.
    # A card that is one model family on a many-provider CLI (DeepSeek).
    model_id_filter: str = CatalogueDefault('model_id_filter')

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

    def prompt_limit_bytes(self) -> int | None:
        """The largest user message this backend's CLI can take, when it has
        no way past one argv string (BACKENDS-08); None means no limit."""
        return None

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

    def external_live_work(self, agent_id: str) -> bool:
        """Whether a turn Clarp did not start (the CLI run from a terminal on
        the agent's conversation) is working right now. Reconciliation must
        not repair such an agent to idle."""
        return False

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

    def routing_env(self) -> dict[str, str]:
        """Extra environment for a ``routing_cmd`` run (none by default)."""
        return {}

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

    def start_pending(self, agent_id: str) -> bool:
        """The agent's turn is registered but its start is not confirmed yet;
        a follow-up waits instead of preempting it (TP-N3). Most never."""
        return False

    def turn_holders(self, agent_id: str) -> list:
        """Processes outside any turn that still hold the agent's native
        session; its next turn waits for them (BACKENDS-07b). Most have none."""
        return []

    def credential_switch_pending(self, message) -> bool:
        """The failure came from a process still on credentials older than the
        ones now selected; the switch waits for it (BACKENDS-06 (c))."""
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

    # --- conversation forks -----------------------------------------------

    def fork_conversation(self, session_id: str, *, source_cwd: str, cwd: str) -> str:
        """Copy conversation ``session_id`` (bound in ``source_cwd``) into a
        new native conversation a child resumes from ``cwd``; returns its id.

        The source keeps running untouched. Raises ``Unsupported`` when the
        CLI has no native fork (the default; ``supports_fork`` says which)
        and ``FileNotFoundError`` when the source conversation is missing.
        """
        raise Unsupported(f"conversation forks unsupported for {self.id}")

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
