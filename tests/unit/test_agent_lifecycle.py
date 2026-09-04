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


def test_lifecycle_delete_rejects_unknown_session(tmp_path):
    with pytest.raises(AgentLifecycleError) as error:
        AgentLifecycleService(_ctx(tmp_path)).delete("missing")

    assert error.value.status == 404
    assert error.value.code == "agent_not_found"


def test_lifecycle_reset_rejects_default_session_without_mutation(tmp_path):
    ctx = _ctx(tmp_path)
    ctx.default_session = "mike"
    old_agent_id = agents_db.create_agent(
        persona="Mike", voice_id="voice", cwd=str(tmp_path), session="mike")

    with pytest.raises(AgentLifecycleError) as error:
        AgentLifecycleService(ctx).reset(["mike"])

    assert error.value.status == 409
    assert error.value.code == "default_session_reset_forbidden"
    assert agents_db.get_by_session("mike")["agent_id"] == old_agent_id


def test_lifecycle_reset_rejects_active_handle_without_terminating_it(
    tmp_path, monkeypatch,
):
    from lib import backends

    old_agent_id = agents_db.create_agent(
        persona="Rachel", voice_id="voice", cwd=str(tmp_path), session="rachel")

    class Handle:
        def __init__(self):
            self.proc = self
            self.terminated = False

        def is_alive(self):
            return not self.terminated

        def terminate(self):
            self.terminated = True

    handle = Handle()
    monkeypatch.setattr(
        backends, "active_handles_any", lambda _agent_id: [handle])

    with pytest.raises(AgentLifecycleError) as error:
        AgentLifecycleService(_ctx(tmp_path)).reset(["rachel"])

    assert error.value.code == "agent_reset_active_work"
    assert handle.terminated is False
    assert agents_db.get_by_session("rachel")["agent_id"] == old_agent_id


def test_lifecycle_reset_rejects_spawn_registration_window(tmp_path):
    from lib import turn_dispatch

    old_agent_id = agents_db.create_agent(
        persona="Rachel", voice_id="voice", cwd=str(tmp_path), session="rachel")
    with turn_dispatch._TURN_LOCK:
        turn_dispatch._INFLIGHT[old_agent_id] = "trace-starting"
        turn_dispatch._CLAIMED_AT[old_agent_id] = 1.0
    try:
        with pytest.raises(AgentLifecycleError) as error:
            AgentLifecycleService(_ctx(tmp_path)).reset(["rachel"])
        assert error.value.code == "agent_reset_spawn_in_progress"
        assert agents_db.get_by_session("rachel")["agent_id"] == old_agent_id
    finally:
        turn_dispatch.clear_for_agent(old_agent_id, preserve_queue=True)


def test_lifecycle_reset_rejects_live_interactive_terminal(tmp_path, monkeypatch):
    from lib import terminal_ws

    old_agent_id = agents_db.create_agent(
        persona="Rachel", voice_id="voice", cwd=str(tmp_path), session="rachel")
    monkeypatch.setattr(
        terminal_ws, "has_live_terminal", lambda agent_id: agent_id == old_agent_id)

    with pytest.raises(AgentLifecycleError) as error:
        AgentLifecycleService(_ctx(tmp_path)).reset(["rachel"])

    assert error.value.code == "agent_reset_terminal_active"
    assert agents_db.get_by_session("rachel")["agent_id"] == old_agent_id


