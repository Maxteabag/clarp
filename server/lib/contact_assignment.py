"""Assign a durable contact without replacing an agent or its conversation."""
from . import agents, contact_minting, db, persona_avatar_generation, personas, roster
from .agent_lifecycle import AgentLifecycleService, AgentLifecycleError
from .voice import CARTESIA, resolve_voice


def available_contacts(backend, exclude_agent_id=""):
    others = [a for a in agents.list_agents() if a["agent_id"] != exclude_agent_id]
    occupied = {str(a["persona"]).strip().casefold() for a in others}
    voices = {
        v for a in others
        if (v := resolve_voice(a.get("voice_id") or "", CARTESIA))
    }
    eligible = [
        p for p in personas.list_all()
        if p["name"].casefold() not in occupied
        and roster.validate_contact_backend(p["name"], backend, p.get("tier", ""))[0]
        and not ((v := resolve_voice(p.get("voice_id") or "", CARTESIA)) and v in voices)
    ]
    return eligible


def assign_contact(session, mode="auto", name=""):
    if mode not in {"auto", "choose", "create", "options"}:
        raise AgentLifecycleError(400, "invalid assignment mode")
    with AgentLifecycleService._create_lock:
        definitions = personas.list_all()
        agent = agents.get_by_session(session)
        if not agent:
            raise AgentLifecycleError(404, "no such agent")
        if not agents.interaction_capabilities(agent)["can_restart"]:
            raise AgentLifecycleError(409, "janitor_managed")
        others = [a for a in agents.list_agents() if a["agent_id"] != agent["agent_id"]]
        occupied = {str(a["persona"]).strip().casefold() for a in others}
        choices = available_contacts(agent["backend"], agent["agent_id"])

        if mode == "options":
            return {"ok": True, "session": session, "contacts": [{"name": p["name"]} for p in choices]}
        name = " ".join(str(name or "").split())
        needs_avatar = False
        if mode == "auto":
            # Repeated automatic assignment keeps an already assigned contact;
            # when the compatible pool is exhausted, mint a fresh Contact immediately.
            contact = next((p for p in choices if p["name"] == agent["persona"]), None)
            contact = contact or next(iter(choices), None)
            if not contact:
                contact = contact_minting.mint_contact(
                    agent["backend"], occupied_names=occupied, generate_avatar=False)
                needs_avatar = True
        elif mode == "choose":
            contact = next((p for p in definitions if p["name"].casefold() == name.casefold()), None)
            if not contact:
                raise AgentLifecycleError(404, "contact not found")
            if contact["name"].casefold() in occupied:
                raise AgentLifecycleError(
                    409, "contact_unavailable",
                    message="This contact already has an active session.")
        else:
            if not name or len(name) > 60:
                raise AgentLifecycleError(400, "name required (maximum 60 characters)")
            if name.casefold() in occupied or personas.get(name):
                raise AgentLifecycleError(409, "contact already exists")
            contact = None
            needs_avatar = True
        connection = db.conn()
        connection.execute("BEGIN IMMEDIATE")
        try:
            if contact is None:
                contact = personas.create(name=name, voice_id="")
            agents.update_agent(agent["agent_id"], persona=contact["name"],
                voice_id=contact.get("voice_id") or "{}", personality=contact.get("personality") or "",
                avatar_symbol=contact.get("avatar_symbol") or "", avatar_path=contact.get("avatar_path") or "")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    if needs_avatar:
        persona_avatar_generation.start_best_effort(
            contact["name"], backend=agent["backend"])
    return {"ok": True, "session": session, "name": contact["name"], "agent_id": agent["agent_id"]}
