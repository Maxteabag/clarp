"""``lib.agy_runner``: delegator for the Antigravity (AGY) runner.

The bodies live on ``lib.backend.agy.AgyBackend`` (slice 3 of
docs/architecture/backend-strategy.md). This module keeps the names the
tests reach and monkeypatch: the public runner API, ``AGY_BIN``,
``AGY_PRINT_TIMEOUT``, ``LIVE_TEXT_INTERVAL_SEC``, ``stderr_text`` (patched
to fail the drain) and the private helpers.
"""
from __future__ import annotations

import pathlib
import shutil  # noqa: F401 — tests patch ``agy_runner.shutil.which``
import tempfile  # noqa: F401 — tests patch ``agy_runner.tempfile.mkstemp``
from typing import Any, Callable, Optional

from . import agents as agents_db  # noqa: F401 — tests patch through this name
from .backend.agy import (  # noqa: F401 — re-exported for existing importers
    AgyBackend,
    _CLEAN_STATUSES,
    _CONV_RE,
    _SECRET_VALUE,
    _TOOL_NAMES,
    _TurnState,
    _bind_session,
    _canonical_tool_input,
    _canonical_tool_name,
    _capture_step_usage,
    _conversation_id_from_log,
    _deliver_terminal,
    _event_conversation_id,
    _event_has_stable_replay_identity,
    _finish_error,
    _finish_result,
    _handle_result,
    _normalize_usage,
    _provider_evidence,
    _runner_error,
    _turn_evidence_scope,
)
from .backend.registry import by_id as _by_id
from .proc_util import stderr_text  # noqa: F401 — tests patch ``agy_runner.stderr_text``
from .process_registry import TurnHandle


AGY_BIN = "agy"  # resolved from PATH; tests can monkeypatch.
AGY_PRINT_TIMEOUT = AgyBackend.print_timeout
LIVE_TEXT_INTERVAL_SEC = AgyBackend.live_text_interval


def _backend() -> AgyBackend:
    return _by_id("agy")


_REGISTRY = _backend()._registry


def active_handles(agent_id: str) -> list[TurnHandle]:
    return _backend().active_handles(agent_id)


def interrupt(agent_id: str) -> int:
    """SIGTERM every in-flight agy turn for an agent. Idempotent."""
    return _backend().interrupt(agent_id)


def _unregister(agent_id: str, h: TurnHandle) -> None:
    _backend().unregister_handle(agent_id, h)


def build_cmd(conversation_id: str = "", *,
              is_new_session: bool = False, model: str = "",
              effort: str = "") -> list[str]:
    return _backend().build_cmd(
        conversation_id, is_new_session=is_new_session, model=model, effort=effort)


def spawn_turn(
    *,
    text: str,
    cwd: pathlib.Path,
    backend_session_id: str = "",
    is_new_session: bool = False,
    session: str = "",
    agent_id: str = "",
    on_session_init: Optional[Callable[[str], None]] = None,
    on_result: Optional[Callable[[dict], None]] = None,
    on_error: Optional[Callable[[str], None]] = None,
    trace_id: str = "",
    stream: Any = None,
    enqueue: Optional[Callable[..., int]] = None,
    voice_preamble: bool = False,
    model: str = "",
    effort: str = "",
    run_if_owned: Optional[Callable[[Callable[[], None]], bool]] = None,
    isolated: bool = False,
) -> TurnHandle:
    """Spawn one AGY turn and normalize its NDJSON stream asynchronously."""
    return _backend().start_turn(
        text=text, cwd=cwd, backend_session_id=backend_session_id,
        is_new_session=is_new_session, session=session, agent_id=agent_id,
        on_session_init=on_session_init, on_result=on_result,
        on_error=on_error, trace_id=trace_id, stream=stream, enqueue=enqueue,
        voice_preamble=voice_preamble, model=model, effort=effort,
        run_if_owned=run_if_owned, isolated=isolated)


def _drain_stream(**kwargs: Any) -> None:
    _backend()._drain_stream(**kwargs)


def _handle_event(event: dict[str, Any], revision: int, st: _TurnState,
                  **kwargs: Any) -> None:
    _backend()._handle_event(event, revision, st, **kwargs)


def _handle_step(update: dict[str, Any], evidence: dict[str, Any],
                 st: _TurnState, **kwargs: Any) -> None:
    _backend()._handle_step(update, evidence, st, **kwargs)


def _finalize_success(st: _TurnState, **kwargs: Any) -> None:
    _backend()._finalize_success(st, **kwargs)


def _retract_live_text(st: _TurnState, **kwargs: Any) -> None:
    _backend()._retract_live_text(st, **kwargs)


def _restore_failed_turn(st: _TurnState, **kwargs: Any) -> None:
    _backend()._restore_failed_turn(st, **kwargs)


def _persist_live_text(st: _TurnState, **kwargs: Any) -> None:
    _backend()._persist_live_text(st, **kwargs)


def _speak(text: str, st: _TurnState, **kwargs: Any) -> None:
    _backend()._speak(text, st, **kwargs)


def _transition(agent_id: str, turn_event: str, detail: dict | None = None) -> None:
    _backend()._transition(agent_id, turn_event, detail)


def _broadcast_transcript(stream: Any, agent_id: str, session: str) -> None:
    _backend()._broadcast_transcript(stream, agent_id, session)


# ---- orchestrator routing -------------------------------------------------

def routing_cmd(prompt: str, *, model: str = "", effort: str = "") -> list[str]:
    return _backend().routing_cmd(prompt, model=model, effort=effort)


def routing_text(stdout: str) -> str:
    return _backend().routing_text(stdout)
