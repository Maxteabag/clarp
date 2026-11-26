"""Shared fixtures for the dispatch/state regression sweep.

Everything here is deliberately process-free: no CLI is executed, no socket is
opened. A turn is driven by calling the callbacks the dispatcher handed the
backend, which is exactly what a runner's drain thread does.
"""
from __future__ import annotations

import io
import itertools
from types import SimpleNamespace
from typing import Any

from lib import agents as agents_db
from lib import db, message_store
from lib.turn_dispatch import TurnDispatchService
import lib.turn_dispatch as _td

_IDS = itertools.count()


class FakeBackends:
    """Stand-in for ``lib.backends``. Records spawns instead of forking."""

    CLAUDE = "claude"
    CODEX = "codex"

    def __init__(self, backend: str = "claude"):
        self.backend = backend
        self.spawned: list[dict[str, Any]] = []
        self.interrupted: list[str] = []
        self.live = True

    def normalize(self, backend):
        return backend or self.backend

    def interrupt(self, backend, agent_id):
        self.interrupted.append(agent_id)
        return 1

    def active_handles(self, backend, agent_id):
        return ["handle"] if self.live else []

    def spawn_turn(self, backend, **kwargs):
        self.spawned.append(kwargs)


class FakeStream:
    def __init__(self):
        self.events: list[dict] = []

    def broadcast(self, event):
        self.events.append(event)


class Harness:
    """One agent, one dispatch service, and the seams a turn runs through."""

    def __init__(self, tmp_path, *, backend: str = "claude",
                 retry_scheduler=None):
        self.session = f"agent{next(_IDS)}"
        self.agent_id = agents_db.create_agent(
            persona=self.session.capitalize(), voice_id="v",
            cwd=str(tmp_path), session=self.session, backend=backend)
        agents_db.start_runtime(self.agent_id, self.session)
        self.backends = FakeBackends(backend)
        self.stream = FakeStream()
        self.ctx = SimpleNamespace(
            default_session=self.session,
            agents_path=tmp_path / "unused.json",
            stream=self.stream,
        )
        self.backend_session_id = f"bs-{self.agent_id}"
        # The dispatcher drops a Claude binding whose transcript is missing
        # ("ghost session"). Give the resume target a file so multi-turn tests
        # exercise dispatch rather than the ghost-session reset.
        transcript_dir = tmp_path / ".claude" / "projects" / "workspace"
        transcript_dir.mkdir(parents=True, exist_ok=True)
        (transcript_dir / f"{self.backend_session_id}.jsonl").write_text("")
        self.service = TurnDispatchService(
            self.ctx, backend_registry=self.backends, home=tmp_path,
            uuid_factory=lambda: self.backend_session_id,
            now=lambda: 1.0,
            retry_scheduler=retry_scheduler or (lambda delay, fn: None),
        )

    # -- driving -------------------------------------------------------
    def send(self, text: str = "yo", *, trace_id: str = "", **kwargs):
        trace_id = trace_id or f"trace-{next(_IDS)}"
        result = self.service.dispatch(
            text=text, requested_session=self.session,
            forced_session=self.session, trace_id=trace_id,
            synthesize_audio=False, **kwargs)
        return trace_id, result

    def last_call(self) -> dict[str, Any]:
        return self.backends.spawned[-1]

    # -- reading -------------------------------------------------------
    def state_kinds(self) -> list[str]:
        rows = db.conn().execute(
            "SELECT kind FROM state_log WHERE agent_id = ? ORDER BY ts, state_id",
            (self.agent_id,)).fetchall()
        return [row["kind"] for row in rows]

    def latest_kind(self) -> str:
        state = agents_db.latest_state(self.agent_id) or {}
        return str(state.get("kind") or "")

    def messages(self, *, include_automated: bool = False) -> list[dict]:
        return message_store.list_messages(
            agent_id=self.agent_id,
            backend_session_id=self.backend_session_id,
            include_automated=include_automated)

    def user_message_id(self, client_msg_id: str) -> str:
        return message_store._client_message_id(client_msg_id)

    def notifications(self) -> list[dict]:
        return [dict(row) for row in db.conn().execute(
            "SELECT * FROM user_notifications WHERE agent_id = ? ORDER BY done_ts",
            (self.agent_id,)).fetchall()]


def reset_dispatcher_globals() -> None:
    """The dispatcher keeps per-process in-flight/queue maps."""
    with _td._TURN_LOCK:
        _td._INFLIGHT.clear()
        _td._QUEUED.clear()
        _td._CLAIMED_AT.clear()


class FakeProc:
    """Enough of ``subprocess.Popen`` for a runner's drain thread body."""

    def __init__(self, *, stdout_lines: list[str] | None = None,
                 stderr: str = "", returncode: int = 0, pid: int = 4242):
        self.stdout = iter(list(stdout_lines or []))
        self.stderr = io.StringIO(stderr)
        self.returncode = returncode
        self.pid = pid
        self.terminated = False

    def wait(self, timeout=None):
        return self.returncode

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.terminated = True
