"""Characterization of the voice-out handlers' branches the full suite did not
reach (Cartesia voices, voice previews per provider, controller narration,
agent mute, voice timeline helpers), as server.py serves them today: G0 for the
S3 voice-out recipe entry (route-registry.md section 8). Synthesis and catalogs
are stubbed at their module only to reach the handler's own branches.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
import uuid

import pytest

from tests.integration.test_server_di import (  # noqa: F401 - fixtures
    _model_catalog_never_probes_installed_clis, fake_ctx, running_server, server_module)


def _request(base, method, path, body=None, raw=None):
    data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
    request = urllib.request.Request(base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def _call(base, method, path, body=None, raw=None):
    status, payload = _request(base, method, path, body, raw)
    return status, json.loads(payload or b"null")


def test_cartesia_voices_unavailable(running_server, monkeypatch):
    from lib import cartesia_voices

    def down(agents_path, *, force=False):
        raise RuntimeError("library offline")
    monkeypatch.setattr(cartesia_voices, "catalog", down)
    base, _ctx, _srv = running_server
    assert _call(base, "GET", "/cartesia-voices") == (
        502, {"error": "Cartesia voice library unavailable"})


@pytest.mark.parametrize("provider, module, function", [
    ("cartesia", "cartesia_tts", "synthesize"),
    ("elevenlabs", "eleven_ws", "synthesize_streaming"),
    ("deepgram", "deepgram_tts", "synthesize"),
    ("gemini", "gemini_tts", "synthesize"),
    ("openai", "tts_providers", "synthesize"),
])
def test_voice_preview_synthesizes_through_each_provider(
        running_server, monkeypatch, provider, module, function):
    import importlib
    from lib import voice_catalog
    voice_id = "v-" + uuid.uuid4().hex[:8]            # a fresh preview cache key per run
    monkeypatch.setattr(voice_catalog, "catalog", lambda agents, current_session="": {"providers": [
        {"id": provider, "available": True, "voices": [{"id": voice_id, "name": "Vera"}]}]})
    calls = []

    def synthesize(*args, **kwargs):
        calls.append(provider)
        kwargs["out_path"].write_bytes(f"mp3 from {provider}".encode())
    monkeypatch.setattr(importlib.import_module(f"lib.{module}"), function, synthesize)
    base, _ctx, _srv = running_server
    assert _request(base, "GET", f"/voice-preview?provider={provider}&id={voice_id}") == (
        200, f"mp3 from {provider}".encode())
    assert calls == [provider]


class _Config:
    """The real config with a Cartesia key, so narration gets past its voice check."""

    def __init__(self, real):
        self._real = real

    def __getattr__(self, name):
        return getattr(self._real, name)

    def cartesia_key(self):
        return "test-key"


@pytest.fixture
def narration_voice(monkeypatch):
    from lib import config, controller_narration
    real = config.load()
    monkeypatch.setattr(config, "load", lambda *a, **k: _Config(real))
    monkeypatch.setattr(controller_narration, "voice_id", lambda cfg: "narrator")


def test_controller_narration_without_a_voice(running_server, monkeypatch):
    from lib import controller_narration
    monkeypatch.setattr(controller_narration, "voice_id", lambda cfg: "")
    base, _ctx, _srv = running_server
    assert _request(base, "GET", "/controller-narration?text=Press%20once") == (
        503, b"narration voice unavailable")


def test_controller_narration_synthesis_failure(running_server, monkeypatch, narration_voice):
    from lib import cartesia_tts

    def refuse(**_kwargs):
        raise cartesia_tts.CartesiaError("quota exhausted")
    monkeypatch.setattr(cartesia_tts, "synthesize", refuse)
    base, _ctx, _srv = running_server
    text = "Press twice " + uuid.uuid4().hex[:6]       # never cached
    assert _request(base, "GET", "/controller-narration?text=" + text.replace(" ", "%20")) == (
        502, b"narration unavailable")


def test_controller_narration_refuses_a_symlinked_cache(running_server, narration_voice, tmp_path):
    cache = server_module._runtime_paths().cache_dir / "controller-narration"
    cache.parent.mkdir(parents=True, exist_ok=True)
    if cache.exists() or cache.is_symlink():
        pytest.skip("narration cache already present in this runtime")
    cache.symlink_to(tmp_path)
    try:
        base, _ctx, _srv = running_server
        assert _request(base, "GET", "/controller-narration?text=Hold") == (
            500, b"narration cache unavailable")
    finally:
        cache.unlink()


def test_agent_mute_refusals(running_server):
    base, _ctx, _srv = running_server
    post = lambda body=None, raw=None: _call(base, "POST", "/agent-mute", body, raw)  # noqa: E731
    assert post(raw=b"[]") == (400, {"error": "bad json"})
    assert post({"session": "claude"}) == (400, {"error": "muted required"})
    assert post({"session": "nobody", "muted": True}) == (404, {"error": "no such agent"})


def test_record_voice_never_fails_the_request(monkeypatch):
    from lib import voice_events

    def broken(event, **_kwargs):
        raise RuntimeError("timeline full")
    monkeypatch.setattr(voice_events, "record", broken)
    handler = object.__new__(server_module.Handler)
    handler._record_voice("send", session="claude", trace_id="t1")   # returns, never raises


def test_voice_query_params_parse_numbers_leniently():
    handler = object.__new__(server_module.Handler)
    handler._query = lambda: {"since": ["x"], "until": ["12.7"], "limit": ["5"], "session": [" claude "]}
    assert handler._voice_query_params() == {
        "session": "claude", "since": None, "until": 12, "utterance_id": None,
        "trace_id": None, "event": None, "limit": 5}
