"""CAP: the advisory capability keys and their enforcement are one function.

``agents.interaction_capabilities`` says can_speak, can_cloud_dictation and
can_voice_target are false for a provider-profile agent, and speech, cloud
dictation and turn audio read exactly that function: patching it alone moves
an ordinary agent's enforcement too.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from lib import agents as agents_db
from lib import tts_queue
from lib import turn_dispatch as _td

pytestmark = pytest.mark.usefixtures("fake_codex_profile")


@pytest.fixture
def fleet():
    eu = agents_db.create_agent(persona="Eunice", voice_id="V", cwd="/tmp", session="eunice",
                                backend="codex", provider_profile="example-eu")
    plain = agents_db.create_agent(persona="Plain", voice_id="V", cwd="/tmp", session="plain",
                                   backend="codex")
    for agent_id, session in ((eu, "eunice"), (plain, "plain")):
        agents_db.start_runtime(agent_id, session)
    return SimpleNamespace(eu=eu, plain=plain)


KEYS = ("can_speak", "can_cloud_dictation", "can_voice_target")


def test_a_profile_agent_advertises_no_speech_cloud_dictation_or_voice_targeting(fleet):
    eu = agents_db.interaction_capabilities(agents_db.get_by_agent_id(fleet.eu))
    plain = agents_db.interaction_capabilities(agents_db.get_by_agent_id(fleet.plain))

    assert {key: eu[key] for key in KEYS} == dict.fromkeys(KEYS, False)
    assert {key: plain[key] for key in KEYS} == dict.fromkeys(KEYS, True)
    assert eu["can_chat"] and eu["can_inspect"]


def test_enforcement_reads_the_same_function(fleet, tmp_path, monkeypatch):
    real = agents_db.interaction_capabilities

    def nobody_speaks(agent):
        return {**real(agent), "can_speak": False, "can_cloud_dictation": False}

    monkeypatch.setattr(agents_db, "interaction_capabilities", nobody_speaks)

    # Speech: an ordinary agent is now text-only too.
    assert tts_queue.enqueue(agent_id=fleet.plain, text="hi", voice_id="V",
                             session="plain", source="test") == 0
    # Turn audio: dispatch turns it off for the same reason.
    monkeypatch.setattr(_td, "_RUNTIME_CLIENT", None)
    spawned = []
    registry = SimpleNamespace(
        CODEX="codex", normalize=lambda b: b or "claude", interrupt=lambda *a: 0,
        active_handles=lambda *a: [], spawn_turn=lambda backend, **kw: spawned.append(kw))
    _td.TurnDispatchService(
        SimpleNamespace(default_session="plain", agents_path=tmp_path / "x.json",
                        stream=SimpleNamespace(broadcast=lambda _e: None)),
        backend_registry=registry, home=tmp_path,
        retry_scheduler=lambda _d, fn: fn()).dispatch(
            text="hi", requested_session="plain", trace_id="t", synthesize_audio=True)
    assert spawned[0]["synthesize_audio"] is False
    # Dictation: a cloud engine is refused for the focused agent.
    from lib import stt as stt_module, transcription_pipeline
    cloud = object.__new__(stt_module.OpenAIWhisperSTT)
    cloud.available = True
    outcome = transcription_pipeline.transcribe(
        SimpleNamespace(stt=cloud, vocab_prompt=lambda **_k: ""),
        audio_bytes=b"RIFF0000WAVE", ctype="audio/wav", hands_free=False,
        requested_model="elevenlabs:scribe_v2", transcription_id="tx", fingerprint="fp",
        record_voice=lambda *a, **k: None, spawn_voice_metrics=lambda *a, **k: None,
        focus_session=lambda: "plain")
    assert outcome.status == 409
