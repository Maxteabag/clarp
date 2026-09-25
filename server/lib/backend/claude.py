"""Claude Code: transcript-file resume, hook plugin and the dispatch lock."""
from __future__ import annotations

import pathlib
import time
import uuid
from typing import Any, Callable

from .base import Backend, adapter_terminal_argv, resolve


def _projects_root(home: pathlib.Path | None) -> pathlib.Path | None:
    return (home / ".claude" / "projects") if home is not None else None


class ClaudeBackend(Backend):
    """Runs through ``clarp_runner``; the only CLI that resumes by transcript
    file, pre-mints its session id and reports through the hook plugin."""

    # --- the runner -------------------------------------------------------

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
        """The transcript to resume from, or ``None`` for a ghost session."""
        if not session_id:
            return None
        return self.adapter.resume_transcript_finder(
            self.id, session_id, cwd, _projects_root(home))

    def bind_new_session(self, agent_id: str, session: str, *,
                         uuid_factory: Callable[[], str] | None = None) -> str:
        """Pre-mint the ``--session-id`` and bind it to the agent.

        A collision with an id already bound is retried once; any other
        binding failure is the dispatcher's 500.
        """
        from .. import agents as agents_db
        from ..log import log_exception
        mint = uuid_factory or (lambda: str(uuid.uuid4()))
        backend_session_id = mint()
        try:
            agents_db.bind_backend_session(agent_id, backend_session_id)
        except agents_db.SessionAlreadyBound as e:
            log_exception("sendPreStampCollision", e, detail=session)
            backend_session_id = mint()
            agents_db.bind_backend_session(agent_id, backend_session_id)
        return backend_session_id

    def find_transcript(self, session_id: str,
                        home: pathlib.Path | None = None) -> pathlib.Path | None:
        find = resolve(self.adapter.transcript_module, "find_latest_jsonl")
        root = _projects_root(home)
        return find(session_id, projects_root=root) if root is not None else find(session_id)

    def parse_transcript(self, path) -> list[dict]:
        return resolve(self.adapter.transcript_module, "parse_turns")(path)

    def list_sessions(self, cwd: str, *, limit: int = 20,
                      all_projects: bool = False) -> list[dict]:
        adapter = self.adapter
        return adapter.session_catalog_reader(
            adapter, cwd, limit=limit, all_projects=all_projects)

    # --- interactive terminal ---------------------------------------------

    def terminal_argv(self, session_id: str) -> list[str]:
        """The adapter argv plus the same plugin the ``-p`` dispatch loads, so
        an interactive terminal reports state exactly like a dispatched turn."""
        argv = adapter_terminal_argv(self, session_id)
        plugin = resolve("deployment", "plugin_dir")()
        if plugin is not None:
            argv += ["--plugin-dir", str(plugin)]
        return argv

    # --- credentials and quota --------------------------------------------

    def account_pool(self) -> str:
        return self.adapter.account_pool

    def quota_identity(self, window: dict) -> Any:
        """Claude reports the same reset with fractional-second jitter, so the
        notification identity rounds it to the minute. Only the identity is
        rounded; provider data and displayed reset times stay exact."""
        identity = window["window_id"]
        if window.get("kind") and window.get("resets_at"):
            import datetime
            try:
                reset = datetime.datetime.fromisoformat(
                    window["resets_at"].replace("Z", "+00:00"))
                if reset.tzinfo is not None:
                    reset_minute = int((reset.timestamp() + 30) // 60)
                    identity = ["claude-reset-minute-v1", window["kind"], reset_minute]
            except (ValueError, TypeError, OverflowError):
                pass
        return identity

    # --- turn plumbing ----------------------------------------------------

    def wrap_turn_callback(self, fn: Callable[..., Any]) -> Callable[..., Any]:
        """Runner callbacks arrive on the runner's own threads; serialise them
        under the dispatch lock."""
        def invoke(*args: Any):
            with resolve("turn_dispatch", "_TURN_LOCK"):
                return fn(*args)
        return invoke

    def arm_source_marker(self, session: str, trace_id: str,
                          synthesize_audio: bool, *,
                          home: pathlib.Path | None = None,
                          now: Callable[[], float] | None = None) -> None:
        """Write the single-use pwa-voice marker the UserPromptSubmit hook
        consumes, so the turn is tagged with its real source."""
        from .. import send_service
        from ..log import log_exception
        try:
            path = send_service.source_marker_path(home or pathlib.Path.home(), session)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(send_service.source_marker_text(
                session=session, trace_id=trace_id, now=(now or time.time)(),
                synthesize_audio=synthesize_audio))
        except OSError as e:
            log_exception("sendLastSourceWriteFail", e)
