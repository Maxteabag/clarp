"""Assign a durable contact without replacing an agent or its conversation."""
from . import agents, personas, roster, db
from .agent_lifecycle import AgentLifecycleService, AgentLifecycleError
from .voice import CARTESIA, resolve_voice


def available_contacts(backend, exclude_agent_id=""):
    others = [a for a in agents.list_agents() if a["agent_id"] != exclude_agent_id]
    occupied = {str(a["persona"]).strip().casefold() for a in others}
    voices = {resolve_voice(a.get("voice_id") or "", CARTESIA) for a in others}
    return [p for p in personas.list_all()
            if p["name"].casefold() not in occupied
            and roster.validate_contact_backend(p["name"], backend, p.get("tier", ""))[0]
            and (not resolve_voice(p.get("voice_id") or "", CARTESIA)
                 or resolve_voice(p.get("voice_id") or "", CARTESIA) not in voices)]


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

        def available(contact):
            return any(p["name"] == contact["name"] for p in choices)

        if mode == "options":
            return {"ok": True, "session": session, "contacts": [{"name": p["name"]} for p in choices]}
        name = " ".join(str(name or "").split())
        if mode == "auto":
            # Repeated automatic assignment keeps an already assigned contact.
            contact = next((p for p in choices if p["name"] == agent["persona"]), None)
            contact = contact or next(iter(choices), None)
            if not contact:
                raise AgentLifecycleError(409, "contact_pool_empty", message="No compatible contacts are available. Choose Create new contact.")
        elif mode == "choose":
            contact = next((p for p in definitions if p["name"].casefold() == name.casefold()), None)
            if not contact:
                raise AgentLifecycleError(404, "contact not found")
            if not available(contact):
                raise AgentLifecycleError(409, "contact_unavailable", message="This contact is occupied or incompatible with this backend.")
        else:
            if not name or len(name) > 60:
                raise AgentLifecycleError(400, "name required (maximum 60 characters)")
            if name.casefold() in occupied or personas.get(name):
                raise AgentLifecycleError(409, "contact already exists")
            if not roster.validate_contact_backend(name, agent["backend"])[0]:
                raise AgentLifecycleError(409, "contact backend mismatch")
            contact = None
        connection = db.conn()
        connection.execute("BEGIN IMMEDIATE")
        try:
            if contact is None:
                contact = personas.create(name=name, voice_id="{}")
            agents.update_agent(agent["agent_id"], persona=contact["name"],
                voice_id=contact.get("voice_id") or "{}", personality=contact.get("personality") or "",
                avatar_symbol=contact.get("avatar_symbol") or "", avatar_path=contact.get("avatar_path") or "")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        return {"ok": True, "session": session, "name": contact["name"], "agent_id": agent["agent_id"]}
