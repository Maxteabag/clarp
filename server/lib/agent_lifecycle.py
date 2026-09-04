"""Application service for creating, relaunching, forking, and deleting agents."""
from __future__ import annotations

import os
import base64
import json
import pathlib
import secrets
import tempfile
import threading
from dataclasses import dataclass
from contextlib import contextmanager

from . import agents as agents_db
from . import backends
from .agent_store import AGENT_ROSTER, load_agents, save_agents
from .db import now_ms
from .fork import fork_session
from .log import log, log_exception
from .mcp_selection import encode as encode_mcp_selection
from .protocol import SSEType
from .roster import lookup_persona
from . import personas as persona_store


class AgentLifecycleError(RuntimeError):
    def __init__(self, status: int, code: str, *, message: str | None = None,
                 extra: dict | None = None):
        self.status = status
        self.code = code
        self.message = message or code
        self.extra = extra or {}
        super().__init__(self.message)

    def response(self) -> dict:
        return {"error": self.code, **self.extra, "message": self.message}


@dataclass(frozen=True)
class AgentLifecycleResult:
    session: str
    persona: str
    voice_id: str
    backend: str


@dataclass(frozen=True)
class AgentResetResult:
    old_session: str
    new_session: str
    old_agent_id: str
    new_agent_id: str
    persona: str

    def response(self) -> dict[str, str]:
        return {
            "old_session": self.old_session,
            "new_session": self.new_session,
            "old_agent_id": self.old_agent_id,
            "new_agent_id": self.new_agent_id,
            "persona": self.persona,
        }


class _LifecycleGate:
    """Concurrent dispatch/terminal reads with exclusive identity mutation."""

    def __init__(self) -> None:
        self._condition = threading.Condition(threading.RLock())
        self._readers = 0
        self._reader_depth: dict[int, int] = {}
        self._writer: int | None = None
        self._write_depth = 0
        self._waiting_writers = 0

    @contextmanager
    def read(self):
        ident = threading.get_ident()
        with self._condition:
            depth = self._reader_depth.get(ident, 0)
            if self._writer != ident and depth == 0:
                while self._writer is not None or self._waiting_writers:
                    self._condition.wait()
            self._readers += 1
            self._reader_depth[ident] = depth + 1
        try:
            yield
        finally:
            with self._condition:
                self._readers -= 1
                depth = self._reader_depth[ident] - 1
                if depth:
                    self._reader_depth[ident] = depth
                else:
                    self._reader_depth.pop(ident, None)
                if self._readers == 0:
                    self._condition.notify_all()

    @contextmanager
    def write(self):
        ident = threading.get_ident()
        with self._condition:
            if self._writer == ident:
                self._write_depth += 1
            else:
                self._waiting_writers += 1
                try:
                    while self._writer is not None or self._readers:
                        self._condition.wait()
                    self._writer = ident
                    self._write_depth = 1
                finally:
                    self._waiting_writers -= 1
        try:
            yield
        finally:
            with self._condition:
                self._write_depth -= 1
                if self._write_depth == 0:
                    self._writer = None
                    self._condition.notify_all()


