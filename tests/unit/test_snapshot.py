import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "server"))
from lib import agents as agents_db  # noqa: E402
from lib import db, turn_queue  # noqa: E402
from lib.audio_stream import AudioStream  # noqa: E402
from lib.context import ServerContext, StubSTT  # noqa: E402
from lib.snapshot import build_agent_snapshot  # noqa: E402
from lib.tts_engine import FakeTTSEngine  # noqa: E402


def test_snapshot_is_read_model_with_roster(tmp_path, monkeypatch):
    agent_id = agents_db.create_agent(
        persona="Rachel", voice_id="V", cwd=str(tmp_path), session="rachel",
        model="claude-sonnet-4-6", effort="high")
    agents_db.start_runtime(agent_id, "rachel")
    agents_db.record_state(agent_id, "thinking")
    turn_queue.enqueue(
        queue_id="queued-1", agent_id=agent_id, session="rachel",
        text="later", trace_id="trace-later", client_msg_id="queued-1",
        synthesize_audio=False, origin="user", sender_agent_id="")
    ctx = ServerContext(
        root=tmp_path,
        static=tmp_path,
        audio_dir=tmp_path / "audio",
        agents_path=tmp_path / "agents.json",
        default_session="rachel",
        tts=FakeTTSEngine(tmp_path / "audio"),
        stream=AudioStream(tmp_path / "audio"),
        stt=StubSTT(),
        roster_names=("Mike", "Rachel"),
    )

    # A live process backs the 'thinking' row, so busy is legitimately True
    # (the reconciler repairs busy rows that have no live work — INV1).
    from lib import reconcile
    monkeypatch.setattr(reconcile.backends, "active_handles", lambda b, a: ["proc"])
    snap = build_agent_snapshot(ctx)

    assert {"Mike", "Rachel"} <= set(snap["roster"])
    assert snap["agents"][0]["session"] == "rachel"
    assert snap["agents"][0]["busy"] is True
    assert snap["agents"][0]["model"] == "claude-sonnet-4-6"
    assert snap["agents"][0]["effort"] == "high"
    assert snap["agents"][0]["queued_turn_count"] == 1
    assert snap["agents"][0]["queue_paused"] is False


def test_snapshot_roster_includes_custom_live_persona(tmp_path):
    agents_db.create_agent(
        persona="Diego", voice_id="V", cwd=str(tmp_path), session="diego")
    ctx = ServerContext(
        root=tmp_path, static=tmp_path, audio_dir=tmp_path / "audio",
        agents_path=tmp_path / "agents.json",
        default_session="diego", tts=FakeTTSEngine(tmp_path / "audio"),
        stream=AudioStream(tmp_path / "audio"), stt=StubSTT(),
        roster_names=("Mike", "Rachel"),
    )

    snap = build_agent_snapshot(ctx)

    assert "Diego" in snap["roster"]


def test_snapshot_roster_deduplicates_persona_case_insensitively(tmp_path):
    agents_db.create_agent(
        persona="diego", voice_id="V", cwd=str(tmp_path), session="diego")
    ctx = ServerContext(
        root=tmp_path, static=tmp_path, audio_dir=tmp_path / "audio",
        agents_path=tmp_path / "agents.json",
        default_session="diego", tts=FakeTTSEngine(tmp_path / "audio"),
        stream=AudioStream(tmp_path / "audio"), stt=StubSTT(),
        roster_names=("Diego",),
    )

    snap = build_agent_snapshot(ctx)

    assert [name.casefold() for name in snap["roster"]].count("diego") == 1


def test_snapshot_emits_persisted_custom_status_across_state_changes(tmp_path):
    agent_id = agents_db.create_agent(
        persona="Arnold", voice_id="V", cwd=str(tmp_path), session="arnold")
    agents_db.set_custom_status(agent_id, "Awaiting Domi: notif policy")
    agents_db.record_state(agent_id, "idle")
    ctx = ServerContext(
        root=tmp_path,
        static=tmp_path,
        audio_dir=tmp_path / "audio",
        agents_path=tmp_path / "agents.json",
        default_session="arnold",
        tts=FakeTTSEngine(tmp_path / "audio"),
        stream=AudioStream(tmp_path / "audio"),
        stt=StubSTT(),
        roster_names=("Arnold",),
    )

    idle_snap = build_agent_snapshot(ctx)
    assert idle_snap["agents"][0]["latest_state"] == "idle"
    assert idle_snap["agents"][0]["status_text"] == "Awaiting Domi: notif policy"

    agents_db.record_state(agent_id, "thinking")
    busy_snap = build_agent_snapshot(ctx)
    # No live process backs that 'thinking' row: the reconciler (INV1) repairs
    # it at read time instead of rendering a phantom "working" badge. The
    # custom status must survive the repair.
    assert busy_snap["agents"][0]["busy"] is False
    assert agents_db.latest_state(agent_id)["kind"] == "idle"
    assert busy_snap["agents"][0]["status_text"] == "Awaiting Domi: notif policy"

    agents_db.set_custom_status(agent_id, "")
    agents_db.record_state(agent_id, "idle")
    cleared_snap = build_agent_snapshot(ctx)
    assert cleared_snap["agents"][0]["latest_state"] == "idle"
    assert cleared_snap["agents"][0]["status_text"] is None


