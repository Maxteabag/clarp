"""Feature speech_outcome (L): a reply that is not spoken says so, once per turn, with the reason.

The owner's case (20 Nov 2026): an agent with voice_id {} and no persona record; the default
provider had no voice for it, the configured fallback (Cartesia) neither; three replies were
silent and nothing said why. The no-voice path sent nothing at all, and the other path guessed
its words from the error text ("Voice unavailable: TTS not configured.").
"""
from __future__ import annotations

import pathlib

import pytest

from lib import agents as agents_db, config, events, tts_queue, tts_registry, tts_worker
from lib.protocol import TurnSource


@pytest.fixture
def told(monkeypatch):
    sent = []
    monkeypatch.setattr(events, "broadcast", lambda stream, event: sent.append(dict(event)))
    return sent


def _agent(tmp_path, persona="Zed", voice_id="{}", session="zed"):
    return agents_db.create_agent(persona=persona, voice_id=voice_id, cwd=str(tmp_path), session=session)


def _speak(tmp_path, agent_id, *, clips=1, trace="trace-1", session="zed", voice_id="{}"):
    for n in range(clips):
        tts_queue.enqueue(agent_id=agent_id, text=f"Sentence {n}.", voice_id=voice_id,
                          session=session, source=TurnSource.PWA, trace_id=trace)
    audio = tmp_path / "audio"
    audio.mkdir(exist_ok=True)
    while tts_worker.synth_one(audio_dir=audio, stream=object()):
        pass


def _errors(sent):
    return [e for e in sent if e.get("type") == "tts-error"]


def test_a_voiceless_turn_of_five_clips_is_told_once(tmp_path, monkeypatch, told):
    monkeypatch.setattr(config, "_CACHED", config.Config(tts_provider="cartesia", cartesia_api_key="ck"))
    agent_id = _agent(tmp_path)
    _speak(tmp_path, agent_id, clips=5)
    errors = _errors(told)
    assert len(errors) == 1
    e = errors[0]
    assert (e["reason"], e["provider"], e["trace_id"], e["error"]) == ("no_voice", "cartesia", "trace-1", "no_voice")
    assert e["message"] == "Not spoken: Cartesia Sonic has no voice for Zed."
    assert all(row["status"] == tts_queue.FAILED for row in tts_queue.recent(limit=5))
    _speak(tmp_path, agent_id, clips=2, trace="trace-2")        # the next turn is told again
    assert [e["trace_id"] for e in _errors(told)] == ["trace-1", "trace-2"]


def test_the_owners_case_names_the_reason_not_the_fallbacks_error(tmp_path, monkeypatch, told):
    monkeypatch.setattr(config, "_CACHED", config.Config(
        tts_provider="gemini", tts_fallback="cartesia", gemini_api_key="gk", cartesia_api_key=""))
    agent_id = _agent(tmp_path)
    _speak(tmp_path, agent_id, clips=3)
    errors = _errors(told)
    assert len(errors) == 1
    assert errors[0]["reason"] == "no_voice" and errors[0]["provider"] == "gemini"
    assert "not configured" not in errors[0]["message"] and "Cartesia" not in errors[0]["message"]


def test_an_outage_carries_no_provider_text(tmp_path, monkeypatch, told):
    monkeypatch.setattr(config, "_CACHED", config.Config(
        tts_provider="cartesia", cartesia_api_key="ck", cartesia_voices={"Zed": "cart-zed"}))
    monkeypatch.setattr(tts_registry, "cartesia_synthesize", lambda **kw: (_ for _ in ()).throw(
        tts_registry.CartesiaError("HTTP 503 upstream id=abc123 secret detail")))
    agent_id = _agent(tmp_path)
    _speak(tmp_path, agent_id, clips=2)
    (e,) = _errors(told)
    assert (e["reason"], e["error"]) == ("provider_failed", "provider_failed")
    assert e["message"] == "Not spoken: Cartesia Sonic did not make the clip."
    assert "abc123" not in str(e)
    assert tts_queue.recent(limit=1)[0]["error"].startswith("provider_failed: ")   # the Host keeps the detail


def test_google_keeps_its_words_with_a_reason(monkeypatch, told):
    tts_worker._publish_tts_error(object(), {"session": "s", "agent_id": "a"}, {"persona": "Mike"},
                                  "google_quota: HTTP 429")
    (e,) = _errors(told)
    assert e["message"] == "Voice paused: Google Chirp 3 HD quota exceeded."
    assert (e["reason"], e["provider"]) == ("provider_failed", "google")
