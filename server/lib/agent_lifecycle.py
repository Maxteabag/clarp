"""Application service for creating, relaunching, forking, and deleting agents.

Request validation and defaulting are ``policies.agent_spec.AgentSpec.parse``;
this module gathers the roster view, checks the collisions only the database
can see, persists the agent and announces it.
"""
from __future__ import annotations

import os
import pathlib
import secrets
import tempfile
import threading
from dataclasses import dataclass, replace

from . import agents as agents_db
from . import backends, events, identity
from .agent_store import AGENT_ROSTER, load_agents, save_agents
from .fork import fork_session
from .log import log, log_exception
from .mcp_selection import encode as encode_mcp_selection
from .policies.agent_spec import AgentSpec, RosterView, SpecError
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

    def create_janitor(self, data: dict) -> AgentLifecycleResult:
        """Private creation path for the dedicated Janitor configuration API.

        Uses the same identity/runtime service, but does not reserve a voice or
        announce a ready chat. The caller configures the paused Janitor before
        publishing its roster event. A JSON field on ordinary create cannot
        enable this behavior.
        """
        if data.get("replace_sid"):
            raise AgentLifecycleError(400, "Use Janitor configure for an existing agent")
        with self._create_lock:
            return self._create_locked(data, janitor=True)

    def _create_locked(self, data: dict, *, janitor: bool = False) -> AgentLifecycleResult:
        """Parse the request, check the IO-backed collisions, persist, announce."""
        agents = load_agents(self.ctx.agents_path)
        parsed = AgentSpec.parse(
            data, backends=backends, roster=self._roster_view(agents),
            personas=persona_store, janitor=janitor)
        if isinstance(parsed, SpecError):
            raise AgentLifecycleError(parsed.status, parsed.message,
                                      message=parsed.detail or None,
                                      extra=dict(parsed.extra))
        if parsed.reopen is not None:
            owner = parsed.reopen
            return AgentLifecycleResult(
                owner["session"], owner["persona"], owner["voice_id"], parsed.backend)
        if parsed.recommendation:
            log(parsed.recommendation)
        spec = parsed if parsed.session else replace(
            parsed, session=self._mint_session(parsed.persona))
        resume_session_id = self._fork_or_resume(spec)
        self._reject_owned_backend_session(
            backend=spec.backend, backend_session_id=resume_session_id,
            session=spec.session)
        agent = self._persist(spec, agents, resume_session_id)
        return self._announce(spec, agent)

    @staticmethod
    def _roster_view(agents: dict) -> RosterView:
        from . import config as app_config
        from .contact_assignment import available_contacts
        from .launch_paths import recover_user_path
        from .session_models import launch_default, recorded_model
        cfg = app_config.load()
        return RosterView(
            agents=agents,
            occupied_personas=frozenset(
                str(a["persona"]).casefold() for a in agents_db.list_agents()),
            existing_agent=lambda session: agents_db.get_by_session(session) or {},
            session_exists=agents_db.session_exists,
            contact_pool=lambda backend: [c["name"] for c in available_contacts(backend)],
            resume_owner=agents_db.get_by_backend_session,
            resume_listed=lambda backend, cwd, sid: any(
                item.get("id") == sid
                for item in backends.list_sessions(backend, cwd, limit=100)),
            existing_cwd=_existing_cwd,
            recover_user_path=lambda raw: str(recover_user_path(raw)),
            launch_default=lambda backend: launch_default(backend, cfg),
            recorded_model=recorded_model,
            default_model=lambda backend: backends.default_model_effort(backend, cfg)[0],
            global_mcp_servers=app_config.read_global_mcp_servers(),
            cartesia_voice_for=cfg.cartesia_voice_for,
            default_roster_voice=next(iter(AGENT_ROSTER.values())),
            resolve_agent=lambda raw: getattr(identity.resolve(raw), "agent_id", ""),
            ancestors=agents_db.ancestors,
        )

    @staticmethod
    def _fork_or_resume(spec: AgentSpec) -> str:
        if not spec.fork_id:
            return spec.resume_session_id
        try:
            resumed = fork_session(spec.fork_id, spec.cwd)
            log("forkOk", f"{spec.fork_id} -> {resumed} in {spec.cwd}")
            return resumed
        except FileNotFoundError as e:
            log_exception("forkSourceMissing", e, detail=spec.fork_id)
            raise AgentLifecycleError(404, "fork source not found") from e
        except OSError as e:
            log_exception("forkIoFail", e, detail=spec.fork_id)
            raise AgentLifecycleError(500, "fork io failed") from e

    def _persist(self, spec: AgentSpec, agents: dict, resume_session_id: str) -> dict:
        session = spec.session
        avatar_temp = None
        if spec.avatar_raw is not None:
            from .deployment import LAYOUT
            avatar_dir = LAYOUT.data_root / "avatars"
            avatar_dir.mkdir(parents=True, exist_ok=True)
            handle = tempfile.NamedTemporaryFile(
                dir=avatar_dir, prefix=".avatar-", suffix=".jpg", delete=False)
            handle.write(spec.avatar_raw)
            handle.close()
            avatar_temp = pathlib.Path(handle.name)
        agents[session] = {
            "name": spec.persona, "voice_id": spec.voice_id, "cwd": spec.cwd,
            "backend": spec.backend,
        }
        if spec.janitor:
            agents_db.create_agent(
                persona=spec.persona, voice_id="", cwd=spec.cwd, session=session,
                backend=spec.backend, model=spec.model, effort=spec.effort,
                **({"creation_request_id": spec.creation_request_id}
                   if spec.creation_request_id else {}))
        else:
            save_agents(agents, self.ctx.agents_path)
        agent = agents_db.get_by_session(session)
        if not agent:
            raise AgentLifecycleError(500, "agent persistence failed")
        if spec.llm_update:
            agents_db.update_agent(agent["agent_id"], **spec.llm_update)
        if spec.mcp_servers is not None:
            agents_db.update_agent(
                agent["agent_id"],
                mcp_servers=encode_mcp_selection(list(spec.mcp_servers)),
            )
        if spec.presentation_update:
            agents_db.update_agent(agent["agent_id"], **spec.presentation_update)
        if avatar_temp:
            try:
                from .deployment import LAYOUT
                avatar_dir = LAYOUT.data_root / "avatars"
                path = avatar_dir / f"{agent['agent_id']}.jpg"
                avatar_temp.replace(path)
                agents_db.update_agent(agent["agent_id"], avatar_path=str(path))
            except OSError as exc:
                raise AgentLifecycleError(400, "invalid avatar", message=str(exc)) from exc
        if spec.write_lineage:
            try:
                agents_db.set_lineage(agent["agent_id"],
                                      parent_agent_id=spec.parent_agent_id or None,
                                      role=spec.role)
            except agents_db.ParentRefused as exc:
                raise AgentLifecycleError(409, exc.code) from exc
            if spec.fork_id and spec.parent_agent_id:
                log("forkParent", f"{session} <- {spec.parent_agent_id}")
        agents_db.start_runtime(agent["agent_id"], session)
        if resume_session_id:
            try:
                agents_db.bind_backend_session(agent["agent_id"], resume_session_id)
            except agents_db.SessionAlreadyBound as e:
                agents_db.end_current_runtime(agent["agent_id"])
                log_exception("agentResumeBindConflict", e, detail=session)
                raise AgentLifecycleError(409, "session in use") from e
        agents_db.record_path_usage(spec.cwd)
        return agent

    def _announce(self, spec: AgentSpec, agent: dict) -> AgentLifecycleResult:
        announcement = (
            f"{spec.persona} relaunched." if spec.replace_sid
            else f"{spec.persona} forked and ready." if spec.fork_id
            else f"{spec.persona} is ready."
        )
        if spec.synthesize_audio:
            # Pass session so the announcement engine can resolve the agent's
            # persona -> Cartesia voice (else it falls back to ElevenLabs).
            self.ctx.speak_announcement(announcement, spec.voice_id, session=spec.session)
        if spec.janitor:
            return AgentLifecycleResult(spec.session, spec.persona, "", spec.backend)
        events.broadcast(self.ctx.stream, events.agent_roster(
            "relaunched" if spec.replace_sid else ("forked" if spec.fork_id else "created"),
            session=spec.session,
            persona=spec.persona,
            voice_id=spec.voice_id,
            backend=spec.backend,
        ))
        return AgentLifecycleResult(spec.session, spec.persona, spec.voice_id, spec.backend)

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
        if not agent:
            raise AgentLifecycleError(
                404, "agent_not_found", message=f"No active agent session: {session}")
        if not agents_db.interaction_capabilities(agent)["can_restart"]:
            raise AgentLifecycleError(409, "janitor_managed",
                message="Remove Janitors from their maintenance configuration")
        runtime_client = getattr(self.ctx, "runtime_client", None)
        if runtime_client is not None:
            runtime_client.release_agent(agent["agent_id"])
        else:
            backends.interrupt_any(agent["agent_id"])
            agents_db.soft_delete(agent["agent_id"])
        if agents_db.get_focus() == agent["agent_id"]:
            agents_db.set_focus(None)
        events.broadcast(self.ctx.stream, events.agent_roster("deleted", session=session))

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