def test_lifecycle_reset_preserves_configuration_with_fresh_history(
    tmp_path, monkeypatch,
):
    from lib import artifacts, backends, db, dreaming, media_store, task_plans
    from lib.paths import RuntimePaths

    monkeypatch.setattr(backends, "interrupt_any", lambda _agent_id: 0)
    old_agent_id = agents_db.create_agent(
        persona="Rachel", voice_id="voice-r", cwd=str(tmp_path),
        session="rachel-demo", backend="codex", model="gpt-5.6",
        effort="high",
    )
    agents_db.update_agent(
        old_agent_id,
        mcp_servers='{"configured": true, "servers": ["files"]}',
        heartbeat_enabled=True,
        dreaming_enabled=True,
        dreaming_last_local_date="2026-09-04",
        muted=True,
        avatar_symbol="sparkles",
        personality="Patient and precise",
        avatar_path="/avatars/rachel.jpg",
    )
    agents_db.set_archived(old_agent_id, True)
    agents_db.start_runtime(old_agent_id, "rachel-demo")
    agents_db.bind_backend_session(old_agent_id, "backend-old")
    agents_db.set_focus(old_agent_id)
    focus_file = RuntimePaths.from_home(tmp_path).app_session
    focus_file.parent.mkdir(parents=True)
    focus_file.write_text("rachel-demo\n")
    connection = db.conn()
    connection.execute(
        """INSERT INTO messages
               (message_id,agent_id,backend_session_id,seq,role,text,updated_at)
             VALUES ('old-message',?,'backend-old',1,'user','hello',1)""",
        (old_agent_id,),
    )
    connection.execute(
        "INSERT INTO teams(team_id,name,created_at,updated_at,leader_agent_id) "
        "VALUES ('team-r','Demo',1,1,?)",
        (old_agent_id,),
    )
    connection.execute(
        "INSERT INTO team_members(team_id,agent_id,position,added_at) "
        "VALUES ('team-r',?,0,1)",
        (old_agent_id,),
    )
    connection.execute(
        """INSERT INTO team_messages
               (team_message_id,team_id,source_agent_id,source_message_id,text,created_at)
             VALUES ('team-message-r','team-r',?,'source-r','Update',1)""",
        (old_agent_id,),
    )
    connection.execute(
        """INSERT INTO team_inbox(team_message_id,agent_id,status)
             VALUES ('team-message-r',?,'unread')""",
        (old_agent_id,),
    )
    connection.execute(
        """INSERT INTO agent_schedules
               (schedule_id,agent_id,session,name,cron_expression,prompt,
                enabled,created_at,updated_at)
             VALUES ('schedule-r',?,'rachel-demo','Daily','0 9 * * *','Check',1,1,1)""",
        (old_agent_id,),
    )
    dreaming.create_dream_run(
        agents_db.get_by_session("rachel-demo"),
        local_date="2026-09-04", timezone_name="Europe/Oslo",
        timezone_source="test",
    )
    dreaming.mark_active_noop(old_agent_id)
    media = media_store.publish(
        session="rachel-demo", blob=b"%PDF-1.7\nkept",
        source_name="kept.pdf", content_type="application/pdf",
        media_dir=tmp_path / "media",
    )
    document = artifacts.create(
        session="rachel-demo", type="document", title="Keep me",
        payload={"content": "Durable content"},
    )
    completed_plan = task_plans.create(
        session="rachel-demo", title="Finished plan",
        items=[{"id": "done", "title": "Done"}],
    )
    task_plans.finish(completed_plan["plan_id"])

    ctx = _ctx(tmp_path)
    [reset] = AgentLifecycleService(ctx).reset(["rachel-demo"])

    assert reset.old_agent_id == old_agent_id
    assert reset.new_agent_id != old_agent_id
    assert reset.new_session != "rachel-demo"
    assert agents_db.get_by_session("rachel-demo") is None
    fresh = agents_db.get_by_session(reset.new_session)
    redirected, redirected_session = agents_db.resolve_live_session("rachel-demo")
    assert redirected["agent_id"] == reset.new_agent_id
    assert redirected_session == reset.new_session
    assert fresh == {
        **fresh,
        "agent_id": reset.new_agent_id,
        "persona": "Rachel",
        "voice_id": "voice-r",
        "cwd": str(tmp_path),
        "backend": "codex",
        "model": "gpt-5.6",
        "effort": "high",
        "mcp_servers": '{"configured": true, "servers": ["files"]}',
        "heartbeat_enabled": 1,
        "dreaming_enabled": 1,
        "dreaming_last_local_date": "2026-09-04",
        "muted": 1,
        "avatar_symbol": "sparkles",
        "personality": "Patient and precise",
        "avatar_path": "/avatars/rachel.jpg",
    }
    assert fresh["archived_at"] is not None
    assert agents_db.get_focus() == reset.new_agent_id
    assert focus_file.read_text() == reset.new_session + "\n"
    assert ctx.stream.events[-1] == {
        "type": "agent-focus",
        "session": reset.new_session,
        "agent_id": reset.new_agent_id,
    }
    assert agents_db.live_backend_session(reset.new_agent_id) == ""
    assert connection.execute(
        "SELECT COUNT(*) FROM messages WHERE agent_id=?", (old_agent_id,),
    ).fetchone()[0] == 1
    assert connection.execute(
        "SELECT COUNT(*) FROM messages WHERE agent_id=?", (reset.new_agent_id,),
    ).fetchone()[0] == 0
    assert dict(connection.execute(
        "SELECT agent_id,session FROM agent_schedules WHERE schedule_id='schedule-r'"
    ).fetchone()) == {
        "agent_id": reset.new_agent_id, "session": reset.new_session,
    }
    assert connection.execute(
        "SELECT leader_agent_id FROM teams WHERE team_id='team-r'"
    ).fetchone()[0] == reset.new_agent_id
    assert connection.execute(
        "SELECT agent_id FROM team_members WHERE team_id='team-r'"
    ).fetchone()[0] == reset.new_agent_id
    assert connection.execute(
        "SELECT agent_id FROM team_inbox WHERE team_message_id='team-message-r'"
    ).fetchone()[0] == reset.new_agent_id
    assert dreaming.dream_runs_for_agent_date(
        reset.new_agent_id, "2026-09-04") == 1
    dream = connection.execute(
        "SELECT agent_id,session FROM dream_runs WHERE local_date='2026-09-04'"
    ).fetchone()
    assert dict(dream) == {
        "agent_id": reset.new_agent_id, "session": reset.new_session,
    }
    assert media_store.get(media["asset_id"])["agent_id"] == reset.new_agent_id
    assert media_store.get(media["asset_id"])["session"] == reset.new_session
    assert artifacts.get(document["artifact_id"])["agent_id"] == reset.new_agent_id
    assert artifacts.get(document["artifact_id"])["session"] == reset.new_session
    assert task_plans.get(completed_plan["plan_id"])["agent_id"] == reset.new_agent_id
    assert task_plans.get(completed_plan["plan_id"])["session"] == reset.new_session


