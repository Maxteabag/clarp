from types import SimpleNamespace

import pytest

from lib import agents as agents_db, config, personas
from lib.agent_lifecycle import AgentLifecycleError, AgentLifecycleService


class _Stream:
    def __init__(self):
        self.events = []

    def broadcast(self, event):
        self.events.append(event)


def _ctx(tmp_path):
    announcements = []
    return SimpleNamespace(
        agents_path=tmp_path / "unused.json",
        stream=_Stream(),
        speak_announcement=lambda text, voice_id, session=None: announcements.append((text, voice_id)),
        announcements=announcements,
    )


def test_lifecycle_service_creates_agent_without_http(tmp_path):
    ctx = _ctx(tmp_path)

    result = AgentLifecycleService(ctx).create({
        "name": "Rachel",
        "session": "rachel",
        "cwd": str(tmp_path),
        "voice_id": "V_RACHEL",
        "backend": "codex",
    })

    assert result.session == "rachel"
    assert result.backend == "codex"
    assert agents_db.get_by_session("rachel")["voice_id"] == "V_RACHEL"
    assert agents_db.current_runtime_id(agents_db.get_by_session("rachel")["agent_id"])
    assert ctx.announcements == [("Rachel is ready.", "V_RACHEL")]
    assert ctx.stream.events[-1]["kind"] == "created"
    assert agents_db.favorite_paths()[0]["path"] == str(tmp_path)


def test_lifecycle_service_applies_validated_mcp_selection_at_creation(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        config, "read_global_mcp_servers", lambda: {"files": {}, "github": {}})
    result = AgentLifecycleService(_ctx(tmp_path)).create({
        "name": "Rachel",
        "session": "rachel",
        "cwd": str(tmp_path),
        "voice_id": "V_RACHEL",
        "mcp_servers": ["github", "files", "github"],
    })
    row = agents_db.get_by_session(result.session)
    assert row["mcp_servers"] == \
        '{"configured": true, "servers": ["github", "files"]}'


def test_lifecycle_service_rejects_unknown_mcp_at_creation(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "read_global_mcp_servers", lambda: {"files": {}})
    with pytest.raises(AgentLifecycleError) as error:
        AgentLifecycleService(_ctx(tmp_path)).create({
            "name": "Rachel",
            "session": "rachel",
            "cwd": str(tmp_path),
            "voice_id": "V_RACHEL",
            "mcp_servers": ["missing"],
        })
    assert error.value.status == 400
    assert error.value.code == "unknown mcp server"
    assert agents_db.get_by_session("rachel") is None


def test_lifecycle_delete_releases_processes_in_external_runtime(tmp_path):
    agent_id = agents_db.create_agent(
        persona="Rachel", voice_id="voice", cwd=str(tmp_path), session="rachel")
    calls = []
    ctx = _ctx(tmp_path)

    def release(value):
        calls.append(value)
        agents_db.soft_delete(value)
        return 1

    ctx.runtime_client = SimpleNamespace(release_agent=release)

    AgentLifecycleService(ctx).delete("rachel")

    assert calls == [agent_id]
    assert agents_db.get_by_session("rachel") is None


def test_lifecycle_service_rejects_mcp_for_non_claude_backend(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "read_global_mcp_servers", lambda: {"files": {}})
    with pytest.raises(AgentLifecycleError) as error:
        AgentLifecycleService(_ctx(tmp_path)).create({
            "name": "Rachel", "session": "rachel", "cwd": str(tmp_path),
            "voice_id": "V_RACHEL", "backend": "codex",
            "mcp_servers": ["files"],
        })
    assert error.value.code == "mcp servers unsupported for backend"


def test_lifecycle_service_rejects_second_active_session_for_contact(tmp_path):
    ctx = _ctx(tmp_path)
    service = AgentLifecycleService(ctx)
    service.create({
        "name": "Lena",
        "session": "lena-xps",
        "cwd": str(tmp_path),
        "voice_id": "V_LENA",
    })

    with pytest.raises(AgentLifecycleError) as error:
        service.create({
            "name": "lena",
            "session": "lena-desktop",
            "cwd": str(tmp_path),
            "voice_id": "V_OTHER",
        })

    assert error.value.status == 409
    assert error.value.code == "contact_occupied"
    assert error.value.extra == {"owner": "Lena", "session": "lena-xps"}
    assert agents_db.get_by_session("lena-desktop") is None


