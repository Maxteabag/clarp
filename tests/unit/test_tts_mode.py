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
    voice_id = agents_db.get_by_agent_id(env[agent_key])["voice_id"] or ""
    tts_queue.enqueue(agent_id=env[agent_key], text="hello there", voice_id=voice_id,
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


def test_vertex_backend_speaks_the_contacts_vertex_voice(env, monkeypatch):
    from lib import config, gemini_tts
    monkeypatch.setattr(config, "_CACHED", config.Config(
        tts_provider="gemini", gemini_api_key="gemini-key",
        gemini_backend="vertex", gemini_vertex_api_key="vertex-key",
        gemini_voices={"Mike": "voice_mike", "Jax": "voice_jax"},
        gemini_vertex_voices={"Mike": "voice_mike-vertex"},
        tts_agent_overrides={"jax": {"provider": "gemini", "gemini_voice": "Charon"}}))
    calls = []

    def synth(*, text, out_path, on_chunk=None, **kw):
        calls.append((kw["voice"], kw["api_key"], kw["backend"]))
        pathlib.Path(out_path).write_bytes(b"\xff\xfb\x90\x00")
        return 4

    monkeypatch.setattr(gemini_tts, "synthesize", synth)
    _speak(env, "mike", "mike-1")
    _speak(env, "jax", "jax-1")
    # The Gemini API id maps to Mike's Vertex voice; a prebuilt name passes.
    assert calls == [("voice_mike-vertex", "vertex-key", "vertex"),
                     ("Charon", "vertex-key", "vertex")]


def test_gemini_voice_follows_cartesia_voice_or_stays_silent(env, providers, monkeypatch):
    from lib import config
    monkeypatch.setattr(config, "_CACHED", config.Config(
        tts_provider="gemini", gemini_api_key="simulated",
        cartesia_voices={"Arnold": "cart-au"}, gemini_voices={"Arnold": "voice_au"}))
    helper = agents_db.create_agent(persona="R1_MAIN", voice_id='{"cartesia":"cart-au"}',
                                    cwd="/tmp", session="r1")
    silent = agents_db.create_agent(persona="Janitor", voice_id="", cwd="/tmp",
                                    session="janitor")
    env.update(helper=helper, silent=silent)
    _speak(env, "helper", "r1")
    assert providers == [{"provider": "gemini", "voice": "voice_au"}]
    from lib import db
    clips_before = db.conn().execute("SELECT count(*) FROM clips").fetchone()[0]
    row = _speak(env, "silent", "janitor")
    assert row["status"] == tts_queue.FAILED
    assert len(providers) == 1
    # No clip is announced for audio that can never be made.
    assert db.conn().execute("SELECT count(*) FROM clips").fetchone()[0] == clips_before


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
    assert cfg.gemini_backend == "gemini_api"
    assert cfg.gemini_tts_voice("voice_arnold") == "voice_arnold"


def test_config_reads_the_vertex_backend(tmp_path):
    from lib import config
    path = tmp_path / "config.toml"
    path.write_text('''
[gemini_tts]
api_key = "k"
backend = "vertex"
vertex_api_key = "vk"

[gemini_tts.voices]
Arnold = "voice_arnold"

[gemini_tts.vertex_voices]
Arnold = "voice_arnold-vertex"
''')
    cfg = config.load(path)
    assert (cfg.gemini_backend, cfg.gemini_tts_key()) == ("vertex", "vk")
    assert cfg.gemini_tts_voice("voice_arnold") == "voice_arnold-vertex"
    # Gemini Live (Oracle) keeps the Gemini API id.
    assert cfg.gemini_voice_for("Arnold") == "voice_arnold"


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


def test_gemini_retries_a_busy_response_before_any_audio(monkeypatch, tmp_path):
    import io, shutil, urllib.error
    from lib import gemini_tts
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")
    chunk = {"candidates": [{"content": {"parts": [{"inlineData": {
        "data": base64.b64encode(b"\x00\x00" * 2400).decode()}}]}}]}
    calls = []

    class Body(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout):
        calls.append(1)
        if len(calls) == 1:
            raise urllib.error.HTTPError(req.full_url, 503, "busy", {}, io.BytesIO(b"{}"))
        return Body(("data: " + json.dumps(chunk) + "\n").encode())

    monkeypatch.setattr(gemini_tts.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(gemini_tts.time, "sleep", lambda s: None)
    out = tmp_path / "clip.mp3"
    assert gemini_tts.synthesize(text="hi", voice="Kore", out_path=out, api_key="k") > 0
    assert len(calls) == 2 and out.stat().st_size > 0


def test_gemini_vertex_backend_posts_to_the_global_vertex_endpoint(monkeypatch, tmp_path):
    import io, shutil
    from lib import gemini_tts
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")
    chunk = {"candidates": [{"content": {"parts": [{"inlineData": {
        "data": base64.b64encode(b"\x00\x00" * 2400).decode()}}]}}]}
    seen = []

    class Body(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout):
        seen.append((req.full_url, req.get_header("X-goog-api-key"),
                     json.loads(req.data)["generationConfig"]["speechConfig"]))
        return Body(("data: " + json.dumps(chunk) + "\n").encode())

    monkeypatch.setattr(gemini_tts.urllib.request, "urlopen", fake_urlopen)
    assert gemini_tts.synthesize(text="hi", voice="voice_abc-123", out_path=tmp_path / "c.mp3",
                                 api_key="vk", backend="vertex") > 0
    assert seen == [(
        "https://aiplatform.googleapis.com/v1/publishers/google/models/"
        "gemini-3.8-flash-tts:streamGenerateContent?alt=sse",
        "vk", {"voiceConfig": {"voice": "voice_abc-123"}})]
