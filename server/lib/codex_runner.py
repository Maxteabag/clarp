"""``lib.codex_runner``: delegator for the Codex ``codex exec`` runner.

The bodies live on ``lib.backend.codex.CodexBackend`` (slice 3 of
docs/architecture/backend-strategy.md). This module keeps the names the
app-server client, the tests and the QA host reach: ``spawn_turn`` (the
``codex exec`` path the app-server uses for isolated jobs), ``interrupt``
and ``active_handles`` over that path's process registry, ``build_cmd``,
the routing pair, ``CODEX_BIN`` (monkeypatched to a fake binary) and the
voice-preamble re-exports.
"""
from __future__ import annotations

import pathlib
import shutil  # noqa: F401 — tests patch ``codex_runner.shutil.which``
from typing import Any, Callable, Optional

from .backend.codex import (  # noqa: F401 — re-exported for existing importers
    CodexBackend,
    _TOOL_ITEM_TYPES,
    _TurnState,
    _agent_text,
    _event_parts,
    _session_id_from,
)
from .backend.registry import by_id as _by_id
from .backend.stream_json import SPEAK_RE
from .process_registry import TurnHandle
from .voice_markup import (  # noqa: F401 — spoken_for_tts remains re-exported
    spoken_chunks_for_tts,
    spoken_for_tts,
)
from .voice_preamble import (  # noqa: F401 — re-exported for existing importers
    _NATURAL_SPEECH,
    _NO_INTERACTIVE_QUESTIONS,
    _VOICE_INSTRUCTION,
    _VOICE_PREAMBLE_HEAD,
    _VOICE_PREAMBLE_SPLIT,
    _narration_clause,
    _preamble,
    app_turn_instructions,
    apply_voice_preamble,
    persona_identity_instruction,
    strip_voice_preamble,
)


CODEX_BIN = "codex"  # resolved from PATH; tests can monkeypatch.
LIVE_TEXT_INTERVAL_SEC = CodexBackend.live_text_interval

# Same voice-gating convention as the Claude path: only text the agent wraps
# in <speak>…</speak> is spoken. The regex and the app-turn preamble live in
# shared modules; both stay importable from here (agy_runner,
# codex_app_server, the transcript parsers and tests reach them this way).
_SPEAK_RE = SPEAK_RE


def _backend() -> CodexBackend:
    return _by_id("codex")


# Live registry of ``codex exec`` turns: agent_id → running TurnHandles, so
# /stop can interrupt in-flight turns.
_REGISTRY = _backend()._registry


def active_handles(agent_id: str) -> list[TurnHandle]:
    return _backend().active_exec_handles(agent_id)


def interrupt(agent_id: str) -> int:
    """SIGTERM every in-flight codex turn for an agent. Idempotent."""
    return _backend().interrupt_exec(agent_id)


def _unregister(agent_id: str, h: TurnHandle) -> None:
    _backend().unregister_handle(agent_id, h)


def build_cmd(session_id: str = "", *, is_new_session: bool = False,
              model: str = "", reasoning_effort: str = "",
              isolated: bool = False) -> list[str]:
    return _backend().build_cmd(
        session_id, is_new_session=is_new_session, model=model,
        reasoning_effort=reasoning_effort, isolated=isolated)


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
) -> TurnHandle:
    """Spawn `codex exec` for ONE turn (``CodexBackend.start_turn``)."""
    return _backend().start_turn(
        text=text, cwd=cwd, backend_session_id=backend_session_id,
        is_new_session=is_new_session, session=session, agent_id=agent_id,
        on_session_init=on_session_init, on_result=on_result,
        on_error=on_error, trace_id=trace_id, stream=stream, enqueue=enqueue,
        voice_preamble=voice_preamble, model=model, effort=effort,
        isolated=isolated)


def _drain_stdout(**kwargs: Any) -> None:
    _backend()._drain_stdout(**kwargs)


def _handle_event(ev: dict, st: _TurnState, **kwargs: Any) -> None:
    _backend()._handle_event(ev, st, **kwargs)


def _handle_item(etype: str, item: dict, st: _TurnState, **kwargs: Any) -> None:
    _backend()._handle_item(etype, item, st, **kwargs)


def _persist_live_text(st: _TurnState, **kwargs: Any) -> None:
    _backend()._persist_live_text(st, **kwargs)


def _speak(text: str, st: _TurnState, **kwargs: Any) -> None:
    _backend()._speak(text, st, **kwargs)


def _record_state(agent_id: str, kind: str, detail: dict | None = None) -> None:
    _backend()._record_state(agent_id, kind, detail)


def _broadcast_transcript(stream: Any, agent_id: str, session: str) -> None:
    _backend()._broadcast_transcript(stream, agent_id, session)


# ---- orchestrator routing -------------------------------------------------

def routing_cmd(prompt: str, *, model: str = "", effort: str = "") -> list[str]:
    return _backend().routing_cmd(prompt, model=model, effort=effort)


def routing_text(stdout: str) -> str:
    return _backend().routing_text(stdout)
