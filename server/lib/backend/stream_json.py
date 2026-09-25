"""``StreamJsonBackend``: what the JSON-lines CLIs share.

Codex, Antigravity, Grok and OpenCode each drive one subprocess per turn and
reproduce the same PWA side-effects off its stdout stream: agent-state
rows, transcript-updated SSEs, ``<speak>`` extraction into the TTS queue, a
bounded-cadence live assistant row and a session binding callback. The
bodies live here once; each subclass keeps its thin private wrappers
(``_record_state``, ``_speak``, ...) that pin the per-CLI event names.

The subprocess contract itself (``popen_turn``, ``make_handle``, ``launch``,
``start_drain``) is module-level so the Claude runner, which is not a
stream-json backend, shares it too.

This module must not import any ``*_runner`` module or ``backends``.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import threading
import time
from typing import Any, Callable, Iterator, Optional

from .. import agents as agents_db
from ..log import log, log_exception
from ..proc_util import attach_stderr_drain
from ..process_registry import TurnHandle
from ..protocol import SSEType, TurnSource
from ..voice_markup import spoken_chunks_for_tts, spoken_for_tts
from .base import Backend, resolve

# Same voice-gating convention as the Claude path: only text the agent
# wraps in <speak>…</speak> is spoken; everything else is silent.
SPEAK_RE = re.compile(r"<speak>(.*?)</speak>", re.DOTALL | re.IGNORECASE)


# ---- subprocess spawn ------------------------------------------------------

def popen_turn(cmd: list[str], *, cwd: Any, session: str,
               stdin: Any = subprocess.DEVNULL,
               env_extra: Optional[dict[str, str]] = None) -> subprocess.Popen:
    """Start one turn subprocess with the runners' common Popen contract.

    Text pipes, line buffering, its own process group on POSIX (so a stop
    reaches descendants) and ``CLAUDE_PWA_SESSION`` so hooks and skills inside
    the CLI can identify the owning agent.
    """
    env = {**os.environ, "CLAUDE_PWA_SESSION": session}
    if env_extra:
        env.update(env_extra)
    return subprocess.Popen(
        cmd,
        cwd=str(cwd),
        stdin=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1, start_new_session=(os.name == "posix"),
        env=env,
    )


def make_handle(proc: subprocess.Popen) -> TurnHandle:
    """Attach the stderr drain and wrap ``proc`` in a group-owning TurnHandle."""
    attach_stderr_drain(proc)
    return TurnHandle(
        proc=proc, drain_thread=None,
        process_group=proc.pid if os.name == "posix" else None)  # type: ignore[arg-type]


def launch(cmd: list[str], *, cwd: Any, session: str,
           stdin: Any = subprocess.DEVNULL,
           env_extra: Optional[dict[str, str]] = None,
           ) -> tuple[subprocess.Popen, TurnHandle]:
    """``popen_turn`` + ``make_handle`` in one step."""
    proc = popen_turn(cmd, cwd=cwd, session=session, stdin=stdin,
                      env_extra=env_extra)
    return proc, make_handle(proc)


def start_drain(handle: TurnHandle, target: Callable[..., None], /, *,
                backend: str, **kwargs: Any) -> threading.Thread:
    """Start the daemon drain thread for ``handle`` and record it on the handle.

    ``kwargs`` are passed through to ``target`` verbatim (they usually include
    their own ``handle=``, hence the positional-only parameters here).
    """
    drain = threading.Thread(
        target=target, kwargs=kwargs,
        daemon=True, name=f"{backend}-drain-{handle.proc.pid}",
    )
    handle.drain_thread = drain
    drain.start()
    return drain


# ---- stream parsing --------------------------------------------------------

def iter_json_dicts(stdout: str) -> Iterator[dict]:
    """Yield each JSON-object line of ``stdout``; skip blanks and non-JSON."""
    for raw in (stdout or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(ev, dict):
            yield ev


class StreamJsonBackend(Backend):
    """A CLI that streams JSON lines and resumes by session id."""

    #: Minimum seconds between two writes of the live assistant row.
    live_text_interval: float = 0.25
    #: ``speak`` prefers the agent's stored trace over the turn's own.
    speak_agent_trace: bool = True

    # --- sessions and transcripts -----------------------------------------

    def resume_target(self, session_id: str, cwd: str,
                      home: pathlib.Path | None = None):
        """The session id itself: the CLI resolves its own transcript."""
        return session_id or None

    def find_transcript(self, session_id: str,
                        home: pathlib.Path | None = None) -> pathlib.Path | None:
        # The transcript modules locate their own roots; ``home`` is only a
        # Claude concern until slice 4 threads it through.
        return resolve(self.adapter.transcript_module, "find_latest_jsonl")(session_id)

    def parse_transcript(self, path) -> list[dict]:
        return resolve(self.adapter.transcript_module, "parse_turns")(path)

    def list_sessions(self, cwd: str, *, limit: int = 20,
                      all_projects: bool = False) -> list[dict]:
        adapter = self.adapter
        return adapter.session_catalog_reader(
            adapter, cwd, limit=limit, all_projects=all_projects)

    # --- subprocess contract ----------------------------------------------

    def popen_turn(self, cmd: list[str], *, cwd: Any, session: str,
                   stdin: Any = subprocess.DEVNULL,
                   env_extra: Optional[dict[str, str]] = None) -> subprocess.Popen:
        """Start one turn subprocess (see the module-level ``popen_turn``)."""
        return popen_turn(cmd, cwd=cwd, session=session, stdin=stdin,
                          env_extra=env_extra)

    def make_handle(self, proc: subprocess.Popen) -> TurnHandle:
        """Attach the stderr drain and wrap ``proc`` in a group-owning TurnHandle."""
        return make_handle(proc)

    def launch(self, cmd: list[str], *, cwd: Any, session: str,
               stdin: Any = subprocess.DEVNULL,
               env_extra: Optional[dict[str, str]] = None,
               ) -> tuple[subprocess.Popen, TurnHandle]:
        """Start one turn subprocess; ``(Popen, TurnHandle)``."""
        return launch(cmd, cwd=cwd, session=session, stdin=stdin,
                      env_extra=env_extra)

    def start_drain(self, handle: TurnHandle, target: Callable[..., None], /,
                    **kwargs: Any) -> threading.Thread:
        """Start the daemon thread that drains the turn's stdout."""
        return start_drain(handle, target, backend=self.runner, **kwargs)

    # --- side-effects -----------------------------------------------------

    def record_state(self, agent_id: str, kind: str, detail: dict | None, *,
                     event: str = "") -> None:
        """Record an agent-state row; failures are logged, never raised."""
        if not agent_id:
            return
        try:
            agents_db.record_state(agent_id, kind, detail)
        except Exception as error:  # noqa: BLE001
            log_exception(event or f"{self.runner}RecordStateFail", error,
                          detail=f"{agent_id}:{kind}")

    def broadcast_transcript(self, stream: Any, agent_id: str, session: str) -> None:
        """Tell the PWA the history pane changed; failures are logged, never raised."""
        if stream is None or not agent_id:
            return
        try:
            stream.broadcast({
                "type": SSEType.TRANSCRIPT_UPDATED,
                "agent_id": agent_id,
                "session": session,
            })
        except Exception as error:  # noqa: BLE001
            log_exception(f"{self.runner}BroadcastFail", error, detail=agent_id)

    def bind_session(self, st: Any, session_id: str, *,
                     on_session_init, on_error, trace_id: str) -> None:
        """Bind the backend session id once per turn.

        ``st`` needs ``session_id``, ``saw_session`` and ``failed_error``. A
        rejected binding fails the turn through ``on_error``.
        """
        if not session_id or st.saw_session:
            return
        st.session_id = session_id
        st.saw_session = True
        log(f"{self.runner}SessionInit", f"sid={session_id} trace={trace_id or '∅'}")
        if on_session_init is not None:
            accepted = on_session_init(session_id)
            if accepted is False:
                st.failed_error = "backend session binding rejected"
                if on_error is not None:
                    on_error(st.failed_error)

    def speak(self, text: str, st: Any, *, agent_id: str, session: str,
              trace_id: str, enqueue: Callable[..., int],
              fail_event: str = "", fail_detail: Optional[str] = None) -> int:
        """Enqueue each completed <speak>…</speak> block of ``text`` once.

        ``text`` may be the accumulated segment, so a block only matches once
        its closing tag has streamed in; ``st.seen_speak`` keeps a later pass
        over the same text (or the same block echoed in a terminal event)
        from re-speaking it. Only voice turns speak; untagged prose stays
        silent. The persona prefix is added to the first chunk of each block
        when the agent is not the focused one.

        ``speak_agent_trace`` prefers the agent's stored trace over
        ``trace_id`` (Antigravity turns it off and uses the turn's trace
        only). Returns the number of chunks handed to ``enqueue``.
        """
        if not agent_id or not agents_db.latest_turn_synthesize_audio(agent_id):
            return 0
        blocks = [spoken_for_tts(m.group(1).strip()) for m in SPEAK_RE.finditer(text)]
        blocks = [b for b in blocks if b]
        if not blocks:
            return 0
        agent = agents_db.get_by_agent_id(agent_id)
        if agent is None:
            return 0
        persona = agent.get("persona") or ""
        voice_id = agent.get("voice_id") or ""
        focused = agents_db.get_focus()
        if self.speak_agent_trace:
            trace = agents_db.get_trace(agent_id) or trace_id or None
        else:
            trace = trace_id or None
        enqueued = 0
        for block in blocks:
            key = block.strip()
            if key in st.seen_speak:
                continue
            st.seen_speak.add(key)
            for index, chunk in enumerate(spoken_chunks_for_tts(block)):
                payload_text = chunk
                # Persona prefix only the first chunk of each explicit speak block.
                if (index == 0 and persona and agent_id != focused
                        and not chunk.lower().startswith(persona.lower())):
                    payload_text = f"{persona} here. {chunk}"
                try:
                    qid = enqueue(
                        agent_id=agent_id,
                        text=payload_text,
                        voice_id=voice_id,
                        session=session,
                        source=TurnSource.PWA,
                        trace_id=trace,
                        synthesize_audio=True,
                    )
                    enqueued += 1
                    log(f"{self.runner}Enqueue", f"agent={agent_id} qid={qid} chars={len(chunk)}")
                except Exception as error:  # noqa: BLE001
                    log_exception(
                        fail_event or f"{self.runner}EnqueueFail", error,
                        detail=str(agent_id) if fail_detail is None else fail_detail)
        return enqueued

    def persist_live_text(self, st: Any, *, text: str, backend_session_id: str,
                          agent_id: str, session: str, trace_id: str, stream: Any,
                          force: bool, interval: float | None = None) -> None:
        """Write one mutable assistant row at a bounded visual cadence.

        ``st`` needs ``persisted_live_text`` and ``last_live_write_at``. The
        row is keyed by the turn's trace, so a later transcript import
        replaces it with the durable assistant text. Unchanged text and writes
        inside ``interval`` (default ``live_text_interval``) of the last one
        (unless ``force``) are skipped.
        """
        if interval is None:
            interval = self.live_text_interval
        if not agent_id or not text.strip():
            return
        if text == st.persisted_live_text:
            return
        now = time.monotonic()
        if not force and st.last_live_write_at and (
                now - st.last_live_write_at < interval):
            return
        backend_session_id = backend_session_id or agents_db.live_backend_session(agent_id)
        if not backend_session_id:
            return
        try:
            row = agents_db.upsert_live_assistant_message(
                agent_id=agent_id, backend_session_id=backend_session_id,
                trace_id=trace_id, text=text,
            )
            st.persisted_live_text = text
            st.last_live_write_at = now
            if row and row.get("changed"):
                self.broadcast_transcript(stream, agent_id, session)
        except Exception as error:  # noqa: BLE001
            log_exception(f"{self.runner}LivePartialFail", error, detail=trace_id or agent_id)
