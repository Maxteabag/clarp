"""How an agent create / relaunch / fork request is parsed.

``agent_lifecycle.AgentLifecycleService._create_locked`` used to interleave
request validation, defaulting and persistence. The validation and defaulting
now live here: ``AgentSpec.parse`` takes the request payload plus a view of the
roster and returns either a fully defaulted ``AgentSpec`` or the first
``SpecError`` the request earns, in the order the service always applied them.

The parser performs no IO. The three collaborators are lookups the caller
provides:

* ``backends``  the backend catalogue (``lib.backends`` or a fake): ``normalize``,
  ``get``, ``is_valid_model``, ``valid_efforts``, ``adapter_for``, ``label``,
  ``capabilities``.
* ``roster``    a ``RosterView``: the persisted roster file as a value plus the
  few lookups that need the resolved backend or the filesystem.
* ``personas``  ``get(name)`` -> persona definition or ``None``.

The request's backend is normalised exactly once; stored backends are
normalised where they are compared with it.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
import secrets
from typing import Any, Callable, Collection, Mapping, Sequence

from .. import roster as contact_roster
from ..voice import CARTESIA, resolve_voice

ANONYMOUS_LABELS = {
    "codex": "Codex", "claude": "Claude", "grok": "Grok", "agy": "AGY",
    "opencode": "OpenCode", "deepseek": "DeepSeek",
}
AVATAR_MAX_BYTES = 512_000


def _no_agent(_session: str) -> Mapping:
    return {}


def _keep(raw: object) -> str:
    return str(raw or "")


@dataclass(frozen=True)
class RosterView:
    """The roster and launch environment as the parser needs them."""
    agents: Mapping[str, Mapping] = field(default_factory=dict)
    occupied_personas: frozenset[str] = frozenset()     # casefolded live persona names
    existing_agent: Callable[[str], Mapping] = _no_agent  # session -> agents row or {}
    session_exists: Callable[[str], bool] = lambda _s: False
    contact_pool: Callable[[str], Sequence[str]] = lambda _b: ()
    resume_owner: Callable[[str], Mapping | None] = lambda _sid: None
    resume_listed: Callable[[str, str, str], bool] = lambda _b, _cwd, _sid: False
    existing_cwd: Callable[[object], str] = _keep
    recover_user_path: Callable[[str], str] = _keep
    launch_default: Callable[[str], str] = lambda _b: ""
    recorded_model: Callable[[str, str], str] = lambda _b, _sid: ""
    default_model: Callable[[str], str] = lambda _b: ""
    global_mcp_servers: Collection[str] = ()
    cartesia_voice_for: Callable[[str], str | None] = lambda _p: None
    default_roster_voice: str = ""
    random_suffix: Callable[[], str] = lambda: secrets.token_hex(2)


@dataclass(frozen=True)
class SpecError:
    status: int
    message: str                      # the error code the API returns
    detail: str = ""                  # human-readable message when it differs
    extra: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentSpec:
    backend: str
    reopen: Mapping | None = None     # an owner already holds this native session
    persona: str = ""
    voice_id: str = ""
    cwd: str = ""
    session: str = ""                 # usable explicit id; "" means mint one
    replace_sid: str = ""
    fork_id: str = ""
    resume_session_id: str = ""
    janitor: bool = False
    synthesize_audio: bool = True
    avatar_raw: bytes | None = None
    model: str = ""                   # effective model to record on create
    effort: str = ""
    llm_update: Mapping[str, str] = field(default_factory=dict)
    mcp_servers: tuple[str, ...] | None = None
    presentation_update: Mapping[str, str] = field(default_factory=dict)
    recommendation: str = ""          # log line when the tier recommends another backend
    creation_request_id: str = ""

    @staticmethod
    def parse(data: Mapping, *, backends, roster: RosterView, personas,
              janitor: bool = False) -> "AgentSpec | SpecError":
        return _parse(dict(data), backends=backends, roster=roster,
                      personas=personas, janitor=janitor)


def _parse(data: dict, *, backends, roster: RosterView, personas,
           janitor: bool) -> AgentSpec | SpecError:
    replace_sid = (data.get("replace_sid") or "").strip()
    current = roster.agents.get(replace_sid) if replace_sid else None
    # A relaunch inherits the agent's backend the way it inherits voice and cwd.
    backend = backends.normalize(
        data.get("backend") or ((current or {}).get("backend") if current else None))

    if data.get("open_existing") is True and data.get("resume_session_id") and not janitor:
        sid = str(data["resume_session_id"]).strip()
        owner = roster.resume_owner(sid)
        if owner and backends.normalize(owner.get("backend")) == backend:
            return AgentSpec(backend=backend, reopen=owner)
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
    # persona-derived id like "bella" on every create. Honoring it verbatim
    # would collide with the soft-deleted old "bella" row and resurrect it.
    # So: honor an explicit id only when it is genuinely unused; otherwise the
    # service mints a unique `<persona>-<hex>` id. Deliberate resurrection
    # still happens via replace_sid (relaunch) below.
    explicit_session = "".join(
        c for c in (data.get("session") or "").strip()
        if c.isalnum() or c in "._-")
    if janitor and explicit_session and roster.session_exists(explicit_session):
        return SpecError(409, "session_taken",
                         detail="The reserved Janitor identity is already in use")
    session = explicit_session if (
        explicit_session and not roster.session_exists(explicit_session)) else ""
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
        # voice and backend. Without this an omitted cwd falls back to $HOME:
        # on a host that silently wakes the agent up outside its repo, and in
        # a container it is rejected as outside the workspace root.
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
    # recommendation the clients surface, not a rule: the owner may run
    # Rachel on Codex or Cipher on Claude (requested 2026-09-20).
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
        return SpecError(
            400, "fork_unsupported",
            detail=f"{backends.label(backend)} does not support session forks.")

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
        backend=backend,
        persona=persona,
        voice_id=voice_id,
        cwd=cwd,
        session=session,
        replace_sid=replace_sid,
        fork_id=fork_id,
        resume_session_id=resume_session_id,
        janitor=janitor,
        synthesize_audio=synthesize_audio,
        avatar_raw=avatar_raw,
        model=effective_model,
        effort=effective_effort,
        llm_update=llm_update,
        mcp_servers=mcp_servers,
        presentation_update=presentation_update,
        recommendation=recommendation,
        creation_request_id=str(data["creation_request_id"]) if data.get("creation_request_id") else "",
    )
