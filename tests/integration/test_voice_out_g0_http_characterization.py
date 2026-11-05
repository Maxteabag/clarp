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
from types import SimpleNamespace

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
    import dataclasses
    import importlib
    import sys
    from lib import config, voice_catalog
    monkeypatch.setattr(config, "_CACHED", dataclasses.replace(
        config.load(), cartesia_api_key="synthetic-key", eleven_api_key="synthetic-key"))
    voice_id = "v-" + uuid.uuid4().hex[:8]            # a fresh preview cache key per run
    monkeypatch.setattr(voice_catalog, "catalog", lambda agents, current_session="": {"providers": [
        {"id": provider, "available": True, "voices": [{"id": voice_id, "name": "Vera"}]}]})
    calls = []

    def synthesize(*args, **kwargs):
        calls.append(provider)
        kwargs["out_path"].write_bytes(f"mp3 from {provider}".encode())
    monkeypatch.setattr(importlib.import_module(f"lib.{module}"), function, synthesize)
    registry = sys.modules.get("lib.tts_registry")
    binding = {"cartesia": "cartesia_synthesize", "elevenlabs": "synthesize_streaming"}.get(provider)
    if registry is not None and binding:
        monkeypatch.setattr(registry, binding, synthesize)
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
    import sys
    registry = sys.modules.get("lib.tts_registry")
    if registry is not None:
        monkeypatch.setattr(registry, "cartesia_synthesize", refuse)
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


@pytest.fixture
def fallback_preview(running_server, monkeypatch, tmp_path):
    """Real HTTP routes; synthetic providers with controllable outage/recovery."""
    import dataclasses
    import sys
    from lib import config, controller_narration, cartesia_tts, cartesia_voices, deepgram_tts, voice_catalog
    from lib.paths import RuntimePaths
    base, _ctx, _srv = running_server
    cache = tmp_path / "cache"
    monkeypatch.setattr(RuntimePaths, "from_home", classmethod(lambda cls, home: SimpleNamespace(cache_dir=cache)))
    monkeypatch.setattr(config, "_CACHED", dataclasses.replace(
        config.load(), tts_provider="cartesia", tts_fallback="deepgram",
        cartesia_api_key="synthetic-primary-key", deepgram_api_key="synthetic-fallback-key",
        cartesia_voices={"Rachel": "cart-primary"}))
    monkeypatch.setattr(controller_narration, "voice_id", lambda cfg: "cart-primary")
    monkeypatch.setattr(cartesia_voices, "cached_english_voice", lambda voice: {"id": "cart-primary", "name": "Ada"})
    monkeypatch.setattr(cartesia_voices, "has_cached_catalog", lambda: True)
    monkeypatch.setattr(voice_catalog, "catalog", lambda *a, **kw: {"providers": [
        {"id": "cartesia", "available": True, "voices": [{"id": "cart-primary", "name": "Ada"}]}]})
    state = {"primary_down": True, "fallback_down": False, "calls": [],
             "primary_error": cartesia_tts.CartesiaError("primary outage")}

    def primary(*, out_path, **kw):
        state["calls"].append("cartesia")
        if state["primary_down"]:
            raise state["primary_error"]
        out_path.write_bytes(b"ID3primary")
        return len(b"ID3primary")

    def fallback(*, out_path, **kw):
        state["calls"].append("deepgram")
        if state["fallback_down"]:
            raise RuntimeError("fallback outage")
        out_path.write_bytes(b"ID3fallback")
        return len(b"ID3fallback")
    monkeypatch.setattr(cartesia_tts, "synthesize", primary)
    registry = sys.modules.get("lib.tts_registry")
    if registry is not None:
        monkeypatch.setattr(registry, "cartesia_synthesize", primary)
    monkeypatch.setattr(deepgram_tts, "synthesize", fallback)
    return base, cache, state


# Cache ownership and primary recovery are new behavior with real HTTP/file
# consequences, beyond the provider-routing table's one-call outcomes.
@pytest.mark.parametrize("path", [
    "/voice-preview?provider=cartesia&id=cart-primary",
    "/cartesia-voice-preview?id=cart-primary",
    "/controller-narration?text=Press%20once",
])
def test_fallback_audio_never_poisons_the_requested_voice_cache(fallback_preview, path):
    base, cache, state = fallback_preview
    with urllib.request.urlopen(base + path, timeout=10) as response:
        assert response.status == 200
        assert response.headers.get_content_type() == "audio/mpeg"
        assert response.read() == b"ID3fallback"
    assert not list(cache.rglob("*.mp3")), "fallback bytes must not enter a primary voice cache"
    # Reading Content-Length bytes can finish before the handler returns from
    # _send_file and unlinks its temporary in finally.
    import time
    cleanup_deadline = time.monotonic() + 5
    while list(cache.rglob("*.tmp")) and time.monotonic() < cleanup_deadline:
        time.sleep(0.01)
    assert not list(cache.rglob("*.tmp")), "served fallback temporary is removed"
    state["primary_down"] = False
    assert _request(base, "GET", path) == (200, b"ID3primary")
    assert _request(base, "GET", path) == (200, b"ID3primary")
    assert state["calls"] == ["cartesia", "deepgram", "cartesia"]
    assert len(list(cache.rglob("*.mp3"))) == 1


@pytest.mark.parametrize("path, message, method", [
    ("/voice-preview?provider=cartesia&id=cart-primary", b"preview unavailable", "_handle_voice_preview"),
    ("/cartesia-voice-preview?id=cart-primary", b"preview unavailable", "_handle_cartesia_voice_preview"),
    ("/controller-narration?text=Press%20once", b"narration unavailable", "_handle_controller_narration"),
])
def test_failed_fallback_keeps_the_primary_route_error(fallback_preview, monkeypatch, path, message, method):
    base, cache, state = fallback_preview
    state["fallback_down"] = True
    errors = []
    monkeypatch.setitem(getattr(server_module.Handler, method).__globals__, "log_exception",
                        lambda event, error, **kw: errors.append(error))
    assert _request(base, "GET", path) == (502, message)
    assert state["calls"] == ["cartesia", "deepgram"]
    assert errors[-1] is state["primary_error"]
    assert not list(cache.rglob("*.mp3")) and not list(cache.rglob("*.tmp"))


def test_get_preview_refuses_an_unusable_voice_before_fallback(running_server, monkeypatch):
    import dataclasses
    from lib import config, deepgram_tts, google_tts, voice_catalog
    base, _ctx, _srv = running_server
    voice = "nb-NO-Chirp3-HD-Aoede"
    monkeypatch.setattr(config, "_CACHED", dataclasses.replace(
        config.load(), tts_fallback="deepgram", deepgram_api_key="synthetic-key"))
    monkeypatch.setattr(voice_catalog, "catalog", lambda *a, **kw: {"providers": [
        {"id": "google", "available": True, "voices": [{"id": voice, "name": "Ada"}]}]})
    monkeypatch.setattr(google_tts, "collision_code", lambda *a: None)
    monkeypatch.setattr(google_tts, "credentials", lambda *a: "synthetic-credentials")
    calls = []

    def unavailable(**kw):
        calls.append("google")
        raise google_tts.GoogleTTSError("voice_unavailable")
    monkeypatch.setattr(google_tts, "synthesize", unavailable)
    monkeypatch.setattr(deepgram_tts, "synthesize", lambda **kw: calls.append("deepgram"))
    assert _request(base, "GET", f"/voice-preview?provider=google&id={voice}") == (502, b"preview unavailable")
    assert calls == ["google"]
