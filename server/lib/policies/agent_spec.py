"""How an agent create / relaunch / fork request is parsed.

``agent_lifecycle.AgentLifecycleService._create_locked`` used to interleave
request validation, defaulting and persistence over ~340 lines. The validation
and defaulting now live here: names, anonymous launch, the contact pool, the
voice fallback, model and effort, the working directory, MCP selection, and
the collision checks that only need the roster values. The service gathers a
``RosterView``, calls ``AgentSpec.parse`` and then does the IO: mint a session
id, fork, refuse an owned backend session, persist, announce.

``parse`` returns either an ``AgentSpec`` (everything the service needs to
persist) or a ``SpecError`` whose ``status``/``message`` become the
``AgentLifecycleError``. The requested backend is normalised exactly once.
No IO happens in this module: every lookup that needs the disk or the database
is a callable on the ``RosterView`` the caller supplies, and the tests hand in
plain lambdas.
"""
from __future__ import annotations

import base64
import secrets
from dataclasses import dataclass, field
from typing import Callable, Collection, Mapping, Sequence

from .. import roster as contact_roster
from ..voice import CARTESIA, resolve_voice
from . import helper_state as helper_policy

AVATAR_MAX_BYTES = 512_000
ANONYMOUS_LABELS = {
    "codex": "Codex", "claude": "Claude", "grok": "Grok", "agy": "AGY",
    "opencode": "OpenCode", "deepseek": "DeepSeek",
}


def _no_row(_session: str) -> Mapping:
    return {}


@dataclass(frozen=True)
class RosterView:
    """The live roster and launch environment as values and lookups."""
    agents: Mapping[str, Mapping] = field(default_factory=dict)  # persisted roster file
    occupied_personas: frozenset[str] = frozenset()  # casefolded live persona names
    existing_agent: Callable[[str], Mapping] = _no_row  # session -> agents row or {}
    session_exists: Callable[[str], bool] = lambda _session: False
    contact_pool: Callable[[str], Sequence[str]] = lambda _backend: ()
    resume_owner: Callable[[str], Mapping | None] = lambda _sid: None
    resume_listed: Callable[[str, str, str], bool] = lambda _backend, _cwd, _sid: False
    existing_cwd: Callable[[object], str] = lambda raw: str(raw or "")
    recover_user_path: Callable[[str], str] = lambda raw: raw
    launch_default: Callable[[str], str] = lambda _backend: ""
    recorded_model: Callable[[str, str], str] = lambda _backend, _sid: ""
    default_model: Callable[[str], str] = lambda _backend: ""
    global_mcp_servers: Collection[str] = ()
    cartesia_voice_for: Callable[[str], str | None] = lambda _persona: None
    default_roster_voice: str = ""
    random_suffix: Callable[[], str] = lambda: secrets.token_hex(2)
    resolve_agent: Callable[[str], str] = lambda _raw: ""  # session or id -> agent_id or ""
    ancestors: Callable[[str], Sequence[str]] = lambda _agent_id: ()


@dataclass(frozen=True)
class SpecError:
    status: int
    message: str                      # the AgentLifecycleError code
    detail: str = ""                  # human message when it differs from the code
    extra: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentSpec:
    persona: str = ""
    voice_id: str = ""
    backend: str = ""
    cwd: str = ""
    session: str = ""                 # usable explicit id; "" means mint one
    replace_sid: str = ""
    fork_id: str = ""
    resume_session_id: str = ""
    janitor: bool = False
    synthesize_audio: bool = True
    avatar_raw: bytes | None = None
    persona_definition: Mapping | None = None
    model: str = ""                   # effective requested model
    effort: str = ""                  # effective requested effort
    llm_update: Mapping[str, str] = field(default_factory=dict)
    mcp_servers: tuple[str, ...] | None = None
    presentation_update: Mapping[str, str] = field(default_factory=dict)
    recommendation: str = ""          # log line when the tier recommends another backend
    creation_request_id: str = ""
    reopen: Mapping | None = None     # an owner to return without creating anything
    parent_agent_id: str = ""         # the creator: an explicit parent or the fork source
    role: str = helper_policy.Role.AGENT
    write_lineage: bool = False       # persist parent_agent_id and role

    @staticmethod
    def parse(data: Mapping, *, backends, roster: RosterView, personas,
              janitor: bool = False) -> "AgentSpec | SpecError":
        return _parse(dict(data), backends=backends, roster=roster,
                      personas=personas, janitor=janitor)


