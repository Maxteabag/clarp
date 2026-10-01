"""Live/quality voice split: which provider speaks a queued clip.

Exercised through `synth_one`, the worker boundary, with the real focus
table and application-activity leases standing in for "the chat is open".
"""
from __future__ import annotations

import base64
import json
import pathlib
import sys
import uuid

import pytest

_SERVER_DIR = pathlib.Path(__file__).resolve().parents[2] / "server"
sys.path.insert(0, str(_SERVER_DIR))

from lib import agents as agents_db                  # noqa: E402
from lib import tts_queue                             # noqa: E402
from lib.protocol import TurnSource                   # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    audio_dir = tmp_path / "audio"; audio_dir.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    from lib import config
    cfg = config.Config(
        tts_provider="cartesia", cartesia_api_key="simulated",
        gemini_api_key="simulated",
        tts_agent_overrides={"jax": {"quality_provider": "gemini",
                                     "gemini_voice": "Charon"}})
    monkeypatch.setattr(config, "_CACHED", cfg)
    jax = agents_db.create_agent(persona="Jax", voice_id="", cwd=str(tmp_path),
                                 session="jax-1")
    mike = agents_db.create_agent(persona="Mike", voice_id="V_MIKE",
                                  cwd=str(tmp_path), session="mike-1")
    return {"audio_dir": audio_dir, "cfg": cfg, "jax": jax, "mike": mike}


@pytest.fixture
def providers(monkeypatch):
    """Record which provider each clip went to; write a tiny mp3 for each."""
    calls: list[dict] = []
    failing: set[str] = set()

    def fake(name):
        def synth(*, text, out_path, on_chunk=None, **kw):
            calls.append({"provider": name, "voice": kw.get("voice") or kw.get("voice_id"),
                          "text": text})
            if name in failing:
                raise RuntimeError(f"{name} unavailable")
            pathlib.Path(out_path).write_bytes(b"\xff\xfb\x90\x00")
            return 4
        return synth

    from lib import gemini_tts, tts_worker
    monkeypatch.setattr(tts_worker, "cartesia_synthesize", fake("cartesia"))
    monkeypatch.setattr(gemini_tts, "synthesize", fake("gemini"))
    return {"calls": calls, "failing": failing}


def _speak(env, agent_key, session, delivery=None):
    from lib.tts_worker import synth_one
    tts_queue.enqueue(agent_id=env[agent_key], text="hello there", voice_id="",
                      session=session, source=TurnSource.PWA, trace_id="t-1")
    assert synth_one(audio_dir=env["audio_dir"], delivery=delivery) is True
    return tts_queue.recent(limit=1)[0]


def _open_chat(agent_id):
    from lib import application_activity, db
    agents_db.set_focus(agent_id)
    application_activity.report("administrator", str(uuid.uuid4()), 1, True, 0,
                                db.now_ms())


def test_closed_chat_uses_quality_voice_only_for_overridden_agent(env, providers):
    from lib.clip_delivery.raw_pcm import RawPcmDelivery
    row = _speak(env, "jax", "jax-1", delivery=RawPcmDelivery())
    assert row["status"] == tts_queue.DONE
    assert providers["calls"][-1] == {"provider": "gemini", "voice": "Charon",
                                      "text": "hello there"}
    # Agents without an override keep the live provider.
    _speak(env, "mike", "mike-1")
    assert providers["calls"][-1]["provider"] == "cartesia"


def test_open_chat_uses_live_voice(env, providers):
    _open_chat(env["jax"])
    _speak(env, "jax", "jax-1")
    assert [c["provider"] for c in providers["calls"]] == ["cartesia"]


def test_focus_without_a_foreground_client_counts_as_closed(env, providers):
    agents_db.set_focus(env["jax"])
    _speak(env, "jax", "jax-1")
    assert [c["provider"] for c in providers["calls"]] == ["gemini"]


def test_failed_quality_clip_is_spoken_by_live_voice(env, providers):
    providers["failing"].add("gemini")
    row = _speak(env, "jax", "jax-1")
    assert row["status"] == tts_queue.DONE
    assert [c["provider"] for c in providers["calls"]] == ["gemini", "cartesia"]


def test_config_reads_quality_provider_and_agent_overrides(tmp_path):
    from lib import config
    path = tmp_path / "config.toml"
    path.write_text('''
[tts]
provider = "cartesia"
quality_provider = "gemini"

[tts.agents.Jax]
live_provider = "elevenlabs"
gemini_voice = "voice_abc"

[gemini_tts]
api_key = "k"
model = "gemini-3.8-flash-lite-tts"
''')
    cfg = config.load(path)
    assert cfg.tts_quality_provider == "gemini"
    assert cfg.gemini_model == "gemini-3.8-flash-lite-tts"
    assert cfg.gemini_voice == "Kore"
    assert cfg.tts_override_for({"persona": "jax"}) == {
        "live_provider": "elevenlabs", "gemini_voice": "voice_abc"}
    assert cfg.tts_override_for({"persona": "Mike", "session": "mike-1"}) == {}


def test_gemini_sse_stream_yields_pcm_and_surfaces_errors():
    from lib.gemini_tts import GeminiTTSError, iter_pcm
    pcm = b"\x01\x00\x02\x00"
    chunk = {"candidates": [{"content": {"parts": [
        {"inlineData": {"mimeType": "audio/L16;rate=24000",
                        "data": base64.b64encode(pcm).decode()}}]}}]}
    lines = [b": keepalive", ("data: " + json.dumps(chunk)).encode(), b""]
    assert list(iter_pcm(lines)) == [pcm]
    with pytest.raises(GeminiTTSError):
        list(iter_pcm([b'data: {"error": {"code": 429}}']))
