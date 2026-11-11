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


def test_options_and_auto_mint_when_pool_empty_and_choose_allows_cross_tier(monkeypatch):
    make_agent()
    agents.create_agent(persona="Occupied Friend", voice_id="{}", cwd="/tmp", session="other")
    definitions = [{"name": "Occupied Friend", "voice_id": "{}"},
                   {"name": "Wrong Backend", "voice_id": "{}", "tier": "claude"}]
    monkeypatch.setattr(personas, "list_all", lambda: definitions)
    assert assign_contact("anon", "options")["contacts"] == []
    # Occupied contacts remain refused on explicit choose.
    with pytest.raises(AgentLifecycleError):
        assign_contact("anon", "choose", "Occupied Friend")
    # Any unoccupied contact can be chosen regardless of recommended tier.
    chosen = assign_contact("anon", "choose", "Wrong Backend")
    assert chosen["name"] == "Wrong Backend"
    assert agents.get_by_session("anon")["persona"] == "Wrong Backend"

    # When no tier-compatible contact is available, auto mints a fresh Contact immediately.
    make_agent(name="Codex-b456", session="anon2")
    minted = assign_contact("anon2", "auto")
    assert minted["name"] == "Soren"
    assert personas.get("Soren") is not None
    assert agents.get_by_session("anon2")["persona"] == "Soren"


def test_create_and_assign_is_durable_auto_assigns_voice_and_keeps_session():
    make_agent()
    before = agents.get_by_session("anon")
    result = assign_contact("anon", "create", "  Brand New Contact  ")
    assert result["name"] == "Brand New Contact"
    created = personas.get("Brand New Contact")
    assert created is not None
    assert '"cartesia":' in created["voice_id"]
    assert agents.get_by_session("anon")["agent_id"] == before["agent_id"]
    with pytest.raises(AgentLifecycleError):
        assign_contact("anon", "create", "Brand New Contact")


def test_available_contacts_excludes_voices_in_use_by_other_live_agents(monkeypatch):
    make_agent(name="Codex-a123", session="anon")
    agents.create_agent(
        persona="Live Speaker",
        voice_id='{"cartesia":"shared-voice"}',
        cwd="/tmp",
        session="speaker",
        backend="codex",
    )
    definitions = [
        {"name": "Shared Voice Contact", "voice_id": '{"cartesia":"shared-voice"}', "tier": "codex"},
        {"name": "Unique Voice Contact", "voice_id": '{"cartesia":"unique-voice"}', "tier": "codex"},
    ]
    monkeypatch.setattr(personas, "list_all", lambda: definitions)
    assert assign_contact("anon", "options")["contacts"] == [{"name": "Unique Voice Contact"}]
    assert assign_contact("anon", "auto")["name"] == "Unique Voice Contact"
