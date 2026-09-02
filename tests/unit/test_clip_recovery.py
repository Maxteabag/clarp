"""Authoritative recovery for audio interrupted by iOS suspension."""

from lib import agents, clip_store


def _audio_event(clip_id: int, session: str, event_id_hint: int = 0) -> dict:
    return {
        "type": "audio",
        "clip_id": clip_id,
        "session": session,
        "url": f"/clips/{clip_id}/stream",
        "audio_format": {
            "encoding": "pcm_s16le", "sample_rate": 24000,
            "channels": 1, "bytes_per_sample": 2,
        },
        "event_id": event_id_hint,
    }


def test_recoverable_events_return_only_nonterminal_recent_session_clips(tmp_path):
    agent_id = agents.create_agent(
        persona="Ada", voice_id="voice", cwd=str(tmp_path), session="ada")
    other_id = agents.create_agent(
        persona="Lin", voice_id="voice", cwd=str(tmp_path), session="lin")
    playing = clip_store.record_clip(
        agent_id=agent_id, path=str(tmp_path / "playing.pcm"),
        producer_status="complete", runtime_id=lambda _agent_id: None)
    played = clip_store.record_clip(
        agent_id=agent_id, path=str(tmp_path / "played.pcm"),
        producer_status="complete", runtime_id=lambda _agent_id: None)
    other = clip_store.record_clip(
        agent_id=other_id, path=str(tmp_path / "other.pcm"),
        producer_status="complete", runtime_id=lambda _agent_id: None)
    agents.record_sse_event(_audio_event(playing, "ada"))
    agents.record_sse_event(_audio_event(played, "ada"))
    agents.record_sse_event(_audio_event(other, "lin"))
    clip_store.mark_clip_status(clip_id=playing, status="play-start")
    clip_store.mark_clip_status(clip_id=played, status="play-ok")

    recovered = clip_store.recoverable_events(session="ada")

    assert [event["clip_id"] for event in recovered] == [playing]


def test_recoverable_events_are_bounded_to_latest_three(tmp_path):
    agent_id = agents.create_agent(
        persona="Ada", voice_id="voice", cwd=str(tmp_path), session="ada")
    ids = []
    for index in range(5):
        clip_id = clip_store.record_clip(
            agent_id=agent_id, path=str(tmp_path / f"{index}.pcm"),
            producer_status="complete", runtime_id=lambda _agent_id: None)
        ids.append(clip_id)
        agents.record_sse_event(_audio_event(clip_id, "ada"))

    recovered = clip_store.recoverable_events(session="ada")

    assert [event["clip_id"] for event in recovered] == ids[-3:]


def test_conversation_held_events_survive_the_ordinary_recent_limit(tmp_path):
    agent_id = agents.create_agent(
        persona="Ada", voice_id="voice", cwd=str(tmp_path), session="ada")
    ids = []
    for index in range(6):
        clip_id = clip_store.record_clip(
            agent_id=agent_id, path=str(tmp_path / f"held-{index}.pcm"),
            producer_status="complete", runtime_id=lambda _agent_id: None)
        ids.append(clip_id)
        agents.record_sse_event(_audio_event(clip_id, "ada"))
    clip_store.mark_clip_status(clip_id=ids[0], status="held")
    clip_store.mark_clip_status(clip_id=ids[1], status="held")

    recovered = clip_store.recoverable_events(session="ada")

    assert [event["clip_id"] for event in recovered] == ids[:2] + ids[-3:]
