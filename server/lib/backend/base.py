"""``Backend``: the operations the host asks a coding CLI to perform.

Every method here is something the host used to decide per backend with a
flag or an identity branch. A subclass gives each one a real body; where
the contract says most backends have nothing to do, the base carries the
documented no-op so a new CLI only overrides what it actually differs on.

The values that are genuinely data (``context_window``, ``model_family``,
``janitor_default_model``, ``api_providers``, ``login_kind``, ``effort_ui``,
``effort_scope``, ``fallback_models``, ``required_binary``) are attributes,
not getters.

During the migration each instance still carries ``adapter``, today's
``BackendAdapter`` row, and the subclasses delegate to its callables and to
the runner modules. Slice 5 of the contract folds the adapter into this
class; until then module attributes are resolved at call time so tests
that monkeypatch runner globals keep working.
"""
from __future__ import annotations

import importlib
import pathlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..backends import BackendAdapter


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


class Backend:
    """One coding CLI the host can run as an agent backend."""

    def __init__(self, adapter: "BackendAdapter") -> None:
        # Transitional: today's registry row, deleted in slice 5.
        self.adapter = adapter
        # --- data -----------------------------------------------------------
        self.id: str = adapter.id
        self.label: str = adapter.label
        self.required_binary: str = adapter.required_binary
        self.aliases: tuple[str, ...] = adapter.aliases
        self.efforts: tuple[str, ...] = adapter.efforts
        self.context_window: int | None = adapter.context_window
        self.model_family: str = adapter.model_family
        self.janitor_default_model: str = adapter.janitor_default_model
        self.api_providers: tuple[str, ...] = adapter.api_providers
        self.login_kind: str = adapter.login_kind
        self.effort_ui: str = adapter.effort_ui
        self.effort_scope: str = adapter.effort_scope
        self.fallback_models: tuple[tuple[str, str], ...] = adapter.fallback_models

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.id}>"

    # --- the runner -------------------------------------------------------

    def spawn_turn(self, **spec: Any):
        """Start one turn and return its ``TurnHandle``.

        ``spec`` is the host's full spawn vocabulary; the backend keeps the
        keywords its runner accepts and drops the rest.
        """
        raise NotImplementedError(f"{self.id}: spawn_turn")

    def interrupt(self, agent_id: str) -> int:
        """Stop the agent's in-flight turn; the number of processes signalled."""
        raise NotImplementedError(f"{self.id}: interrupt")

    def active_handles(self, agent_id: str) -> list:
        """Handles of the agent's live turn processes (empty when idle)."""
        raise NotImplementedError(f"{self.id}: active_handles")

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
                        home: pathlib.Path | None = None) -> pathlib.Path | None:
        """The transcript file of a session, or ``None`` when none exists."""
        raise NotImplementedError(f"{self.id}: find_transcript")

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
        adapter = self.adapter
        model = ""
        effort = ""
        if adapter.config_model_field:
            model = str(getattr(cfg, adapter.config_model_field, "") or "")
        if adapter.config_effort_field:
            effort = str(getattr(cfg, adapter.config_effort_field, "") or "")
        return model.strip(), self.clean_effort(effort)

    def clean_effort(self, effort: str | None) -> str:
        """``effort`` lower-cased when this CLI offers it, else ""."""
        e = (effort or "").strip().lower()
        return e if e in self.efforts else ""

    def is_valid_model(self, model: str | None) -> bool:
        """Whether a pinned model id can be dispatched through this CLI."""
        return self.adapter.is_valid_model(model)

    def recorded_model(self, session_id: str) -> str:
        """The model the session actually ran, or "" when unrecorded."""
        return resolve("session_models", "recorded_model")(self.id, session_id)

    # --- compaction -------------------------------------------------------

    def compaction(self, session: str) -> CompactionStrategy:
        """How to compact ``session``; raises ``Unsupported`` when the CLI
        cannot be compacted from outside.
        """
        adapter = self.adapter
        if not (adapter.compact_launch and adapter.compact_command):
            raise Unsupported(f"compaction unsupported for {self.id}")
        return CompactionStrategy(
            launch=tuple(adapter.compact_launch),
            command=adapter.compact_command,
            watches_transcript=bool(adapter.compaction_watches_transcript),
        )

    # --- turn plumbing ----------------------------------------------------

    def wrap_turn_callback(self, fn: Callable[..., Any]) -> Callable[..., Any]:
        """Adapt a runner callback for the thread it will fire on.

        Most runners already serialise their callbacks; ``fn`` is returned
        as is.
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
        return self.adapter.executable()
