"""``lib.grok_runner``: delegator for the Grok Build runner.

The bodies live on ``lib.backend.grok.GrokBackend`` (slice 3 of
docs/architecture/backend-strategy.md). This module keeps the names the
tests reach and monkeypatch: the public runner API, ``GROK_BIN``,
``LIVE_TEXT_INTERVAL_SEC`` and the private helpers.
"""
from __future__ import annotations

import pathlib
import shutil  # noqa: F401 — tests patch ``grok_runner.shutil.which``
from typing import Any, Callable, Optional

from .backend.grok import (  # noqa: F401 — re-exported for existing importers
    GrokBackend,
    _TOOL_DONE_STATUSES,
    _TOOL_START_TYPES,
    _TOOL_UPDATE_TYPES,
    _TurnState,
    _assistant_delta,
    _session_id_from,
    _text_from,
    _tool_name_from,
    _usage_from,
)
from .backend.registry import by_id as _by_id
from .backend.stream_json import SPEAK_RE
from .process_registry import TurnHandle


GROK_BIN = "grok"
LIVE_TEXT_INTERVAL_SEC = GrokBackend.live_text_interval
_SPEAK_RE = SPEAK_RE


def _backend() -> GrokBackend:
    return _by_id("grok")


_REGISTRY = _backend()._registry


def active_handles(agent_id: str) -> list[TurnHandle]:
    return _backend().active_handles(agent_id)


def interrupt(agent_id: str) -> int:
    return _backend().interrupt(agent_id)


def build_cmd(session_id: str = "", *, is_new_session: bool = False,
              model: str = "", effort: str = "") -> list[str]:
    return _backend().build_cmd(
        session_id, is_new_session=is_new_session, model=model, effort=effort)


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
    isolated: bool = False,
    **_kwargs: Any,
) -> TurnHandle:
    return _backend().start_turn(
        text=text, cwd=cwd, backend_session_id=backend_session_id,
        is_new_session=is_new_session, session=session, agent_id=agent_id,
        on_session_init=on_session_init, on_result=on_result,
        on_error=on_error, trace_id=trace_id, stream=stream, enqueue=enqueue,
        voice_preamble=voice_preamble, model=model, effort=effort,
        isolated=isolated, **_kwargs)


def _drain_stdout(**kwargs: Any) -> None:
    _backend()._drain_stdout(**kwargs)


def _handle_event(ev: dict, st: _TurnState, **kwargs: Any) -> None:
    _backend()._handle_event(ev, st, **kwargs)


def _persist_live_text(st: _TurnState, **kwargs: Any) -> None:
    _backend()._persist_live_text(st, **kwargs)


def _bind(st: _TurnState, session_id: str, **kwargs: Any) -> None:
    _backend()._bind(st, session_id, **kwargs)


def _speak(text: str, st: _TurnState, **kwargs: Any) -> None:
    _backend()._speak(text, st, **kwargs)


def _record_state(agent_id: str, kind: str, detail: dict[str, Any]) -> None:
    _backend()._record_state(agent_id, kind, detail)


def _broadcast(stream: Any, agent_id: str, session: str) -> None:
    _backend()._broadcast(stream, agent_id, session)


# ---- orchestrator routing -------------------------------------------------

def routing_cmd(prompt: str, *, model: str = "", effort: str = "") -> list[str]:
    return _backend().routing_cmd(prompt, model=model, effort=effort)


def routing_text(stdout: str) -> str:
    return _backend().routing_text(stdout)
