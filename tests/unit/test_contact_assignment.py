import pytest

from lib import agents, personas
from lib.agent_lifecycle import AgentLifecycleError
from lib.contact_assignment import assign_contact


def make_agent(name="Codex-a123", session="anon"):
    return agents.create_agent(persona=name, voice_id="{}", cwd="/tmp", session=session,
                               backend="codex", model="preserved-model", effort="high")


def test_assign_preserves_identifiers_and_backend_and_copies_contact(monkeypatch):
    make_agent()
    contact = personas.create(name="Test Assign Friend", voice_id="{}", personality="Be concise.", avatar_symbol="F")
    monkeypatch.setattr(personas, "list_all", lambda: [contact])
    before = agents.get_by_session("anon")
    result = assign_contact("anon")
    after = agents.get_by_session("anon")
    assert after["persona"] == contact["name"]
    assert after["personality"] == "Be concise."
    assert after["avatar_symbol"] == "F"
    for field in ("agent_id", "session", "backend", "model", "effort", "cwd", "created_at"):
        assert after[field] == before[field]
    assert result["agent_id"] == before["agent_id"]
    assert assign_contact("anon")["name"] == contact["name"]


def test_options_and_auto_exclude_occupied_and_incompatible_contacts(monkeypatch):
    make_agent()
    agents.create_agent(persona="Occupied Friend", voice_id="{}", cwd="/tmp", session="other")
    definitions = [{"name": "Occupied Friend", "voice_id": "{}"},
                   {"name": "Wrong Backend", "voice_id": "{}", "tier": "claude"}]
    monkeypatch.setattr(personas, "list_all", lambda: definitions)
    assert assign_contact("anon", "options")["contacts"] == []
    with pytest.raises(AgentLifecycleError) as error:
        assign_contact("anon")
    assert error.value.code == "contact_pool_empty"
    assert agents.get_by_session("anon")["persona"] == "Codex-a123"
    with pytest.raises(AgentLifecycleError):
        assign_contact("anon", "choose", "Occupied Friend")
    with pytest.raises(AgentLifecycleError):
        assign_contact("anon", "choose", "Wrong Backend")


def test_create_and_assign_is_durable_and_keeps_session():
    make_agent()
    before = agents.get_by_session("anon")
    result = assign_contact("anon", "create", "  Brand New Contact  ")
    assert result["name"] == "Brand New Contact"
    assert personas.get("Brand New Contact") is not None
    assert agents.get_by_session("anon")["agent_id"] == before["agent_id"]
    with pytest.raises(AgentLifecycleError):
        assign_contact("anon", "create", "Brand New Contact")
