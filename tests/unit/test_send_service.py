from lib import agents as agents_db
from lib.send_service import resolve_send_target, source_marker_text


def test_resolve_send_target_routes_by_spoken_agent_name(tmp_path):
    agents_db.create_agent(
        persona="Rachel", voice_id="V", cwd=str(tmp_path), session="rachel"
    )

    target = resolve_send_target(
        text="Rachel can you check this",
        requested_session="mike",
        default_session="mike",
        agents_path=tmp_path / "unused.json",
    )

    assert target.session == "rachel"
    assert target.text == "can you check this"


def test_resolve_send_target_flags_name_routing_for_sticky_focus(tmp_path):
    agents_db.create_agent(persona="Rachel", voice_id="V", cwd=str(tmp_path), session="rachel")
    agents_db.create_agent(persona="Mike", voice_id="V", cwd=str(tmp_path), session="mike")

    target = resolve_send_target(
        text="Rachel, status?", requested_session="mike",
        default_session="mike", agents_path=tmp_path / "unused.json",
    )
    assert target.session == "rachel"
    assert target.routed_by_name is True


def test_resolve_send_target_does_not_route_trailing_name_mention(tmp_path):
    agents_db.create_agent(persona="Bella", voice_id="V", cwd=str(tmp_path), session="bella")
    agents_db.create_agent(persona="Antoni", voice_id="V", cwd=str(tmp_path), session="antoni")

    target = resolve_send_target(
        text="Yeah, go ahead and fix that, Bella.",
        requested_session="antoni",
        default_session="antoni",
        agents_path=tmp_path / "unused.json",
        sticky_session="antoni",
    )

    assert target.session == "antoni"
    assert target.routed_by_name is False


def test_resolve_send_target_does_not_route_mid_utterance_name_mention(tmp_path):
    agents_db.create_agent(persona="Bella", voice_id="V", cwd=str(tmp_path), session="bella")
    agents_db.create_agent(persona="Antoni", voice_id="V", cwd=str(tmp_path), session="antoni")

    target = resolve_send_target(
        text=(
            "Yeah, go ahead and fix that, Bella. And also, why am I hearing "
            "Bella here ready for an update?"
        ),
        requested_session="antoni",
        default_session="antoni",
        agents_path=tmp_path / "unused.json",
        sticky_session="antoni",
    )

    assert target.session == "antoni"
    assert target.routed_by_name is False


def test_resolve_send_target_routes_second_word_spoken_agent_name(tmp_path):
    agents_db.create_agent(
        persona="Arnold", voice_id="V", cwd=str(tmp_path), session="arnold"
    )
    agents_db.create_agent(
        persona="Mike", voice_id="V", cwd=str(tmp_path), session="mike"
    )

    target = resolve_send_target(
        text="also Arnold, do X",
        requested_session="mike",
        default_session="mike",
        agents_path=tmp_path / "unused.json",
    )

    assert target.session == "arnold"
    assert target.text == "do X"
    assert target.routed_by_name is True


def test_resolve_send_target_unnamed_sticks_to_last_addressed(tmp_path):
    """An un-named message follows the sticky (last-addressed) agent, not the
    client's requested session — so a hands-free user keeps talking to whoever
    they last named without repeating it."""
    agents_db.create_agent(persona="Rachel", voice_id="V", cwd=str(tmp_path), session="rachel")
    agents_db.create_agent(persona="Mike", voice_id="V", cwd=str(tmp_path), session="mike")

    target = resolve_send_target(
        text="okay keep going", requested_session="mike",
        default_session="mike", agents_path=tmp_path / "unused.json",
        sticky_session="rachel",
    )
    assert target.session == "rachel"      # stuck to Rachel, not the requested "mike"
    assert target.routed_by_name is False  # no name in the message


def test_resolve_send_target_falls_back_when_requested_agent_is_missing(tmp_path):
    agents_db.create_agent(
        persona="Mike", voice_id="V", cwd=str(tmp_path), session="mike"
    )

    target = resolve_send_target(
        text="hello",
        requested_session="missing",
        default_session="mike",
        agents_path=tmp_path / "unused.json",
    )

    assert target.session == "mike"
    assert target.text == "hello"


def test_source_marker_text_has_trace_and_session():
    assert source_marker_text(
        session="mike", trace_id="t1", now=12.3456, synthesize_audio=False
    ) == (
        "pwa-voice mike 12.346 t1 0\n"
    )