def _parse(data: dict, *, backends, roster: RosterView, personas,
           janitor: bool) -> AgentSpec | SpecError:
    replace_sid = (data.get("replace_sid") or "").strip()
    current = roster.agents.get(replace_sid) if replace_sid else None
    # A relaunch inherits the backend the way it inherits voice and cwd.
    backend = backends.normalize(data.get("backend") or (current or {}).get("backend"))

    if data.get("open_existing") is True and data.get("resume_session_id") and not janitor:
        sid = str(data["resume_session_id"]).strip()
        owner = roster.resume_owner(sid)
        if owner and backends.normalize(owner.get("backend")) == backend:
            return AgentSpec(reopen=owner, backend=backend)
        if not backends.get(backend).resumable:
            return SpecError(400, "resume_unsupported")
        cwd = roster.recover_user_path(str(data.get("cwd") or "~"))
        if not roster.resume_listed(backend, cwd, sid):
            return SpecError(404, "Session is no longer available in this directory")

    if data.get("auto_contact") is True and not janitor:
        if data.get("anonymous") or data.get("replace_sid"):
            return SpecError(400, "conflicting launch options")
        choices = roster.contact_pool(backend)
        if not choices:
            return SpecError(409, "contact_pool_empty")
        data["name"] = choices[0]

    if data.get("anonymous") is True and not janitor:
        if data.get("replace_sid"):
            return SpecError(400, "anonymous launch must be fresh")
        label = ANONYMOUS_LABELS.get(backend, backend)
        while True:
            name = f"{label}-{roster.random_suffix()}"
            if name.casefold() not in roster.occupied_personas and not personas.get(name):
                break
        data.update(name=name, voice_id="{}", personality="", avatar_symbol="")
        data.pop("session", None)

    persona = (data.get("name") or "").strip()
    if not persona:
        return SpecError(400, "name required")
    voice_id = "" if janitor else (data.get("voice_id") or "").strip()
    persona_definition = personas.get(persona)
    avatar_raw = None
    avatar_data = str(data.get("avatar_base64") or "").strip()
    if avatar_data:
        try:
            raw = base64.b64decode(avatar_data, validate=True)
            if len(raw) > AVATAR_MAX_BYTES:
                raise ValueError("avatar is too large")
            avatar_raw = raw
        except (ValueError, OSError) as exc:
            return SpecError(400, "invalid avatar", detail=str(exc))

    # Session id selection. The client (incl. the native app) often re-sends a
    # persona-derived id like "bella" on every create. Honoring that verbatim
    # would collide with the soft-deleted old "bella" row and RESURRECT it. So
    # honor an explicit id only when it's genuinely unused; otherwise the
    # service mints a unique `<persona>-<hex>` id. Deliberate resurrection
    # still happens via replace_sid (relaunch) below.
    explicit_session = "".join(
        c for c in (data.get("session") or "").strip()
        if c.isalnum() or c in "._-")
    if janitor and explicit_session and roster.session_exists(explicit_session):
        return SpecError(409, "session_taken",
                         detail="The reserved Janitor identity is already in use")
    session = explicit_session if explicit_session and not roster.session_exists(
        explicit_session) else ""
    cwd = roster.existing_cwd(data.get("cwd"))
    fork_id = (data.get("fork_session_id") or "").strip()
    synthesize_audio = (not janitor and data.get("anonymous") is not True
                        and data.get("synthesize_audio", True) is not False)
    agents = roster.agents
    clear_retained_model = False
    clear_retained_effort = False
    retained_model = ""
    retained_effort = ""
    if replace_sid:
        if current is None:
            return SpecError(404, "no such agent to replace")
        session = replace_sid
        voice_id = voice_id or current.get("voice_id") or voice_id
        persona = persona or current.get("name") or persona
        existing_agent = roster.existing_agent(replace_sid) or {}
        if existing_agent.get("is_janitor"):
            return SpecError(409, "janitor_managed",
                             detail="Janitors are managed from their maintenance configuration")
        # A relaunch inherits the agent's directory the same way it inherits
        # voice and backend. Without this an omitted cwd falls back to $HOME.
        if not str(data.get("cwd") or "").strip():
            cwd = roster.existing_cwd(current.get("cwd") or existing_agent.get("cwd"))
        previous_backend = backends.normalize(existing_agent.get("backend"))
        retained_model = str(existing_agent.get("model") or "").strip()
        retained_effort = str(existing_agent.get("effort") or "").strip()
        if backend != previous_backend:
            clear_retained_model = (
                "model" not in data
                and not backends.is_valid_model(backend, retained_model))
            clear_retained_effort = (
                "effort" not in data and bool(retained_effort)
                and retained_effort not in backends.valid_efforts(backend))

    # A contact's tier names the backend it was designed for. That is a
    # recommendation the clients surface, not a rule (requested 2026-09-20).
    persona_tier = persona_definition.get("tier") if persona_definition else ""
    is_recommended, recommended_backend = contact_roster.validate_contact_backend(
        persona, backend, persona_tier)
    recommendation = ""
    if not is_recommended and recommended_backend:
        recommendation = (
            f"[agents] {persona} starts on '{backend}'; its "
            f"{(persona_tier or contact_roster.tier_for_contact(persona) or 'listed')} tier "
            f"recommends '{recommended_backend}'")

    occupied = next((
        (sid, info) for sid, info in agents.items()
        if sid != replace_sid
        and str((info or {}).get("name") or sid).strip().casefold() == persona.casefold()
    ), None)
    if occupied is not None:
        owner_session, owner_info = occupied
        owner = str((owner_info or {}).get("name") or owner_session)
        return SpecError(
            409, "contact_occupied",
            detail=(f"{owner} already has an active session. "
                    "Open or release that chat before starting another."),
            extra={"owner": owner, "session": owner_session})
    if not replace_sid and session in agents:
        owner = (agents[session] or {}).get("name") or session
        return SpecError(
            409, "session_taken",
            detail=(f"Cannot start {persona}: session '{session}' is already "
                    f"used by {owner}. Stop {owner} first or pick a different session id."),
            extra={"owner": owner, "session": session})

    if "model" in data and data.get("model") is not None \
            and not isinstance(data.get("model"), str):
        return SpecError(400, "model must be a string or null")
    requested_model = (data.get("model") or "").strip() if "model" in data else ""
    if "model" in data and not backends.is_valid_model(backend, requested_model):
        return SpecError(400, "invalid model for backend")
    if "effort" in data and data.get("effort") is not None \
            and not isinstance(data.get("effort"), str):
        return SpecError(400, "effort must be a string or null")
    requested_effort = ((data.get("effort") or "").strip().lower()
                        if "effort" in data else "")
    if ("effort" in data and requested_effort
            and requested_effort not in backends.valid_efforts(backend)):
        return SpecError(400, "invalid effort for backend")
    effective_model = (
        requested_model if "model" in data
        else ("" if clear_retained_model else retained_model))
    effective_effort = (
        requested_effort if "effort" in data
        else ("" if clear_retained_effort else retained_effort))
    if not effective_model and not janitor:
        resume_id = str(data.get("resume_session_id") or data.get("fork_session_id") or "")
        effective_model = (roster.recorded_model(backend, resume_id) if resume_id
                           else roster.launch_default(backend))
    mcp_servers: tuple[str, ...] | None = None
    if "mcp_servers" in data:
        raw_mcp = data.get("mcp_servers")
        if not isinstance(raw_mcp, list) or any(
                not isinstance(item, str) for item in raw_mcp):
            return SpecError(400, "mcp_servers must be a list of names")
        mcp_servers = tuple(dict.fromkeys(
            item.strip() for item in raw_mcp if item.strip()))
        if mcp_servers and not backends.adapter_for(backend).supports_mcp:
            return SpecError(
                400, "mcp servers unsupported for backend",
                detail=f"MCP server selection is unavailable for {backends.label(backend)}.")
        unknown_mcp = [item for item in mcp_servers if item not in roster.global_mcp_servers]
        if unknown_mcp:
            return SpecError(400, "unknown mcp server",
                             detail=f"Unknown MCP server: {', '.join(unknown_mcp)}")
    effective_validation_model = effective_model
    effort_compatibility_unknown = backends.adapter_for(backend).effort_compatibility_unknown
    if effort_compatibility_unknown:
        effective_validation_model = effective_validation_model or roster.default_model(backend)
    if effort_compatibility_unknown and effective_validation_model and effective_effort:
        return SpecError(400, "AGY model-specific effort compatibility is unknown")
    if not janitor and not voice_id:
        _, roster_voice = contact_roster.lookup_persona(persona)
        voice_id = ((persona_definition or {}).get("voice_id")
                    or roster_voice or roster.default_roster_voice)
    selected_cartesia = "" if janitor else resolve_voice(voice_id, CARTESIA)
    if selected_cartesia:
        for sid, info in agents.items():
            if sid == replace_sid or (info or {}).get("is_janitor"):
                continue
            existing_cartesia = (
                resolve_voice((info or {}).get("voice_id"), CARTESIA)
                or roster.cartesia_voice_for(str((info or {}).get("name") or "")))
            if existing_cartesia == selected_cartesia:
                return SpecError(
                    409, "voice_in_use",
                    detail=f"That voice is already used by {(info or {}).get('name') or sid}.")

    resume_session_id = (data.get("resume_session_id") or "").strip()
    if fork_id and not backends.capabilities(backend).supports_fork:
        return SpecError(400, "fork_unsupported",
                         detail=f"{backends.label(backend)} does not support session forks.")
    lineage = _lineage(data, roster=roster, replace_sid=replace_sid,
                       fork_id=fork_id, janitor=janitor)
    if isinstance(lineage, SpecError):
        return lineage
    parent_agent_id, role, write_lineage = lineage

    # Per-agent model / effort override (create + relaunch). Only fields the
    # client actually sent are touched, so a relaunch that omits them keeps
    # the existing pins.
    llm_update: dict[str, str] = {}
    if "model" in data:
        llm_update["model"] = effective_model
    elif clear_retained_model or (not retained_model and effective_model):
        llm_update["model"] = effective_model
    if "effort" in data:
        llm_update["effort"] = requested_effort
    elif clear_retained_effort:
        llm_update["effort"] = ""
    presentation_update = {
        "avatar_symbol": str(data.get("avatar_symbol")
                             or (persona_definition or {}).get("avatar_symbol") or "").strip()[:64],
        "personality": str(data.get("personality")
                           or (persona_definition or {}).get("personality") or "").strip()[:4000],
        "avatar_path": str((persona_definition or {}).get("avatar_path") or ""),
    }
    if replace_sid:
        presentation_update = {
            key: value for key, value in presentation_update.items() if key in data}
    return AgentSpec(
        persona=persona, voice_id=voice_id, backend=backend, cwd=cwd, session=session,
        replace_sid=replace_sid, fork_id=fork_id, resume_session_id=resume_session_id,
        janitor=janitor, synthesize_audio=synthesize_audio, avatar_raw=avatar_raw,
        persona_definition=persona_definition, model=effective_model,
        effort=effective_effort, llm_update=llm_update, mcp_servers=mcp_servers,
        presentation_update=presentation_update, recommendation=recommendation,
        creation_request_id=str(data.get("creation_request_id") or ""),
        parent_agent_id=parent_agent_id, role=role, write_lineage=write_lineage,
    )


