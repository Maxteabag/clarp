from lib import agents, db, personas


def test_custom_persona_survives_session_deletion(tmp_path):
    row = personas.create(
        name="Nova", voice_id='{"cartesia":"voice-1"}',
        avatar_symbol="sparkles", personality="Warm and direct.")
    agent_id = agents.create_agent(
        persona="Nova", voice_id=row["voice_id"], cwd=str(tmp_path),
        session="nova-session")
    agents.soft_delete(agent_id)

    assert agents.get_by_agent_id(agent_id) is None
    assert personas.get("Nova")["avatar_symbol"] == "sparkles"


def test_custom_contact_removal_preserves_active_session():
    personas.create(name="Nova", voice_id='{"cartesia":"voice-1"}')
    agent_id = agents.create_agent(
        persona="Nova", voice_id="", cwd="/tmp", session="nova")

    assert personas.delete("Nova") is True
    assert personas.get("Nova") is None
    assert agents.get_by_agent_id(agent_id)["session"] == "nova"


def test_builtin_contact_cannot_be_removed():
    personas.ensure_builtins()

    assert personas.delete("Mike") is False
    assert personas.get("Mike")["builtin"] == 1


def test_contact_avatar_is_retained_while_agent_references_it(tmp_path):
    avatar = tmp_path / "nova.jpg"
    avatar.write_bytes(b"avatar")
    personas.create(name="Nova", voice_id='{"cartesia":"voice-1"}')
    persona = personas.get("Nova")
    agent_id = agents.create_agent(
        persona="Nova", voice_id="", cwd="/tmp", session="nova")
    agents.update_agent(agent_id, avatar_path=str(avatar))
    db.conn().execute(
        "UPDATE personas SET avatar_path=? WHERE persona_id=?",
        (str(avatar), persona["persona_id"]),)

    assert personas.delete("Nova") is True
    assert avatar.read_bytes() == b"avatar"


def test_custom_contact_can_be_edited_without_touching_its_chat(tmp_path):
    personas.create(
        name="Nova", voice_id='{"deepgram":"flux-haley-en"}',
        personality="Original")
    agent_id = agents.create_agent(
        persona="Nova", voice_id='{"deepgram":"flux-haley-en"}',
        cwd=str(tmp_path), session="nova-chat")

    updated = personas.update(
        original_name="Nova", name="Nova Prime",
        voice_id='{"deepgram":"flux-hannah-en"}',
        personality="Edited and calm")

    assert updated["name"] == "Nova Prime"
    assert updated["personality"] == "Edited and calm"
    assert personas.get("Nova") is None
    assert agents.get_by_agent_id(agent_id)["session"] == "nova-chat"
    assert agents.get_by_agent_id(agent_id)["persona"] == "Nova"


def test_builtin_contact_cannot_be_edited_in_server_store():
    personas.ensure_builtins()

    try:
        personas.update(
            original_name="Mike", name="Mike",
            voice_id='{"deepgram":"flux-haley-en"}')
    except ValueError as error:
        assert "custom Contacts" in str(error)
    else:
        raise AssertionError("built-in server Contact edit should be rejected")
