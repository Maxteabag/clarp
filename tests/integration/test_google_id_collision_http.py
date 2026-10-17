"""The google-id collision at the HTTP surface: the real server in-process, a disposable config file,
an owned adapter root with a synthetic legacy "google" package (never executed), a recording restart
boundary and a recording Google transport. No provider, credential, restart or live config.
"""
from __future__ import annotations

import hashlib
import importlib.util as _ilu
import json
import pathlib
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

_SERVER_DIR = pathlib.Path(__file__).resolve().parents[2] / "server"
sys.path.insert(0, str(_SERVER_DIR))
from lib.audio_stream import AudioStream  # noqa: E402
from lib.context import ServerContext, StubSTT  # noqa: E402
from lib.tts_engine import FakeTTSEngine  # noqa: E402

_spec = _ilu.spec_from_file_location("collision_http_server", _SERVER_DIR / "server.py")
_server_module = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_server_module)

COLLISION = "google_id_collision"
AOEDE = "nb-NO-Chirp3-HD-Aoede"
LEGACY = {"schema_version": 1, "id": "google", "name": "Legacy Google", "description": "a 64 adapter",
          "executable": "./adapter", "operations": ["voices", "preview", "synthesize"],
          "audio_format": "audio/mpeg", "default_voice": "legacy-voice", "can_fallback": True}


@pytest.fixture
def adapters(tmp_path, monkeypatch):
    from lib import custom_tts_adapters
    root = tmp_path / "tts-adapters.d"
    package = root / "legacy-google"
    package.mkdir(parents=True)
    (package / "adapter").write_text("#!/bin/sh\nexit 1\n")      # never executed
    (package / "adapter").chmod(0o755)
    (package / "manifest.json").write_text(json.dumps(LEGACY))
    monkeypatch.setattr(custom_tts_adapters, "ROOT", root)
    return root


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    from lib import config
    path = tmp_path / "disposable-config.toml"
    placeholder = tmp_path / "adc-placeholder.json"
    placeholder.write_text("{}")
    path.write_text('[tts]\nprovider = "cartesia"\nfallback = "none"\n\n'
                    f'[google_tts]\ncredentials = "{placeholder}"\nproject = "fixture-project"\n')
    path.chmod(0o600)
    monkeypatch.setenv("CLAUDE_PWA_CONFIG", str(path))
    config.reset_cache()
    return path


@pytest.fixture
def boundaries(monkeypatch):
    """No live catalogue, no real restart (recorded), Google synthesis recorded (never the network)."""
    from lib import cartesia_voices, deepgram_voices, elevenlabs_voices, google_tts, service_manager
    for module in (cartesia_voices, deepgram_voices, elevenlabs_voices):
        monkeypatch.setattr(module, "english_voices", lambda *a, **k: [])
    restarts, google_calls = [], []
    monkeypatch.setattr(service_manager, "restart", lambda **kw: restarts.append(kw))
    real_timer = threading.Timer

    def timer(interval, function, *args, **kwargs):
        if sys._getframe(1).f_code.co_name == "_handle_tts_providers_post":
            function()
            return type("Done", (), {"start": lambda self: None, "daemon": False})()
        return real_timer(interval, function, *args, **kwargs)
    monkeypatch.setattr(threading, "Timer", timer)
    real_synthesize = google_tts.synthesize

    def recording(*, text, voice, out_path, creds, **kwargs):
        google_tts.refuse_if_collision()            # the real first statement still applies
        google_calls.append(voice)
        raise google_tts.GoogleTTSError("google_quota")
    monkeypatch.setattr(google_tts, "synthesize", recording)
    return {"restarts": restarts, "google": google_calls, "real": real_synthesize}


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def host(tmp_path, adapters, config_file, boundaries):
    from lib import agents
    audio = tmp_path / "audio"
    audio.mkdir()
    agents.create_agent(persona="Mike", voice_id=json.dumps({"google": AOEDE}), cwd=str(tmp_path), session="claude")
    ctx = ServerContext(
        root=tmp_path, static=pathlib.Path(__file__).resolve().parents[2] / "static",
        audio_dir=audio, agents_path=tmp_path / "agents.json", default_session="claude",
        tts=FakeTTSEngine(audio), stream=AudioStream(audio),
        stt=StubSTT(text="", ends_terminal=True), roster_names=("Mike",))
    port = _free_port()
    srv = _server_module.build_server(ctx, port, bind_addr="127.0.0.1")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            urllib.request.urlopen(base + "/agents/snapshot", timeout=0.2).read()
            break
        except Exception:
            time.sleep(0.02)
    yield base
    srv.shutdown()
    srv.server_close()


