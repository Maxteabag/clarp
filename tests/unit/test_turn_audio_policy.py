from lib import agents as agents_db
from lib import tts_queue
from lib.protocol import TurnSource


def _agent(tmp_path):
    return agents_db.create_agent(
        persona="Mike", voice_id="V", cwd=str(tmp_path), session="mike"
    )


def test_turn_audio_policy_defaults_true(tmp_path):
    agent_id = _agent(tmp_path)
    agents_db.open_turn(agent_id=agent_id, source=TurnSource.PWA, trace_id="t1")

    assert agents_db.latest_turn_synthesize_audio(agent_id) is True


def test_turn_audio_policy_persists_false(tmp_path):
    agent_id = _agent(tmp_path)
    agents_db.open_turn(
        agent_id=agent_id, source=TurnSource.PWA, trace_id="t1",
        synthesize_audio=False,
    )

    assert agents_db.latest_turn_synthesize_audio(agent_id) is False


def test_queue_suppresses_explicit_silent_turn(tmp_path):
    qid = tts_queue.enqueue(
        agent_id=_agent(tmp_path), text="silent", voice_id="V",
        session="mike", source=TurnSource.PWA,
        synthesize_audio=False,
    )

    assert qid == 0
    assert tts_queue.pending_count() == 0