def test_snapshot_last_message_drops_team_blocks(tmp_path):
    agent_id = agents_db.create_agent(
        persona="Rachel", voice_id="V", cwd=str(tmp_path), session="rachel")
    db.conn().execute(
        "INSERT INTO messages (message_id, agent_id, seq, role, timestamp, text,"
        " tools_json, updated_at, origin, revision) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            "m-team-preview", agent_id, 1, "assistant",
            "2026-06-21T10:00:00.000Z",
            "Done. <team>private coordination update</team> Next.",
            "[]", db.now_ms(), "user", 1,
        ),
    )
    ctx = ServerContext(
        root=tmp_path,
        static=tmp_path,
        audio_dir=tmp_path / "audio",
        agents_path=tmp_path / "agents.json",
        default_session="rachel",
        tts=FakeTTSEngine(tmp_path / "audio"),
        stream=AudioStream(tmp_path / "audio"),
        stt=StubSTT(),
        roster_names=("Rachel",),
    )

    snap = build_agent_snapshot(ctx)

    assert snap["agents"][0]["last_message"] == "Done. Next."
    assert snap["agents"][0]["last_message_id"] == "m-team-preview"
    # No runtime → no bound backend session → /log answers revision 0 for
    # this agent, and the snapshot head must agree (audit bug D1: a head the
    # client could never reach kept it reloading the transcript forever).
    # The preview above is still served from the stored messages.
    assert snap["agents"][0]["head_revision"] == 0
    assert snap["agents"][0]["conversation_id"] == ""


def test_snapshot_head_revision_is_zero_without_bound_session(tmp_path):
    """Audit bug D1: with no bound backend session, /log reports revision 0,
    but the snapshot queried latest_revision with an empty session id — which
    drops the WHERE clause and returns the MAX over the agent's previous
    conversations. The client then saw a head it could never reach and
    reloaded the full transcript on every poll. Both read models must agree."""
    agent_id = agents_db.create_agent(
        persona="Diego", voice_id="V", cwd=str(tmp_path), session="diego")
    agents_db.start_runtime(agent_id, "diego")   # runtime open, no session bound
    # A stale head from an earlier conversation of the same agent.
    db.conn().execute(
        "INSERT INTO conversation_heads(agent_id, backend_session_id, revision, replace_revision)"
        " VALUES (?, ?, ?, ?)", (agent_id, "old-session-uuid", 42, 0))
    ctx = ServerContext(
        root=tmp_path,
        static=tmp_path,
        audio_dir=tmp_path / "audio",
        agents_path=tmp_path / "agents.json",
        default_session="diego",
        tts=FakeTTSEngine(tmp_path / "audio"),
        stream=AudioStream(tmp_path / "audio"),
        stt=StubSTT(),
        roster_names=("Diego",),
    )
    snap = build_agent_snapshot(ctx)
    row = next(a for a in snap["agents"] if a["session"] == "diego")
    assert row.get("head_revision") == 0


def _model_avatar_ctx(tmp_path, static_root):
    return ServerContext(
        root=tmp_path, static=static_root, audio_dir=tmp_path / "audio",
        agents_path=tmp_path / "agents.json",
        default_session="rachel", tts=FakeTTSEngine(tmp_path / "audio"),
        stream=AudioStream(tmp_path / "audio"), stt=StubSTT(),
        roster_names=("Rachel",),
    )


def _bundle_model_avatar(static_root, name):
    folder = static_root / "avatars" / "models"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_bytes(b"\x89PNG\r\n\x1a\nvariant")


def test_snapshot_offers_the_portrait_drawn_for_the_agent_model(tmp_path):
    static_root = tmp_path / "static"
    _bundle_model_avatar(static_root, "rachel.opus.png")
    agents_db.create_agent(
        persona="Rachel", voice_id="V", cwd=str(tmp_path), session="rachel",
        backend="claude", model="claude-opus-5")

    snap = build_agent_snapshot(_model_avatar_ctx(tmp_path, static_root))

    row = snap["agents"][0]
    assert row["model_avatar_url"].startswith(
        "/static/avatars/models/rachel.opus.png?v=")
    # The ordinary portrait is still served; the preference decides which wins.
    assert row["avatar_url"] == ""
    assert snap["model_avatars"] is False


def test_snapshot_never_swaps_out_a_portrait_the_user_chose(tmp_path):
    static_root = tmp_path / "static"
    _bundle_model_avatar(static_root, "rachel.opus.png")
    portrait = tmp_path / "chosen.png"
    portrait.write_bytes(b"\x89PNG\r\n\x1a\nchosen")
    agent_id = agents_db.create_agent(
        persona="Rachel", voice_id="V", cwd=str(tmp_path), session="rachel",
        backend="claude", model="claude-opus-5")
    db.conn().execute("UPDATE agents SET avatar_path=? WHERE agent_id=?",
                      (str(portrait), agent_id))

    snap = build_agent_snapshot(_model_avatar_ctx(tmp_path, static_root))

    assert snap["agents"][0]["model_avatar_url"] == ""
    assert snap["agents"][0]["avatar_url"].startswith("/avatars/")


def test_snapshot_carries_the_computer_preference(tmp_path):
    from lib import avatar_settings

    agents_db.create_agent(
        persona="Rachel", voice_id="V", cwd=str(tmp_path), session="rachel")
    avatar_settings.update(True)

    snap = build_agent_snapshot(_model_avatar_ctx(tmp_path, tmp_path / "static"))

    assert snap["model_avatars"] is True
    assert snap["agents"][0]["model_avatar_url"] == ""
