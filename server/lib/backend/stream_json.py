"""``StreamJsonBackend``: what the JSON-lines CLIs share.

Codex, Antigravity, Grok and OpenCode each drive one subprocess per turn and
reproduce the same PWA side-effects off its stdout stream: agent-state
rows, transcript-updated SSEs, ``<speak>`` extraction into the TTS queue, a
bounded-cadence live assistant row and a session binding callback. Today
those bodies live in ``lib.runner_common`` and the per-CLI runner modules;
this class declares the shared surface and delegates to them. Slice 3 of
the contract moves the bodies here and turns the runner modules into thin
delegators.
"""
from __future__ import annotations

import pathlib
from typing import Any, Callable

from .base import Backend, resolve


class StreamJsonBackend(Backend):
    """A CLI that streams JSON lines and resumes by session id."""

    # --- the runner (per-CLI module, resolved late) -------------------------

    def spawn_turn(self, **spec: Any):
        adapter = self.adapter
        return resolve(adapter.runner_module, "spawn_turn")(**adapter.spawn_kwargs(spec))

    def interrupt(self, agent_id: str) -> int:
        return int(resolve(self.adapter.runner_module, "interrupt")(agent_id) or 0)

    def active_handles(self, agent_id: str) -> list:
        return list(resolve(self.adapter.runner_module, "active_handles")(agent_id) or [])

    def routing_cmd(self, prompt: str, *, model: str = "", effort: str = "") -> list[str]:
        return resolve(self.adapter.routing_module, "routing_cmd")(
            prompt, model=model, effort=effort)

    def routing_text(self, stdout: str) -> str:
        return resolve(self.adapter.routing_module, "routing_text")(stdout)

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

    # --- shared subprocess contract (runner_common today) -----------------
    # Each takes the ``backend=`` tag runner_common keys its log events by,
    # filled in from this backend's id.

    def launch(self, cmd: list[str], *, cwd: Any, session: str, **kwargs: Any):
        """Start one turn subprocess; ``(Popen, TurnHandle)``."""
        return resolve("runner_common", "launch")(
            cmd, cwd=cwd, session=session, **kwargs)

    def start_drain(self, handle, target: Callable[..., None], /, **kwargs: Any):
        """Start the daemon thread that drains the turn's stdout."""
        return resolve("runner_common", "start_drain")(
            handle, target, backend=self.id, **kwargs)

    def record_state(self, agent_id: str, kind: str, detail: dict | None, *,
                     event: str = "") -> None:
        """Record one agent-state row for the turn; never raises."""
        return resolve("runner_common", "record_state")(
            agent_id, kind, detail, backend=self.id, event=event)

    def broadcast_transcript(self, stream: Any, agent_id: str, session: str) -> None:
        """Tell listening clients the session's transcript advanced."""
        return resolve("runner_common", "broadcast_transcript")(
            stream, agent_id, session, backend=self.id)

    def speak(self, text: str, st: Any, **kwargs: Any) -> int:
        """Enqueue each completed ``<speak>`` block of ``text`` once."""
        return resolve("runner_common", "speak")(text, st, backend=self.id, **kwargs)

    def persist_live_text(self, st: Any, **kwargs: Any) -> None:
        """Write the mutable live assistant row at a bounded cadence."""
        return resolve("runner_common", "persist_live_text")(
            st, backend=self.id, **kwargs)

    def bind_session(self, st: Any, session_id: str, **kwargs: Any) -> None:
        """Bind the id the CLI reported to the agent's runtime row."""
        return resolve("runner_common", "bind_session")(
            st, session_id, backend=self.id, **kwargs)
