"""Application service for creating, relaunching, forking, and deleting agents."""
from __future__ import annotations

import os
import base64
import pathlib
import secrets
import tempfile
import threading
from dataclasses import dataclass

from . import agents as agents_db
from . import backends
from .agent_store import AGENT_ROSTER, load_agents, save_agents
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


class AgentLifecycleService:
    _create_lock = threading.RLock()

    def __init__(self, ctx):
        self.ctx = ctx

    def create(self, data: dict) -> AgentLifecycleResult:
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
        session = session.strip("/")
        if not session:
            raise AgentLifecycleError(400, "name required")
        agent = agents_db.get_by_session(session)
        if agent:
            backends.interrupt_any(agent["agent_id"])
            agents_db.soft_delete(agent["agent_id"])
            if agents_db.get_focus() == agent["agent_id"]:
                agents_db.set_focus(None)
        self.ctx.stream.broadcast({
            "type": SSEType.AGENT_ROSTER, "kind": "deleted", "session": session,
        })

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