def _call(base, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("body", [{"provider": "google", "fallback": "none"},
                                  {"provider": "cartesia", "fallback": "google"}], ids=["primary", "fallback"])
def test_selecting_google_is_409_with_the_code_and_changes_nothing(host, config_file, boundaries, body):
    before = _sha(config_file)
    status, raw = _call(host, "POST", "/tts/providers", body)
    assert status == 409 and json.loads(raw) == {"error": COLLISION}
    assert _sha(config_file) == before and boundaries["restarts"] == []


def test_a_non_google_selection_still_works(host, config_file):
    status, raw = _call(host, "POST", "/tts/providers", {"provider": "cartesia", "fallback": "none"})
    assert status == 200 and json.loads(raw)["provider"] == "cartesia"


def test_choosing_a_google_voice_is_409_before_the_allowlist(host):
    status, raw = _call(host, "POST", "/agent-voice",
                        {"session": "claude", "voice_id": "not-on-the-allowlist", "provider": "google"})
    assert status == 409 and json.loads(raw) == {"error": COLLISION}


def test_a_google_preview_is_409_even_when_a_cached_preview_exists(host, boundaries):
    prompt = "Hello, I'm Aoede. This is how I'll sound in Clarp."
    key = hashlib.sha256(f"v3\0google\0{AOEDE}\0\0{prompt}".encode()).hexdigest()
    cache = _server_module._runtime_paths().cache_dir / "voice-previews"
    cache.mkdir(parents=True, exist_ok=True)
    cached = cache / f"{key}.mp3"
    cached.write_bytes(b"ID3cached-before-the-collision")
    try:
        status, raw = _call(host, "GET", f"/voice-preview?provider=google&id={AOEDE}")
        assert status == 409 and json.loads(raw) == {"error": COLLISION}
        assert cached.read_bytes() == b"ID3cached-before-the-collision"
        assert boundaries["google"] == []
    finally:
        cached.unlink(missing_ok=True)


def test_without_a_collision_preview_statuses_are_unchanged(host, adapters, boundaries):
    for child in (adapters / "legacy-google").iterdir():
        child.unlink()
    (adapters / "legacy-google").rmdir()
    status, _ = _call(host, "GET", "/voice-preview?provider=google&id=nb-NO-Chirp3-HD-Nobody")
    assert status == 404                                       # unknown voice, as today
    status, _ = _call(host, "GET", f"/voice-preview?provider=google&id={AOEDE}")
    assert status == 502 and boundaries["google"] == [AOEDE]   # provider failure, as today


def test_listings_carry_only_the_fixed_code_and_the_required_row_shape(host, adapters):
    status, raw = _call(host, "GET", "/tts/providers")
    assert status == 200 and str(adapters) not in raw.decode()
    google = next(r for r in json.loads(raw)["providers"] if r["id"] == "google")
    assert google["available"] is False and google["error"] == COLLISION
    status, raw = _call(host, "GET", "/voice-catalog?for=claude")
    assert status == 200 and str(adapters) not in raw.decode()
    provider = next(p for p in json.loads(raw)["providers"] if p["id"] == "google")
    assert provider["error"] == COLLISION
    for row in provider["voices"]:
        assert row["current"] is False and row["taken_by"] is None
