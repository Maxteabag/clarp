"""HTTP tests for POST /upload — the client (phone) ships a raw file body and
the server saves it under a per-session uploads dir, returning the absolute
path the client drops into the prompt.

Runs against a real ThreadingHTTPServer with a fully-stubbed ServerContext
whose uploads_dir is a tmp path, so nothing touches ~/.cache.
"""
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


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def uploads_dir(tmp_path):
    return tmp_path / "uploads"


@pytest.fixture
def running_server(tmp_path, uploads_dir):
    static = pathlib.Path(__file__).resolve().parents[2] / "static"
    audio = tmp_path / "audio"
    audio.mkdir()
    from lib.agents import create_agent
    create_agent(persona="Rachel", voice_id="V_RACHEL",
                 cwd=str(tmp_path), session="rachel")
    ctx = ServerContext(
        root=tmp_path,
        static=static,
        audio_dir=audio,
        agents_path=tmp_path / "agents.json",
        default_session="rachel",
        uploads_dir=uploads_dir,
        tts=FakeTTSEngine(audio),
        stream=AudioStream(audio),
        stt=StubSTT(text="hi", ends_terminal=True),
        roster_names=("Rachel",),
    )
    port = _free_port()
    srv = build_server(ctx, port, bind_addr="127.0.0.1")
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
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


def _upload(base, blob, *, name, session=None, ctype="application/octet-stream"):
    headers = {"Content-Type": ctype, "X-File-Name": name}
    if session is not None:
        headers["X-Session"] = session
    req = urllib.request.Request(base + "/upload", data=blob, method="POST",
                                 headers=headers)
    with urllib.request.urlopen(req, timeout=3) as resp:
        return resp.status, json.loads(resp.read())


def test_upload_writes_file_and_returns_absolute_path(running_server, uploads_dir):
    base = running_server
    blob = b"\x89PNG\r\n\x1a\n fake image bytes"
    status, out = _upload(base, blob, name="photo.png", session="rachel",
                          ctype="image/png")

    assert status == 200
    assert out["ok"] is True
    assert out["size"] == len(blob)
    assert out["name"] == "photo.png"

    saved = pathlib.Path(out["path"])
    assert saved.is_absolute()
    assert saved.exists()
    assert saved.read_bytes() == blob
    # Lands under the injected per-session uploads dir, not ~/.cache.
    assert saved.parent == uploads_dir / "rachel"
    # Unique token prefix keeps the original name as the suffix.
    assert saved.name.endswith("-photo.png")


def test_upload_blocks_path_traversal(running_server, uploads_dir):
    base = running_server
    status, out = _upload(base, b"data", name="../../etc/evil.png",
                          session="rachel", ctype="image/png")

    assert status == 200
    saved = pathlib.Path(out["path"])
    # The traversal components are stripped — it stays inside the session dir.
    assert saved.parent == uploads_dir / "rachel"
    assert out["name"] == "evil.png"
    assert ".." not in saved.parts


def test_upload_falls_back_to_default_session(running_server, uploads_dir):
    base = running_server
    # No X-Session header → server uses the focused/default session.
    status, out = _upload(base, b"x", name="note.txt")
    assert status == 200
    assert pathlib.Path(out["path"]).exists()


def test_upload_names_unknown_extension_from_content_type(running_server):
    base = running_server
    status, out = _upload(base, b"x", name="screenshot", ctype="image/jpeg")
    assert status == 200
    assert out["name"].endswith(".jpg") or out["name"].endswith(".jpeg")


def test_upload_rejects_empty_body(running_server):
    base = running_server
    req = urllib.request.Request(base + "/upload", data=b"", method="POST",
                                 headers={"X-File-Name": "x.png"})
    with pytest.raises(urllib.error.HTTPError) as ei:
        urllib.request.urlopen(req, timeout=3)
    assert ei.value.code == 400


def test_safe_upload_name_unit():
    f = server_module._safe_upload_name
    assert f("../../evil.sh") == "evil.sh"
    assert f("my photo.png") == "my_photo.png"
    assert f("", "image/png") == "upload.png"
    assert f("noext", "image/jpeg") in {"noext.jpg", "noext.jpeg"}
    assert "/" not in f("a/b/c.png")
