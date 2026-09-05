"""HTTP tests for the model-portrait preference and the art it points at."""
from __future__ import annotations

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

import importlib.util as _ilu  # noqa: E402
_spec = _ilu.spec_from_file_location("claude_pwa_server", _SERVER_DIR / "server.py")
assert _spec and _spec.loader
server_module = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(server_module)
server_module.BIND_ADDR = "127.0.0.1"
build_server = server_module.build_server

AUTH = {"Authorization": "Bearer avatar-test-token"}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def running_server(tmp_path):
    static = pathlib.Path(__file__).resolve().parents[2] / "static"
    audio = tmp_path / "audio"
    audio.mkdir()
    from lib.agents import create_agent
    create_agent(persona="Rachel", voice_id="V_RACHEL", cwd=str(tmp_path),
                 session="rachel-7b4b", backend="claude", model="claude-opus-5")
    ctx = ServerContext(
        root=tmp_path,
        static=static,
        audio_dir=audio,
        agents_path=tmp_path / "agents.json",
        default_session="rachel-7b4b",
        uploads_dir=tmp_path / "uploads",
        media_dir=tmp_path / "media",
        tts=FakeTTSEngine(audio),
        stream=AudioStream(audio),
        stt=StubSTT(text="hi", ends_terminal=True),
        roster_names=("Rachel",),
        auth_token="avatar-test-token",
    )
    port = _free_port()
    srv = build_server(ctx, port, bind_addr="127.0.0.1")
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            urllib.request.urlopen(urllib.request.Request(
                base + "/agents/snapshot", headers=AUTH), timeout=0.2).read()
            break
        except Exception:
            time.sleep(0.02)
    yield base
    srv.shutdown()
    srv.server_close()


def _get(base, path):
    request = urllib.request.Request(base + path, headers=AUTH)
    with urllib.request.urlopen(request, timeout=3) as response:
        return response.status, json.loads(response.read())


def _post(base, path, payload):
    request = urllib.request.Request(
        base + path, data=json.dumps(payload).encode(), method="POST",
        headers={**AUTH, "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=3) as response:
        return response.status, json.loads(response.read())


def test_the_preference_round_trips_and_reaches_the_snapshot(running_server):
    base = running_server

    assert _get(base, "/avatar-settings") == (200, {"model_avatars": False})
    assert _post(base, "/avatar-settings", {"model_avatars": True}) == (
        200, {"model_avatars": True})
    assert _get(base, "/avatar-settings") == (200, {"model_avatars": True})

    _, snapshot = _get(base, "/agents/snapshot")
    assert snapshot["model_avatars"] is True


def test_a_non_boolean_preference_is_refused(running_server):
    with pytest.raises(urllib.error.HTTPError) as raised:
        _post(running_server, "/avatar-settings", {"model_avatars": "yes"})
    assert raised.value.code == 400


def test_the_snapshot_points_at_bundled_art_the_server_actually_serves(running_server):
    """The URL is only useful if the file behind it comes back."""
    base = running_server
    _, snapshot = _get(base, "/agents/snapshot")
    url = snapshot["agents"][0]["model_avatar_url"]
    assert url.startswith("/static/avatars/models/rachel.opus.png?v=")

    with urllib.request.urlopen(base + url, timeout=3) as response:
        assert response.status == 200
        assert response.read(8) == b"\x89PNG\r\n\x1a\n"
