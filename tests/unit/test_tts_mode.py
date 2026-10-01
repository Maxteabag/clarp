"""Per-agent voice provider: which provider speaks a queued clip.

Exercised through `synth_one`, the worker boundary.
"""
from __future__ import annotations

import base64
import json
import pathlib
import sys

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
        tts_agent_overrides={"jax": {"provider": "gemini",
                                     "gemini_voice": "Charon"}})
    monkeypatch.setattr(config, "_CACHED", cfg)
    jax = agents_db.create_agent(persona="Jax", voice_id="", cwd=str(tmp_path),
                                 session="jax-1")
    mike = agents_db.create_agent(persona="Mike", voice_id="V_MIKE",
                                  cwd=str(tmp_path), session="mike-1")
    return {"audio_dir": audio_dir, "jax": jax, "mike": mike}


@pytest.fixture
def providers(monkeypatch):
    """Record which provider each clip went to; write a tiny mp3 for each."""
    calls: list[dict] = []

    def fake(name):
        def synth(*, text, out_path, on_chunk=None, **kw):
            calls.append({"provider": name,
                          "voice": kw.get("voice") or kw.get("voice_id")})
            pathlib.Path(out_path).write_bytes(b"\xff\xfb\x90\x00")
            return 4
        return synth

    from lib import gemini_tts, tts_worker
    monkeypatch.setattr(tts_worker, "cartesia_synthesize", fake("cartesia"))
    monkeypatch.setattr(gemini_tts, "synthesize", fake("gemini"))
    return calls


def _speak(env, agent_key, session, delivery=None):
    from lib.tts_worker import synth_one
    tts_queue.enqueue(agent_id=env[agent_key], text="hello there", voice_id="",
                      session=session, source=TurnSource.PWA, trace_id="t-1")
    assert synth_one(audio_dir=env["audio_dir"], delivery=delivery) is True
    return tts_queue.recent(limit=1)[0]


def test_agent_override_picks_provider_and_voice_under_raw_pcm(env, providers):
    from lib.clip_delivery.raw_pcm import RawPcmDelivery
    row = _speak(env, "jax", "jax-1", delivery=RawPcmDelivery())
    assert row["status"] == tts_queue.DONE
    assert providers == [{"provider": "gemini", "voice": "Charon"}]


def test_agents_without_override_keep_the_global_provider(env, providers):
    _speak(env, "mike", "mike-1")
    assert [c["provider"] for c in providers] == ["cartesia"]


def test_gemini_provider_uses_contact_voice_map(env, providers, monkeypatch):
    from lib import config
    monkeypatch.setattr(config, "_CACHED", config.Config(
        tts_provider="gemini", gemini_api_key="simulated",
        gemini_voices={"Mike": "voice_mike"}))
    _speak(env, "mike", "mike-1")
    # Mike's stored voice map has no gemini entry; the contact map supplies it.
    assert providers == [{"provider": "gemini", "voice": "voice_mike"}]


def test_config_reads_agent_overrides(tmp_path):
    from lib import config
    path = tmp_path / "config.toml"
    path.write_text('''
[tts]
provider = "cartesia"

[tts.agents.Jax]
provider = "gemini"
gemini_voice = "voice_abc"

[gemini_tts]
api_key = "k"
model = "gemini-3.8-flash-lite-tts"

[gemini_tts.voices]
Arnold = "voice_arnold"
''')
    cfg = config.load(path)
    assert cfg.gemini_model == "gemini-3.8-flash-lite-tts"
    assert cfg.gemini_voice == "Kore"
    assert cfg.gemini_voice_for("arnold") == "voice_arnold"
    assert cfg.gemini_voice_for("Arnold-1a2b") == "voice_arnold"
    assert cfg.gemini_voice_for("Mike") is None
    assert cfg.tts_override_for({"persona": "jax"}) == {
        "provider": "gemini", "gemini_voice": "voice_abc"}
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