class AgentLifecycleService:
    _create_lock = threading.RLock()
    _lifecycle_gate = _LifecycleGate()

    def __init__(self, ctx):
        self.ctx = ctx

    def create(self, data: dict) -> AgentLifecycleResult:
        # Reset/delete share this lock, so they cannot swap the identity while
        # creation performs slow fork/avatar/TTS preparation. Dispatch keeps its
        # own per-agent serialization and is not globally stalled by provider I/O.
        with self._create_lock:
            return self._create_locked(data)

    def _create_locked(self, data: dict) -> AgentLifecycleResult:
        persona = (data.get("name") or "").strip()
        if not persona:
            raise AgentLifecycleError(400, "name required")
        voice_id = (data.get("voice_id") or "").strip()
        persona_definition = persona_store.get(persona)
        avatar_temp = None
        avatar_raw = None
        avatar_data = str(data.get("avatar_base64") or "").strip()
        if avatar_data:
            try:
                raw = base64.b64decode(avatar_data, validate=True)
                if len(raw) > 512_000:
                    raise ValueError("avatar is too large")
                avatar_raw = raw
            except (ValueError, OSError) as exc:
                raise AgentLifecycleError(400, "invalid avatar", message=str(exc)) from exc
        # Session id selection. The client (incl. the native app) often
        # re-sends a persona-derived id like "bella" on every create. If we
        # honored that verbatim it would collide with the soft-deleted old
        # "bella" row and RESURRECT it — old agent_id, old turns/clips/
        # conversation all come back. That's the "I deleted it but the old
        # conversation is still there" bug.
        #
        # So: honor an explicit id only when it's genuinely unused (fresh
        # automation/tests). Otherwise mint a unique `<persona>-<hex>` id so
        # "delete and start over" is a truly fresh agent. Deliberate
        # resurrection still happens via replace_sid (relaunch) below.
        explicit_session = "".join(
            c for c in (data.get("session") or "").strip()
            if c.isalnum() or c in "._-")
        if explicit_session and not agents_db.session_exists(explicit_session):
            session = explicit_session
        else:
            session = self._mint_session(persona)
        cwd = _existing_cwd(data.get("cwd"))
        replace_sid = (data.get("replace_sid") or "").strip()
        fork_id = (data.get("fork_session_id") or "").strip()
        backend = backends.normalize(data.get("backend"))
        synthesize_audio = data.get("synthesize_audio", True) is not False
        agents = load_agents(self.ctx.agents_path)
        clear_retained_model = False
        clear_retained_effort = False
        retained_model = ""
        retained_effort = ""
        if replace_sid:
            if replace_sid not in agents:
                raise AgentLifecycleError(404, "no such agent to replace")
            session = replace_sid
            current = agents[replace_sid]
            voice_id = voice_id or current.get("voice_id") or voice_id
            persona = persona or current.get("name") or persona
            if not data.get("backend"):
                backend = backends.normalize(current.get("backend"))
            existing_agent = agents_db.get_by_session(replace_sid) or {}
            # A relaunch inherits the agent's directory the same way it inherits
            # voice and backend. Without this an omitted cwd falls back to $HOME:
            # on a host that silently wakes the agent up outside its repo, and in
            # a container it is rejected as outside the workspace root.
            if not str(data.get("cwd") or "").strip():
                cwd = _existing_cwd(
                    current.get("cwd") or existing_agent.get("cwd"))
            previous_backend = backends.normalize(existing_agent.get("backend"))
            retained_model = str(existing_agent.get("model") or "").strip()
            retained_effort = str(existing_agent.get("effort") or "").strip()
            if backend != previous_backend:
                clear_retained_model = (
                    "model" not in data
                    and not backends.is_valid_model(backend, retained_model)
                )
                clear_retained_effort = (
                    "effort" not in data and bool(retained_effort)
                    and retained_effort not in backends.valid_efforts(backend)
                )
        occupied = next((
            (sid, info) for sid, info in agents.items()
            if sid != replace_sid
            and str((info or {}).get("name") or sid).strip().casefold()
            == persona.casefold()
        ), None)
        if occupied is not None:
            owner_session, owner_info = occupied
            owner = str((owner_info or {}).get("name") or owner_session)
            raise AgentLifecycleError(
                409, "contact_occupied",
                message=(f"{owner} already has an active session. "
                         "Open or release that chat before starting another."),
                extra={"owner": owner, "session": owner_session},
            )
        if not replace_sid and session in agents:
            owner = (agents[session] or {}).get("name") or session
            raise AgentLifecycleError(
                409, "session_taken",
                message=(f"Cannot start {persona}: session '{session}' is already "
                         f"used by {owner}. Stop {owner} first or pick a different session id."),
                extra={"owner": owner, "session": session},
            )
        if "model" in data and data.get("model") is not None \
                and not isinstance(data.get("model"), str):
            raise AgentLifecycleError(400, "model must be a string or null")
        requested_model = (data.get("model") or "").strip() if "model" in data else ""
        if "model" in data and not backends.is_valid_model(backend, requested_model):
            raise AgentLifecycleError(400, "invalid model for backend")
        if "effort" in data and data.get("effort") is not None \
                and not isinstance(data.get("effort"), str):
            raise AgentLifecycleError(400, "effort must be a string or null")
        requested_effort = ((data.get("effort") or "").strip().lower()
                            if "effort" in data else "")
        if ("effort" in data and requested_effort
                and requested_effort not in backends.valid_efforts(backend)):
            raise AgentLifecycleError(400, "invalid effort for backend")
        effective_requested_model = (
            requested_model if "model" in data
            else ("" if clear_retained_model else retained_model))
        effective_requested_effort = (
            requested_effort if "effort" in data
            else ("" if clear_retained_effort else retained_effort))
        from . import config as app_config
        cfg = app_config.load()
        requested_mcp_servers: list[str] | None = None
        if "mcp_servers" in data:
            raw_mcp = data.get("mcp_servers")
            if not isinstance(raw_mcp, list) or any(
                    not isinstance(item, str) for item in raw_mcp):
                raise AgentLifecycleError(400, "mcp_servers must be a list of names")
            requested_mcp_servers = list(dict.fromkeys(
                item.strip() for item in raw_mcp if item.strip()))
            if requested_mcp_servers and backend != backends.CLAUDE:
                raise AgentLifecycleError(
                    400, "mcp servers unsupported for backend",
                    message=f"MCP server selection is unavailable for {backends.label(backend)}.")
            available_mcp = app_config.read_global_mcp_servers()
            unknown_mcp = [
                item for item in requested_mcp_servers if item not in available_mcp
            ]
            if unknown_mcp:
                raise AgentLifecycleError(
                    400, "unknown mcp server",
                    message=f"Unknown MCP server: {', '.join(unknown_mcp)}")
        effective_validation_model = effective_requested_model
        if backend == backends.AGY:
            effective_validation_model = (
                effective_validation_model or cfg.agy_model.strip())
        if (backend == backends.AGY and effective_validation_model
                and effective_requested_effort):
            raise AgentLifecycleError(
                400, "AGY model-specific effort compatibility is unknown")
        if not voice_id:
            _, roster_voice = lookup_persona(persona)
            voice_id = ((persona_definition or {}).get("voice_id")
                        or roster_voice or next(iter(AGENT_ROSTER.values())))
        from .voice import CARTESIA, resolve_voice
        selected_cartesia = resolve_voice(voice_id, CARTESIA)
        if selected_cartesia:
            for sid, info in agents.items():
                if sid == replace_sid:
                    continue
                existing_cartesia = (
                    resolve_voice((info or {}).get("voice_id"), CARTESIA)
                    or cfg.cartesia_voice_for(str((info or {}).get("name") or "")))
                if existing_cartesia == selected_cartesia:
                    raise AgentLifecycleError(
                        409, "voice_in_use",
                        message=f"That voice is already used by {(info or {}).get('name') or sid}.")

        resume_session_id = (data.get("resume_session_id") or "").strip()
        if fork_id:
            if not backends.capabilities(backend).supports_fork:
                raise AgentLifecycleError(
                    400, "fork_unsupported",
                    message=f"{backends.label(backend)} does not support session forks.",
                )
            try:
                resume_session_id = fork_session(fork_id, cwd)
                log("forkOk", f"{fork_id} -> {resume_session_id} in {cwd}")
            except FileNotFoundError as e:
                log_exception("forkSourceMissing", e, detail=fork_id)
                raise AgentLifecycleError(404, "fork source not found") from e
            except OSError as e:
                log_exception("forkIoFail", e, detail=fork_id)
                raise AgentLifecycleError(500, "fork io failed") from e

        self._reject_owned_backend_session(
            backend=backend,
            backend_session_id=resume_session_id,
            session=session,
        )
        if avatar_raw is not None:
            from .deployment import LAYOUT
            avatar_dir = LAYOUT.data_root / "avatars"
            avatar_dir.mkdir(parents=True, exist_ok=True)
            handle = tempfile.NamedTemporaryFile(
                dir=avatar_dir, prefix=".avatar-", suffix=".jpg", delete=False)
            handle.write(avatar_raw); handle.close()
            avatar_temp = pathlib.Path(handle.name)
        agents[session] = {
            "name": persona, "voice_id": voice_id, "cwd": cwd, "backend": backend,
        }
        save_agents(agents, self.ctx.agents_path)
        agent = agents_db.get_by_session(session)
        if not agent:
            raise AgentLifecycleError(500, "agent persistence failed")
        # Per-agent model / effort override (create + relaunch). Only fields the
        # client actually sent are touched, so a relaunch that omits them keeps
        # the existing pins. Effort is validated against the backend's CLI.
        llm_update: dict[str, str] = {}
        if "model" in data:
            llm_update["model"] = requested_model
        elif clear_retained_model:
            llm_update["model"] = ""
        if "effort" in data:
            llm_update["effort"] = requested_effort
        elif clear_retained_effort:
            llm_update["effort"] = ""
        if llm_update:
            agents_db.update_agent(agent["agent_id"], **llm_update)
        if requested_mcp_servers is not None:
            agents_db.update_agent(
                agent["agent_id"],
                mcp_servers=encode_mcp_selection(requested_mcp_servers),
            )
        presentation_update = {
            "avatar_symbol": str(data.get("avatar_symbol")
                                 or (persona_definition or {}).get("avatar_symbol") or "").strip()[:64],
            "personality": str(data.get("personality")
                               or (persona_definition or {}).get("personality") or "").strip()[:4000],
            "avatar_path": str((persona_definition or {}).get("avatar_path") or ""),
        }
        if replace_sid:
            presentation_update = {
                key: value for key, value in presentation_update.items()
                if key in data
            }
        if presentation_update:
            agents_db.update_agent(agent["agent_id"], **presentation_update)
        if avatar_temp:
            try:
                from .deployment import LAYOUT
                avatar_dir = LAYOUT.data_root / "avatars"
                path = avatar_dir / f"{agent['agent_id']}.jpg"
                avatar_temp.replace(path)
                agents_db.update_agent(agent["agent_id"], avatar_path=str(path))
            except OSError as exc:
                raise AgentLifecycleError(400, "invalid avatar", message=str(exc)) from exc
        agents_db.start_runtime(agent["agent_id"], session)
        if resume_session_id:
            try:
                agents_db.bind_backend_session(agent["agent_id"], resume_session_id)
            except agents_db.SessionAlreadyBound as e:
                agents_db.end_current_runtime(agent["agent_id"])
                log_exception("agentResumeBindConflict", e, detail=session)
                raise AgentLifecycleError(409, "session in use") from e
        agents_db.record_path_usage(cwd)

        announcement = (
            f"{persona} relaunched." if replace_sid
            else f"{persona} forked and ready." if fork_id
            else f"{persona} is ready."
        )
        if synthesize_audio:
            # Pass session so the announcement engine can resolve the agent's
            # persona → Cartesia voice (else it falls back to ElevenLabs).
            self.ctx.speak_announcement(announcement, voice_id, session=session)
        self.ctx.stream.broadcast({
            "type": SSEType.AGENT_ROSTER,
            "kind": "relaunched" if replace_sid else ("forked" if fork_id else "created"),
            "session": session,
            "persona": persona,
            "voice_id": voice_id,
            "backend": backend,
        })
        return AgentLifecycleResult(session, persona, voice_id, backend)

    @staticmethod
    def _mint_session(persona: str) -> str:
        """Mint a fresh, unique session id from a persona name.

        Shape: `<persona-slug>-<4 hex>` e.g. `antoni-3f9c`. Checked against
        ALL agent rows (including soft-deleted) so it never resurrects a
        prior agent. The slug keeps the id human-readable in logs/UI."""
        base = "".join(c for c in persona.lower()
                       if c.isalnum() or c in "._-") or "agent"
        for _ in range(20):
            sid = f"{base}-{secrets.token_hex(2)}"
            if not agents_db.session_exists(sid):
                return sid
        # Astronomically unlikely; widen the entropy as a last resort.
        return f"{base}-{secrets.token_hex(8)}"

    def delete(self, session: str) -> None:
        with self._create_lock:
            with self._lifecycle_gate.write():
                self._delete_locked(session)

    def _delete_locked(self, session: str) -> None:
        session = session.strip("/")
        if not session:
            raise AgentLifecycleError(400, "name required")
        agent = agents_db.get_by_session(session)
        if not agent:
            raise AgentLifecycleError(
                404, "agent_not_found", message=f"No active agent session: {session}")
        from . import turn_dispatch
        turn_dispatch.clear_for_agent(agent["agent_id"])
        backends.interrupt_any(agent["agent_id"])
        agents_db.soft_delete(agent["agent_id"])
        if agents_db.get_focus() == agent["agent_id"]:
            agents_db.set_focus(None)
        self.ctx.stream.broadcast({
            "type": SSEType.AGENT_ROSTER, "kind": "deleted", "session": session,
        })

    def reset(self, sessions: list[str]) -> list[AgentResetResult]:
        """Replace active sessions with fresh identities in one DB transaction."""
        with (self._create_lock,
              self._lifecycle_gate.write(),
              agents_db.focus_guard()):
            normalized = [str(session or "").strip().strip("/") for session in sessions]
            if not normalized or any(not session for session in normalized):
                raise AgentLifecycleError(400, "sessions required")
            if len(normalized) > 50:
                raise AgentLifecycleError(400, "too many sessions")
            if len(set(normalized)) != len(normalized):
                raise AgentLifecycleError(400, "duplicate sessions")
            from . import db, turn_dispatch

            preflight = [agents_db.get_by_session(session) for session in normalized]
            missing = [
                session for session, agent in zip(normalized, preflight)
                if agent is None
            ]
            if missing:
                raise AgentLifecycleError(
                    404, "agent_not_found",
                    message=f"No active agent session: {', '.join(missing)}",
                    extra={"missing": missing},
                )
            default_session = str(
                getattr(self.ctx, "default_session", "") or "").strip()
            if default_session in normalized:
                raise AgentLifecycleError(
                    409, "default_session_reset_forbidden",
                    message=(f"Cannot reset the default session '{default_session}'. "
                             "Change [server] default_session and restart Clarp first."),
                    extra={"session": default_session},
                )
            preflight_agents = [
                dict(agent) for agent in preflight if agent is not None
            ]
            preflight_ids = [str(agent["agent_id"]) for agent in preflight_agents]
            session_by_id = {
                str(agent["agent_id"]): str(agent["session"])
                for agent in preflight_agents
            }
            blockers = turn_dispatch.reset_blockers(preflight_ids)
            if blockers["spawning"]:
                sessions = [
                    session_by_id[agent_id] for agent_id in blockers["spawning"]
                ]
                raise AgentLifecycleError(
                    409, "agent_reset_spawn_in_progress",
                    message=("Agent work is still starting for: "
                             f"{', '.join(sessions)}. Retry shortly."),
                    extra={"sessions": sessions},
                )
            if blockers["terminals"]:
                sessions = [
                    session_by_id[agent_id] for agent_id in blockers["terminals"]
                ]
                raise AgentLifecycleError(
                    409, "agent_reset_terminal_active",
                    message=("Close the live terminal for: "
                             f"{', '.join(sessions)} before resetting."),
                    extra={"sessions": sessions},
                )
            from . import compaction
            process_busy = set(blockers["turns"])
            process_busy.update(blockers["memory_queues"])
            process_busy.update(blockers["routing"])
            process_busy.update(
                agent_id for agent_id in preflight_ids
                if any(
                    handle.is_alive()
                    for handle in backends.active_handles_any(agent_id)
                )
            )
            process_busy.update(
                str(agent["agent_id"]) for agent in preflight_agents
                if agents_db.is_busy(str(agent["agent_id"]))
                or compaction.is_compacting(str(agent["session"]))
            )
            if process_busy:
                sessions = [
                    session_by_id[agent_id]
                    for agent_id in preflight_ids if agent_id in process_busy
                ]
                raise AgentLifecycleError(
                    409, "agent_reset_active_work",
                    message=("Stop active work for: "
                             f"{', '.join(sessions)} before resetting."),
                    extra={"sessions": sessions},
                )

            connection = db.conn()
            results: list[AgentResetResult] = []
            focus_file_restore = None
            focused_result: AgentResetResult | None = None
            session_by_id: dict[str, str] = {}
            try:
                connection.execute("BEGIN IMMEDIATE")
                snapshots = [agents_db.get_by_session(session) for session in normalized]
                missing = [
                    session for session, agent in zip(normalized, snapshots)
                    if agent is None
                ]
                if missing:
                    raise AgentLifecycleError(
                        404, "agent_not_found",
                        message=f"No active agent session: {', '.join(missing)}",
                        extra={"missing": missing},
                    )
                default_session = str(
                    getattr(self.ctx, "default_session", "") or "").strip()
                if default_session in normalized:
                    raise AgentLifecycleError(
                        409, "default_session_reset_forbidden",
                        message=(f"Cannot reset the default session '{default_session}'. "
                                 "Change [server] default_session and restart "
                                 "Clarp first."),
                        extra={"session": default_session},
                    )
                agents = [dict(agent) for agent in snapshots if agent is not None]
                fresh_sessions: list[str] = []
                reserved: set[str] = set()
                for agent in agents:
                    for _ in range(20):
                        candidate = self._mint_session(str(agent["persona"]))
                        if candidate not in reserved:
                            reserved.add(candidate)
                            fresh_sessions.append(candidate)
                            break
                    else:
                        raise AgentLifecycleError(
                            500, "fresh session allocation failed")

                agent_ids = [str(agent["agent_id"]) for agent in agents]
                session_by_id = {
                    str(agent["agent_id"]): str(agent["session"])
                    for agent in agents
                }
                placeholders = ",".join("?" for _ in agent_ids)
                durable_queue_ids = {
                    str(row["agent_id"])
                    for row in connection.execute(
                        f"""SELECT DISTINCT agent_id FROM queued_turns
                              WHERE agent_id IN ({placeholders})
                                AND status IN ('queued','claimed')""",
                        tuple(agent_ids),
                    ).fetchall()
                }
                background_job_ids = {
                    str(row["agent_id"])
                    for row in connection.execute(
                        f"""SELECT DISTINCT agent_id FROM background_jobs
                              WHERE agent_id IN ({placeholders})
                                AND status IN ('queued','running')""",
                        tuple(agent_ids),
                    ).fetchall()
                }
                dream_run_ids = {
                    str(row["agent_id"])
                    for row in connection.execute(
                        f"""SELECT DISTINCT agent_id FROM dream_runs
                              WHERE agent_id IN ({placeholders})
                                AND status='active'""",
                        tuple(agent_ids),
                    ).fetchall()
                }
                tts_job_ids = {
                    str(row["agent_id"])
                    for row in connection.execute(
                        f"""SELECT DISTINCT agent_id FROM tts_queue
                              WHERE agent_id IN ({placeholders})
                                AND status IN ('queued','synthesizing')""",
                        tuple(agent_ids),
                    ).fetchall()
                }
                oracle_delegation_ids = {
                    str(row["agent_id"])
                    for row in connection.execute(
                        f"""SELECT DISTINCT agent_id FROM oracle_delegations
                              WHERE agent_id IN ({placeholders})
                                AND status IN ('accepted','queued')""",
                        tuple(agent_ids),
                    ).fetchall()
                }
                task_plan_ids = {
                    str(row["agent_id"])
                    for row in connection.execute(
                        f"""SELECT DISTINCT agent_id FROM task_plans
                              WHERE agent_id IN ({placeholders})
                                AND status='active'""",
                        tuple(agent_ids),
                    ).fetchall()
                }
                active_artifact_ids = {
                    str(row["agent_id"])
                    for row in connection.execute(
                        f"""SELECT DISTINCT agent_id FROM artifacts
                              WHERE agent_id IN ({placeholders})
                                AND status IN ('active','draft')
                                AND deleted_at IS NULL""",
                        tuple(agent_ids),
                    ).fetchall()
                }
                pending_delivery_sessions = {
                    str(row["session"])
                    for row in connection.execute(
                        f"""SELECT DISTINCT session FROM decision_deliveries
                              WHERE session IN ({placeholders})
                                AND status='pending'""",
                        tuple(normalized),
                    ).fetchall()
                }
                pending_routing_sessions = {
                    str(value)
                    for row in connection.execute(
                        f"""SELECT requested_session,candidate_session,speak_as_session
                              FROM orchestrator_pending_utterances
                             WHERE status='pending' AND expires_at>?
                               AND (requested_session IN ({placeholders})
                                 OR candidate_session IN ({placeholders})
                                 OR speak_as_session IN ({placeholders}))""",
                        (now_ms(), *normalized, *normalized, *normalized),
                    ).fetchall()
                    for value in row
                    if value
                }
                pending_clip_ids = {
                    str(row["agent_id"])
                    for row in connection.execute(
                        f"""SELECT DISTINCT agent_id FROM clips
                              WHERE agent_id IN ({placeholders})
                                AND COALESCE(status,'synthesized')
                                    NOT IN ('play-ok','play-fail')
                                AND COALESCE(producer_status,'complete')!='failed'""",
                        tuple(agent_ids),
                    ).fetchall()
                }
                session_to_id = {
                    str(agent["session"]): str(agent["agent_id"])
                    for agent in agents
                }
                portrait_job_ids: set[str] = set()
                portrait_jobs = connection.execute(
                    """SELECT metadata_json FROM background_jobs
                         WHERE owner_kind='computer'
                           AND kind='portrait-generation'
                           AND status IN ('queued','running')"""
                ).fetchall()
                for row in portrait_jobs:
                    try:
                        target_session = str(
                            json.loads(row["metadata_json"] or "{}").get("session")
                            or "")
                    except (TypeError, json.JSONDecodeError):
                        continue
                    agent_id = session_to_id.get(target_session)
                    if agent_id:
                        portrait_job_ids.add(agent_id)
                busy_ids: set[str] = set()
                busy_ids.update(durable_queue_ids)
                busy_ids.update(background_job_ids)
                busy_ids.update(dream_run_ids)
                busy_ids.update(tts_job_ids)
                busy_ids.update(oracle_delegation_ids)
                busy_ids.update(task_plan_ids)
                busy_ids.update(active_artifact_ids)
                busy_ids.update(pending_clip_ids)
                busy_ids.update(
                    session_to_id[session]
                    for session in pending_delivery_sessions
                    if session in session_to_id
                )
                busy_ids.update(
                    session_to_id[session]
                    for session in pending_routing_sessions
                    if session in session_to_id
                )
                busy_ids.update(portrait_job_ids)
                if busy_ids:
                    sessions = [
                        session_by_id[agent_id]
                        for agent_id in agent_ids if agent_id in busy_ids
                    ]
                    raise AgentLifecycleError(
                        409, "agent_reset_active_work",
                        message=("Stop active work for: "
                                 f"{', '.join(sessions)} before resetting."),
                        extra={"sessions": sessions},
                    )

                focused = agents_db.get_focus()
                for agent, fresh_session in zip(agents, fresh_sessions):
                    old_agent_id = str(agent["agent_id"])
                    new_agent_id = agents_db.create_agent(
                        persona=str(agent["persona"]),
                        voice_id=str(agent["voice_id"]),
                        cwd=str(agent["cwd"]),
                        session=fresh_session,
                        backend=str(agent["backend"]),
                        model=str(agent["model"]),
                        effort=str(agent["effort"]),
                    )
                    agents_db.update_agent(
                        new_agent_id,
                        mcp_servers=str(agent["mcp_servers"]),
                        heartbeat_enabled=bool(agent["heartbeat_enabled"]),
                        dreaming_enabled=bool(agent["dreaming_enabled"]),
                        dreaming_last_local_date=agent.get(
                            "dreaming_last_local_date"),
                        muted=bool(agent["muted"]),
                        avatar_symbol=str(agent["avatar_symbol"]),
                        personality=str(agent["personality"]),
                        avatar_path=str(agent["avatar_path"]),
                    )
                    if agent.get("archived_at") is not None:
                        agents_db.set_archived(new_agent_id, True)
                    agents_db.start_runtime(new_agent_id, fresh_session)
                    connection.execute(
                        """INSERT INTO settings(key,value,updated_at) VALUES(?,?,?)
                             ON CONFLICT(key) DO UPDATE SET
                               value=excluded.value,updated_at=excluded.updated_at""",
                        (f"session_redirect.{agent['session']}", fresh_session,
                         now_ms()),
                    )
                    self._move_reset_configuration(
                        connection, old_agent_id=old_agent_id,
                        new_agent_id=new_agent_id, new_session=fresh_session,
                    )
                    self._drop_reset_queue(connection, old_agent_id)
                    agents_db.soft_delete(old_agent_id)
                    if focused == old_agent_id:
                        agents_db.set_focus(new_agent_id)
                    result = AgentResetResult(
                        old_session=str(agent["session"]),
                        new_session=fresh_session,
                        old_agent_id=old_agent_id,
                        new_agent_id=new_agent_id,
                        persona=str(agent["persona"]),
                    )
                    results.append(result)
                    if focused == old_agent_id:
                        focused_result = result
                if focused_result is not None:
                    focus_file_restore = self._replace_focus_file(
                        focused_result.new_session)
                connection.execute("COMMIT")
                focus_file_restore = None
            except Exception as exc:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                if focus_file_restore is not None:
                    try:
                        self._restore_focus_file(*focus_file_restore)
                    except Exception as restore_exc:
                        log_exception(
                            "agentResetFocusRollbackFail", restore_exc,
                            detail=str(focus_file_restore[0]))
                if isinstance(exc, AgentLifecycleError):
                    raise
                log_exception("agentResetFail", exc, detail=",".join(normalized))
                raise AgentLifecycleError(
                    500, "agent_reset_failed",
                    message="Agent reset was rolled back",
                ) from exc

            for result in results:
                self.ctx.stream.broadcast({
                    "type": SSEType.AGENT_ROSTER,
                    "kind": "reset",
                    "session": result.new_session,
                    "old_session": result.old_session,
                    "persona": result.persona,
                })
            if focused_result is not None:
                herald = getattr(self.ctx, "herald", None)
                if herald is not None:
                    try:
                        herald.set_focus(focused_result.new_session)
                    except Exception as exc:
                        log_exception(
                            "agentResetHeraldFocusFail", exc,
                            detail=focused_result.new_session)
                self.ctx.stream.broadcast({
                    "type": SSEType.AGENT_FOCUS,
                    "session": focused_result.new_session,
                    "agent_id": focused_result.new_agent_id,
                })
            return results

    @staticmethod
    def _move_reset_configuration(
        connection, *, old_agent_id: str, new_agent_id: str, new_session: str,
    ) -> None:
        """Move identity-linked settings, leaving conversation/history behind."""
        connection.execute(
            "UPDATE team_members SET agent_id=? WHERE agent_id=?",
            (new_agent_id, old_agent_id),
        )
        connection.execute(
            """UPDATE team_inbox SET agent_id=?
                 WHERE agent_id=? AND status='unread'""",
            (new_agent_id, old_agent_id),
        )
        connection.execute(
            "UPDATE teams SET leader_agent_id=? WHERE leader_agent_id=?",
            (new_agent_id, old_agent_id),
        )
        connection.execute(
            "UPDATE vocab_assignments SET agent_id=? WHERE agent_id=?",
            (new_agent_id, old_agent_id),
        )
        connection.execute(
            "UPDATE agent_portraits SET agent_id=? WHERE agent_id=?",
            (new_agent_id, old_agent_id),
        )
        connection.execute(
            """UPDATE media_assets SET agent_id=?,session=?
                 WHERE agent_id=? AND deleted_at IS NULL""",
            (new_agent_id, new_session, old_agent_id),
        )
        connection.execute(
            "UPDATE task_plans SET agent_id=?,session=? WHERE agent_id=?",
            (new_agent_id, new_session, old_agent_id),
        )
        connection.execute(
            "UPDATE artifacts SET agent_id=?,session=? WHERE agent_id=?",
            (new_agent_id, new_session, old_agent_id),
        )
        connection.execute(
            "UPDATE decision_deliveries SET session=? WHERE session=?",
            (new_session, str(connection.execute(
                "SELECT session FROM agents WHERE agent_id=?",
                (old_agent_id,),
            ).fetchone()["session"])),
        )
        connection.execute(
            """UPDATE dream_runs SET agent_id=?,session=?,updated_at=?
                 WHERE agent_id=?""",
            (new_agent_id, new_session, now_ms(), old_agent_id),
        )
        connection.execute(
            """UPDATE agent_schedules
                  SET agent_id=?,session=?,updated_at=? WHERE agent_id=?""",
            (new_agent_id, new_session, now_ms(), old_agent_id),
        )

    @staticmethod
    def _drop_reset_queue(connection, old_agent_id: str) -> None:
        """Remove acknowledged queued work within the reset transaction."""
        from . import prompt_admissions

        rows = connection.execute(
            "SELECT prompt_admission_id FROM queued_turns WHERE agent_id=?",
            (old_agent_id,),
        ).fetchall()
        connection.execute(
            "DELETE FROM queued_turns WHERE agent_id=?", (old_agent_id,))
        for row in rows:
            prompt_admissions.delete_unmaterialized(
                str(row["prompt_admission_id"] or ""))
        connection.execute(
            """INSERT INTO queue_state_revisions(agent_id,revision,paused)
                 VALUES (?,1,0)
                 ON CONFLICT(agent_id) DO UPDATE SET
                   revision=queue_state_revisions.revision+1,paused=0""",
            (old_agent_id,),
        )

    @staticmethod
    def _replace_focus_file(session: str):
        from .paths import RuntimePaths

        path = RuntimePaths.from_home(pathlib.Path.home()).app_session
        path.parent.mkdir(parents=True, exist_ok=True)
        previous = path.read_bytes() if path.is_file() else None
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=f".{path.name}.", delete=False,
        ) as handle:
            handle.write((session + "\n").encode())
            temporary = pathlib.Path(handle.name)
        temporary.chmod(0o600)
        temporary.replace(path)
        return path, previous

    @staticmethod
    def _restore_focus_file(path: pathlib.Path, previous: bytes | None) -> None:
        if previous is None:
            path.unlink(missing_ok=True)
            return
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=f".{path.name}.rollback.", delete=False,
        ) as handle:
            handle.write(previous)
            temporary = pathlib.Path(handle.name)
        temporary.chmod(0o600)
        temporary.replace(path)

    @staticmethod
    def _reject_owned_backend_session(*, backend: str,
                                      backend_session_id: str,
                                      session: str) -> None:
        if not backend_session_id:
            return
        owner = agents_db.get_by_backend_session(backend_session_id)
        if not owner or owner["session"] == session:
            return
        label = backends.label(backend)
        raise AgentLifecycleError(
            409, "session_in_use",
            message=(f"{label} session '{backend_session_id}' is already used by "
                     f"{owner['session']}."),
            extra={"owner": owner["session"]},
        )


def _existing_cwd(raw) -> str:
    cwd = os.path.expanduser(str(raw or "").strip() or str(pathlib.Path.home()))
    candidate = pathlib.Path(cwd)
    if os.environ.get("CLARP_DEPLOYMENT_MODE") == "container":
        from .launch_paths import validate_workspace_path, workspace_root
        try:
            candidate = validate_workspace_path(candidate)
        except ValueError as exc:
            raise AgentLifecycleError(
                403, "workspace_path_forbidden", message=str(exc)) from exc
        fallback = workspace_root() or pathlib.Path.home()
    else:
        fallback = pathlib.Path.home()
    return str(candidate) if candidate.is_dir() else str(fallback)
