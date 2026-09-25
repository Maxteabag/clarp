"""``lib.clarp_runner``: delegator for the Claude runner.

The bodies live on ``lib.backend.claude.ClaudeBackend`` (slice 3 of
docs/architecture/backend-strategy.md). This module keeps the names the
tests and ``provider_capabilities`` reach and monkeypatch: the public
runner API, ``configured_claude_bin`` (which stays here because the
package guard keeps CLI names out of the classes) and the private helpers.
"""
from __future__ import annotations

import pathlib
import shutil  # noqa: F401 — tests patch ``clarp_runner.shutil.which``
from typing import Any, Callable, Optional

from . import agents as agents_db  # noqa: F401 — tests patch through this name
from . import config as _config
from .backend.claude import (  # noqa: F401 — re-exported for existing importers
    ClaudeBackend,
    _agent_mcp_servers,
    _assistant_event_text,
    _content_text,
    _is_assistant_event,
    _scoped_mcp_config_path,
    _store_live_partial,
)
from .backend.registry import by_id as _by_id
from .log import log
from .process_registry import TurnHandle


DEFAULT_CLAUDE_BIN = "claude"


def configured_claude_bin(cfg: _config.Config | None = None) -> str:
    """Return the configured Claude backend executable.

    Supported values:
      * "claude" / "claude-code" / "official" → official Claude Code CLI
      * "clarp" / "clarp-cli"                 → clarp wrapper

    Invalid values fail open to the official CLI and are logged so a typo does
    not strand voice turns on a non-existent runner.
    """
    cfg = cfg or _config.load()
    raw = (getattr(cfg, "claude_cli", "") or DEFAULT_CLAUDE_BIN).strip().lower()
    if raw in {"claude", "claude-code", "claude_code", "official"}:
        return "claude"
    if raw in {"clarp", "clarp-cli", "clarp_cli"}:
        return "clarp"
    log("claudeCliProviderInvalid",
        f"value={raw!r} fallback={DEFAULT_CLAUDE_BIN}")
    return DEFAULT_CLAUDE_BIN


def _backend() -> ClaudeBackend:
    return _by_id("claude")


# Live registry: agent_id → list of currently-running TurnHandle objects.
# /stop reads this to interrupt a turn; the drainer thread evicts handles
# when their subprocess exits.
_REGISTRY = _backend()._registry


def active_handles(agent_id: str) -> list[TurnHandle]:
    """Snapshot of currently-running clarp turns for one agent."""
    return _backend().active_handles(agent_id)


def interrupt(agent_id: str) -> int:
    """SIGTERM every in-flight clarp turn for an agent. Returns the count
    of processes signalled. Idempotent — already-finished handles are
    silently skipped."""
    return _backend().interrupt(agent_id)


def _unregister(agent_id: str, h: TurnHandle) -> None:
    _backend().unregister_handle(agent_id, h)


def build_cmd(backend_session_id: str = "", *,
              is_new_session: bool = False, model: str = "",
              effort: str = "", persona: str = "", session: str = "") -> list[str]:
    return _backend().build_cmd(
        backend_session_id, is_new_session=is_new_session, model=model,
        effort=effort, persona=persona, session=session)


def spawn_turn(
    *,
    text: str,
    cwd: pathlib.Path,
    backend_session_id: str = "",
    is_new_session: bool = False,
    session: str = "",
    agent_id: str = "",
    on_session_init: Optional[Callable[[str], None]] = None,
    on_result:       Optional[Callable[[dict], None]] = None,
    on_error:        Optional[Callable[[str], None]] = None,
    trace_id: str = "",
    model: str = "",
    effort: str = "",
    stream=None,
    isolated: bool = False,
    hook_session: str | None = None,
) -> TurnHandle:
    """Spawn the configured Claude CLI for ONE turn (``ClaudeBackend.start_turn``)."""
    return _backend().start_turn(
        text=text, cwd=cwd, backend_session_id=backend_session_id,
        is_new_session=is_new_session, session=session, agent_id=agent_id,
        on_session_init=on_session_init, on_result=on_result,
        on_error=on_error, trace_id=trace_id, model=model, effort=effort,
        stream=stream, isolated=isolated, hook_session=hook_session)


def _drain_stdout(*args: Any, **kwargs: Any) -> None:
    _backend()._drain_stdout(*args, **kwargs)


# ---- orchestrator routing -------------------------------------------------

def routing_cmd(prompt: str, *, model: str = "", effort: str = "") -> list[str]:
    return _backend().routing_cmd(prompt, model=model, effort=effort)


def routing_text(stdout: str) -> str:
    return _backend().routing_text(stdout)