def _lineage(data: dict, *, roster: RosterView, replace_sid: str, fork_id: str,
             janitor: bool) -> tuple[str, str, bool] | SpecError:
    """``(parent_agent_id, role, write)`` for a create, relaunch or fork.

    ``parent`` names the creator by session or agent id. A fork without one
    records its source agent as the parent. A helper must have a parent. A
    relaunch keeps its lineage unless the request names a parent or role.
    """
    if janitor:
        return "", helper_policy.Role.JANITOR, False
    existing = roster.existing_agent(replace_sid) if replace_sid else {}
    raw_parent = str(data.get("parent") or "").strip()
    has_role = bool(str(data.get("role") or "").strip())
    role = helper_policy.parse_role(data.get("role") if has_role else existing.get("role"))
    if role is None:
        return SpecError(400, "invalid_role",
                         detail="role must be 'agent' or 'helper'")
    parent_id = ""
    if raw_parent:
        parent_id = roster.resolve_agent(raw_parent)
        if not parent_id:
            return SpecError(404, "parent_not_found",
                             detail=f"No live agent matches parent '{raw_parent}'.")
    elif fork_id:
        owner = roster.resume_owner(fork_id) or {}
        parent_id = str(owner.get("agent_id") or "")
    elif replace_sid:
        parent_id = str(existing.get("parent_agent_id") or "")
    if role == helper_policy.Role.HELPER and not parent_id:
        return SpecError(400, "helper_requires_parent",
                         detail="A helper needs a parent session.")
    child_id = str(existing.get("agent_id") or "")
    refusal = helper_policy.parent_refusal(
        child_id, parent_id, roster.ancestors(parent_id) if parent_id else ())
    if refusal:
        return SpecError(409, refusal, detail=(
            "An agent cannot be its own parent." if refusal == "self_parent"
            else "That parent is already a descendant of this agent."))
    write = not replace_sid or bool(raw_parent) or has_role
    return parent_id, role, write