def test_lifecycle_service_rejects_backend_session_owned_by_another_agent(tmp_path):
    ctx = _ctx(tmp_path)
    owner_id = agents_db.create_agent(
        persona="Mike", voice_id="V", cwd=str(tmp_path), session="mike",
    )
    agents_db.start_runtime(owner_id, "mike")
    agents_db.bind_backend_session(owner_id, "shared-session")

    with pytest.raises(AgentLifecycleError) as error:
        AgentLifecycleService(ctx).create({
            "name": "Rachel",
            "session": "rachel",
            "cwd": str(tmp_path),
            "voice_id": "V_RACHEL",
            "resume_session_id": "shared-session",
        })

    assert error.value.status == 409
    assert error.value.code == "session_in_use"


def test_lifecycle_service_preserves_voice_when_relaunch_omits_one(tmp_path):
    ctx = _ctx(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    agents_db.create_agent(
        persona="Rachel", voice_id="V_EXISTING", cwd=str(tmp_path), session="rachel",
    )

    result = AgentLifecycleService(ctx).create({
        "name": "Rachel",
        "replace_sid": "rachel",
        "cwd": str(other),
    })

    assert result.voice_id == "V_EXISTING"
    assert agents_db.get_by_session("rachel")["voice_id"] == "V_EXISTING"
    assert agents_db.get_by_session("rachel")["cwd"] == str(other)
    assert agents_db.favorite_paths()[0]["path"] == str(other)


def test_lifecycle_service_preserves_cwd_when_relaunch_omits_one(tmp_path):
    """A relaunch inherits the directory, like it inherits voice and backend.

    Without this an omitted cwd falls back to $HOME, which silently moves the
    agent out of its repo on a host and is refused outright in a container.
    """
    ctx = _ctx(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    agents_db.create_agent(
        persona="Rachel", voice_id="V", cwd=str(repo), session="rachel",
    )

    AgentLifecycleService(ctx).create({
        "name": "Rachel",
        "replace_sid": "rachel",
    })

    assert agents_db.get_by_session("rachel")["cwd"] == str(repo)


def test_path_usage_ranks_by_count_then_recency(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()

    agents_db.record_path_usage(str(first))
    agents_db.record_path_usage(str(second))
    agents_db.record_path_usage(str(first))

    favorites = agents_db.favorite_paths()
    assert [row["path"] for row in favorites] == [str(first), str(second)]
    assert favorites[0]["use_count"] == 2


def test_lifecycle_service_rejects_fork_for_backend_without_copy_semantics(tmp_path):
    with pytest.raises(AgentLifecycleError) as error:
        AgentLifecycleService(_ctx(tmp_path)).create({
            "name": "Arnold",
            "session": "arnold",
            "cwd": str(tmp_path),
            "backend": "agy",
            "fork_session_id": "conversation-1",
        })

    assert error.value.status == 400
    assert error.value.code == "fork_unsupported"


def test_lifecycle_service_rejects_invalid_agy_model_slug(tmp_path):
    with pytest.raises(AgentLifecycleError) as error:
        AgentLifecycleService(_ctx(tmp_path)).create({
            "name": "Arnold", "session": "arnold", "cwd": str(tmp_path),
            "backend": "agy", "model": "4.8",
        })
    assert error.value.status == 400
    assert "invalid model" in error.value.message
    assert agents_db.get_by_session("arnold") is None


def test_lifecycle_service_rejects_invalid_agy_effort(tmp_path):
    with pytest.raises(AgentLifecycleError) as error:
        AgentLifecycleService(_ctx(tmp_path)).create({
            "name": "Arnold", "session": "arnold", "cwd": str(tmp_path),
            "backend": "agy", "effort": "ultra",
        })
    assert error.value.status == 400
    assert agents_db.get_by_session("arnold") is None


def test_lifecycle_service_rejects_agy_model_effort_pair(tmp_path):
    with pytest.raises(AgentLifecycleError) as error:
        AgentLifecycleService(_ctx(tmp_path)).create({
            "name": "Arnold", "session": "arnold", "cwd": str(tmp_path),
            "backend": "agy", "model": "gemini-3.7-flash-low",
            "effort": "high",
        })
    assert error.value.status == 400


def test_lifecycle_rejects_effort_against_global_agy_model(tmp_path, monkeypatch):
    monkeypatch.setattr(
        config, "_CACHED",
        config.Config(agy_model="gemini-3.7-flash-low"))
    with pytest.raises(AgentLifecycleError) as error:
        AgentLifecycleService(_ctx(tmp_path)).create({
            "name": "Arnold", "session": "arnold", "cwd": str(tmp_path),
            "backend": "agy", "effort": "high",
        })
    assert error.value.status == 400
    assert agents_db.get_by_session("arnold") is None


def test_relaunch_rejects_partial_override_that_pairs_with_retained_agy_pin(tmp_path):
    service = AgentLifecycleService(_ctx(tmp_path))
    service.create({
        "name": "Arnold", "session": "arnold", "cwd": str(tmp_path),
        "backend": "agy", "model": "gemini-3.7-flash-low",
    })
    with pytest.raises(AgentLifecycleError) as error:
        service.create({
            "name": "Arnold", "replace_sid": "arnold", "cwd": str(tmp_path),
            "backend": "agy", "effort": "high",
        })
    assert error.value.status == 400


@pytest.mark.parametrize("field,value", [("model", 48), ("effort", ["high"])])
def test_lifecycle_service_rejects_non_string_llm_values(tmp_path, field, value):
    with pytest.raises(AgentLifecycleError) as error:
        AgentLifecycleService(_ctx(tmp_path)).create({
            "name": "Arnold", "session": "arnold", "cwd": str(tmp_path),
            "backend": "agy", field: value,
        })
    assert error.value.status == 400
    assert agents_db.get_by_session("arnold") is None


def test_lifecycle_service_allows_deleting_last_agent_for_server_move(tmp_path):
    ctx = _ctx(tmp_path)
    agent_id = agents_db.create_agent(
        persona="Mike", voice_id="V", cwd=str(tmp_path), session="mike",
    )
    agents_db.set_focus(agent_id)

    AgentLifecycleService(ctx).delete("mike")

    assert agents_db.get_by_session("mike") is None
    assert agents_db.get_focus() is None
    assert ctx.stream.events[-1] == {
        "type": "agent-roster", "kind": "deleted", "session": "mike",
    }


def test_lifecycle_service_releases_session_without_deleting_contact(tmp_path):
    ctx = _ctx(tmp_path)
    personas.create(name="Nova", voice_id='{"cartesia":"voice-1"}')
    agents_db.create_agent(
        persona="Nova", voice_id="", cwd=str(tmp_path), session="nova-chat",
    )

    AgentLifecycleService(ctx).delete("nova-chat")

    assert agents_db.get_by_session("nova-chat") is None
    assert personas.get("Nova") is not None

    replacement = AgentLifecycleService(ctx).create({
        "name": "Nova", "session": "nova-chat", "cwd": str(tmp_path),
        "synthesize_audio": False,
    })
    assert replacement.session != "nova-chat"
    assert agents_db.get_by_session(replacement.session)["persona"] == "Nova"


def test_lifecycle_service_suppresses_launch_announcement_for_silent_client(tmp_path):
    ctx = _ctx(tmp_path)

    AgentLifecycleService(ctx).create({
        "name": "Rachel",
        "session": "rachel",
        "cwd": str(tmp_path),
        "synthesize_audio": False,
    })

    assert ctx.announcements == []


def test_mint_session_is_unique_and_avoids_existing():
    """Regression: deleting + recreating an agent kept the old conversation,
    because the persona-derived session ('bella') collided with the soft-
    deleted row and resurrected it. create() now mints a unique session so a
    recreate is a genuinely fresh agent."""
    from lib.agent_lifecycle import AgentLifecycleService
    from lib import agents as agents_db

    a = AgentLifecycleService._mint_session("Antoni")
    b = AgentLifecycleService._mint_session("Antoni")
    assert a.startswith("antoni-") and b.startswith("antoni-")
    assert a != b, "successive mints must differ"
    assert not agents_db.session_exists(a), "minted id must be unused"

    # Seed that exact session, then confirm a fresh mint never returns it.
    agents_db.create_agent(persona="Antoni", voice_id="v", cwd="/tmp", session=a)
    assert agents_db.session_exists(a)
    for _ in range(10):
        assert AgentLifecycleService._mint_session("Antoni") != a