def test_lifecycle_multi_reset_rolls_back_every_session_on_failure(
    tmp_path, monkeypatch,
):
    from lib import backends
    from lib.paths import RuntimePaths

    monkeypatch.setattr(backends, "interrupt_any", lambda _agent_id: 0)
    agents_db.create_agent(
        persona="Rachel", voice_id="r", cwd=str(tmp_path), session="rachel")
    agents_db.create_agent(
        persona="Mike", voice_id="m", cwd=str(tmp_path), session="mike")
    rachel_id = agents_db.get_by_session("rachel")["agent_id"]
    agents_db.set_focus(rachel_id)
    focus_file = RuntimePaths.from_home(tmp_path).app_session
    focus_file.parent.mkdir(parents=True)
    focus_file.write_text("rachel\n")
    real_create = agents_db.create_agent
    calls = 0

    def fail_second_create(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected reset failure")
        return real_create(**kwargs)

    monkeypatch.setattr(agents_db, "create_agent", fail_second_create)

    with pytest.raises(AgentLifecycleError) as error:
        AgentLifecycleService(_ctx(tmp_path)).reset(["rachel", "mike"])

    assert error.value.status == 500
    assert agents_db.get_by_session("rachel") is not None
    assert agents_db.get_by_session("mike") is not None
    assert {row["session"] for row in agents_db.list_agents()} == {"rachel", "mike"}
    assert agents_db.get_focus() == rachel_id
    assert focus_file.read_text() == "rachel\n"


def test_lifecycle_reset_rejects_durable_queued_work(tmp_path):
    from lib import db

    agent_id = agents_db.create_agent(
        persona="Rachel", voice_id="r", cwd=str(tmp_path), session="rachel")
    db.conn().execute(
        """INSERT INTO queued_turns
               (queue_id,agent_id,session,text,trace_id,client_msg_id,
                synthesize_audio,origin,sender_agent_id,enqueued_at)
             VALUES ('queued-r',?,'rachel','keep me','trace-r','client-r',
                     0,'user','',1)""",
        (agent_id,),
    )

    with pytest.raises(AgentLifecycleError) as error:
        AgentLifecycleService(_ctx(tmp_path)).reset(["rachel"])

    assert error.value.code == "agent_reset_active_work"
    assert agents_db.get_by_session("rachel")["agent_id"] == agent_id
    assert db.conn().execute(
        "SELECT text FROM queued_turns WHERE queue_id='queued-r'"
    ).fetchone()[0] == "keep me"


def test_lifecycle_reset_rejects_agent_owned_background_job(tmp_path):
    from lib import background_jobs

    agent_id = agents_db.create_agent(
        persona="Rachel", voice_id="r", cwd=str(tmp_path), session="rachel")
    background_jobs.upsert(
        session="rachel", job_id="work-r", kind="other", title="Still working")

    with pytest.raises(AgentLifecycleError) as error:
        AgentLifecycleService(_ctx(tmp_path)).reset(["rachel"])

    assert error.value.code == "agent_reset_active_work"
    assert agents_db.get_by_session("rachel")["agent_id"] == agent_id


def test_lifecycle_reset_rejects_computer_owned_portrait_job(tmp_path):
    from lib import background_jobs

    agent_id = agents_db.create_agent(
        persona="Rachel", voice_id="r", cwd=str(tmp_path), session="rachel")
    background_jobs.upsert_computer(
        computer_id="computer-a",
        job_id="portrait-r",
        kind="portrait-generation",
        title="Generate Rachel portraits",
        status="queued",
        metadata={"session": "rachel"},
    )

    with pytest.raises(AgentLifecycleError) as error:
        AgentLifecycleService(_ctx(tmp_path)).reset(["rachel"])

    assert error.value.code == "agent_reset_active_work"
    assert agents_db.get_by_session("rachel")["agent_id"] == agent_id


def test_lifecycle_reset_rejects_active_dream_run(tmp_path):
    from lib import dreaming

    agent_id = agents_db.create_agent(
        persona="Rachel", voice_id="r", cwd=str(tmp_path), session="rachel")
    dreaming.create_dream_run(
        agents_db.get_by_session("rachel"),
        local_date="2026-09-04",
        timezone_name="Europe/Oslo",
        timezone_source="test",
    )

    with pytest.raises(AgentLifecycleError) as error:
        AgentLifecycleService(_ctx(tmp_path)).reset(["rachel"])

    assert error.value.code == "agent_reset_active_work"
    assert agents_db.get_by_session("rachel")["agent_id"] == agent_id


def test_lifecycle_reset_rejects_queued_tts(tmp_path):
    from lib import tts_queue

    agent_id = agents_db.create_agent(
        persona="Rachel", voice_id="r", cwd=str(tmp_path), session="rachel")
    tts_queue.enqueue(
        agent_id=agent_id,
        text="Still speaking",
        voice_id="r",
        session="rachel",
        source="test",
    )

    with pytest.raises(AgentLifecycleError) as error:
        AgentLifecycleService(_ctx(tmp_path)).reset(["rachel"])

    assert error.value.code == "agent_reset_active_work"
    assert agents_db.get_by_session("rachel")["agent_id"] == agent_id


def test_lifecycle_reset_rejects_accepted_oracle_delegation(tmp_path):
    from lib import oracle_delegations

    agent_id = agents_db.create_agent(
        persona="Rachel", voice_id="r", cwd=str(tmp_path), session="rachel")
    oracle_delegations.begin(
        delegation_id="oracle-reset-test",
        trace_id="oracle-trace",
        client_msg_id="oracle-client",
        agent_id=agent_id,
        session="rachel",
        request_text="Do the work",
    )

    with pytest.raises(AgentLifecycleError) as error:
        AgentLifecycleService(_ctx(tmp_path)).reset(["rachel"])

    assert error.value.code == "agent_reset_active_work"
    assert agents_db.get_by_session("rachel")["agent_id"] == agent_id


def test_lifecycle_reset_rejects_active_plan_and_pending_decision(tmp_path):
    from lib import artifacts, db, task_plans

    agent_id = agents_db.create_agent(
        persona="Rachel", voice_id="r", cwd=str(tmp_path), session="rachel")
    task_plans.create(
        session="rachel", title="Active plan",
        items=[{"id": "one", "title": "Keep working"}],
    )

    with pytest.raises(AgentLifecycleError) as plan_error:
        AgentLifecycleService(_ctx(tmp_path)).reset(["rachel"])
    assert plan_error.value.code == "agent_reset_active_work"

    task_plan = task_plans.active_for_session("rachel")
    task_plans.finish(task_plan["plan_id"])
    decision = artifacts.create_decision(
        session="rachel", title="Approval", question="Continue?")
    decision_id = db.conn().execute(
        "SELECT decision_id FROM artifact_decisions WHERE artifact_id=?",
        (decision["artifact_id"],),
    ).fetchone()[0]
    artifacts.resolve(decision_id, choice="accepted", expected_revision=1)

    with pytest.raises(AgentLifecycleError) as decision_error:
        AgentLifecycleService(_ctx(tmp_path)).reset(["rachel"])
    assert decision_error.value.code == "agent_reset_active_work"
    assert agents_db.get_by_session("rachel")["agent_id"] == agent_id


def test_lifecycle_reset_rejects_pending_hands_free_route(tmp_path):
    from lib import turn_dispatch

    agent_id = agents_db.create_agent(
        persona="Rachel", voice_id="r", cwd=str(tmp_path), session="rachel")

    with turn_dispatch.orchestrator_admission("rachel"):
        with pytest.raises(AgentLifecycleError) as error:
            AgentLifecycleService(_ctx(tmp_path)).reset(["rachel"])

    assert error.value.code == "agent_reset_active_work"
    assert agents_db.get_by_session("rachel")["agent_id"] == agent_id


def test_lifecycle_reset_rejects_persisted_routing_clarification(tmp_path):
    from lib import db

    agent_id = agents_db.create_agent(
        persona="Rachel", voice_id="r", cwd=str(tmp_path), session="rachel")
    now = db.now_ms()
    db.conn().execute(
        """INSERT INTO orchestrator_pending_utterances
               (pending_id,utterance,requested_session,candidate_session,
                speak_as_session,created_at,expires_at,status)
             VALUES ('pending-r','yes','rachel','rachel','rachel',?,?,'pending')""",
        (now, now + 60_000),
    )

    with pytest.raises(AgentLifecycleError) as error:
        AgentLifecycleService(_ctx(tmp_path)).reset(["rachel"])

    assert error.value.code == "agent_reset_active_work"
    assert agents_db.get_by_session("rachel")["agent_id"] == agent_id


def test_lifecycle_reset_rejects_unheard_clip_and_draft_artifact(tmp_path):
    from lib import artifacts, db

    agent_id = agents_db.create_agent(
        persona="Rachel", voice_id="r", cwd=str(tmp_path), session="rachel")
    db.conn().execute(
        """INSERT INTO clips(agent_id,path,created_at,status,producer_status)
             VALUES (?,'/tmp/unheard.mp3',1,'broadcast','complete')""",
        (agent_id,),
    )
    artifacts.create(
        session="rachel", type="document", title="Draft",
        status="draft", payload={"content": "Not finished"},
    )

    with pytest.raises(AgentLifecycleError) as error:
        AgentLifecycleService(_ctx(tmp_path)).reset(["rachel"])

    assert error.value.code == "agent_reset_active_work"
    assert agents_db.get_by_session("rachel")["agent_id"] == agent_id


def test_lifecycle_reset_ignores_terminal_failed_producer_clip(tmp_path):
    from lib import db

    agent_id = agents_db.create_agent(
        persona="Rachel", voice_id="r", cwd=str(tmp_path), session="rachel")
    db.conn().execute(
        """INSERT INTO clips(agent_id,path,created_at,status,producer_status)
             VALUES (?,'/tmp/failed.mp3',1,'synthesized','failed')""",
        (agent_id,),
    )

    [result] = AgentLifecycleService(_ctx(tmp_path)).reset(["rachel"])

    assert agents_db.get_by_session(result.new_session) is not None


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
