"""HTTP tests for agent-published media assets."""
from __future__ import annotations

import json
import pathlib
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest

_SERVER_DIR = pathlib.Path(__file__).resolve().parents[2] / "server"
sys.path.insert(0, str(_SERVER_DIR))
from lib import db  # noqa: E402
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

PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    b"\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\nIDATx\x9cc`\x00\x00\x00\x02\x00\x01"
    b"\xe2!\xbc3\x00\x00\x00\x00IEND\xaeB`\x82"
)
AUTH = {"Authorization": "Bearer portrait-test-token"}


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
    create_agent(persona="Lena", voice_id="V_LENA",
                 cwd=str(tmp_path), session="lena-7b4b")
    ctx = ServerContext(
        root=tmp_path,
        static=static,
        audio_dir=audio,
        agents_path=tmp_path / "agents.json",
        default_session="lena-7b4b",
        uploads_dir=tmp_path / "uploads",
        media_dir=tmp_path / "media",
        tts=FakeTTSEngine(audio),
        stream=AudioStream(audio),
        stt=StubSTT(text="hi", ends_terminal=True),
        roster_names=("Lena",),
        auth_token="portrait-test-token",
    )
    port = _free_port()
    srv = build_server(ctx, port, bind_addr="127.0.0.1")
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            urllib.request.urlopen(urllib.request.Request(
                base + "/agents/snapshot", headers=AUTH), timeout=0.2).read()
            break
        except Exception:
            time.sleep(0.02)
    yield base, tmp_path / "media"
    srv.shutdown()
    srv.server_close()


def _publish(base, blob=PNG_1X1, *, session="lena-7b4b", ctype="image/png"):
    headers = {
        **AUTH,
        "Content-Type": ctype,
        "X-Session": session,
        "X-File-Name": urllib.parse.quote("style.png"),
        "X-Caption": urllib.parse.quote("Style option"),
    }
    req = urllib.request.Request(base + "/media", data=blob, method="POST",
                                 headers=headers)
    with urllib.request.urlopen(req, timeout=3) as resp:
        return resp.status, json.loads(resp.read())


def _upload(base, blob=b"document", *, upload_id="share-job:0",
            name="report.txt", ctype="text/plain", session="lena-7b4b"):
    req = urllib.request.Request(
        base + "/upload", data=blob, method="POST",
        headers={
            **AUTH,
            "Content-Type": ctype,
            "X-Session": session,
            "X-File-Name": urllib.parse.quote(name),
            "X-Upload-ID": upload_id,
        })
    with urllib.request.urlopen(req, timeout=3) as response:
        return response.status, json.loads(response.read())


def test_media_publish_indexes_blob_and_serves_content(running_server):
    base, media_dir = running_server
    status, out = _publish(base)

    assert status == 200
    assert out["ok"] is True
    asset = out["asset"]
    assert asset["session"] == "lena-7b4b"
    assert asset["mime_type"] == "image/png"
    assert asset["width"] == 1
    assert asset["height"] == 1
    assert asset["markdown"].startswith("![Style option](clarp-media://asset/")

    row = db.conn().execute(
        "SELECT storage_path, caption FROM media_assets WHERE asset_id = ?",
        (asset["asset_id"],),
    ).fetchone()
    assert row is not None
    assert row["caption"] == "Style option"
    saved = pathlib.Path(row["storage_path"])
    assert saved.exists()
    assert media_dir.resolve() in saved.resolve().parents

    with urllib.request.urlopen(urllib.request.Request(
        base + asset["url"], headers=AUTH), timeout=3) as resp:
        assert resp.status == 200
        assert resp.headers["Content-Type"] == "image/png"
        assert resp.read() == PNG_1X1


def test_upload_idempotency_returns_one_server_file(running_server):
    base, media_dir = running_server
    first_status, first = _upload(base)
    second_status, second = _upload(base)

    assert first_status == second_status == 200
    assert second == first
    saved = pathlib.Path(first["path"])
    assert saved.read_bytes() == b"document"
    upload_dir = media_dir.parent / "uploads" / "lena-7b4b"
    assert list(upload_dir.glob("u-*-report.txt")) == [saved]


def test_upload_id_collision_returns_409(running_server):
    base, _ = running_server
    _upload(base)
    with pytest.raises(urllib.error.HTTPError) as error:
        _upload(base, blob=b"different")
    assert error.value.code == 409


def test_upload_id_cannot_be_reused_for_another_session(running_server):
    base, _ = running_server
    _upload(base)
    with pytest.raises(urllib.error.HTTPError) as error:
        _upload(base, session="other")
    assert error.value.code == 409


def test_media_gallery_lists_session_assets(running_server):
    base, _ = running_server
    _, out = _publish(base)

    with urllib.request.urlopen(urllib.request.Request(
        base + "/media?session=lena-7b4b&limit=10", headers=AUTH
    ), timeout=3) as resp:
        body = json.loads(resp.read())

    assert body["session"] == "lena-7b4b"
    assert [asset["asset_id"] for asset in body["assets"]] == [out["asset_id"]]


def test_media_publisher_keeps_single_image_and_gallery_as_media(running_server, tmp_path):
    base, _ = running_server
    one = tmp_path / "one.png"; two = tmp_path / "two.png"
    one.write_bytes(PNG_1X1); two.write_bytes(PNG_1X1)
    script = pathlib.Path(__file__).resolve().parents[2] / "scripts/clarp-media-publish.py"
    single = subprocess.run([
        sys.executable, str(script), "--session", "lena-7b4b", "--base-url", base,
        "--token", "portrait-test-token", "--json", str(one),
    ], text=True, capture_output=True, check=True)
    single_output = json.loads(single.stdout)
    assert "artifact" not in single_output
    assert single_output["asset"]["markdown"].startswith("![")
    gallery = subprocess.run([
        sys.executable, str(script), "--session", "lena-7b4b", "--base-url", base,
        "--token", "portrait-test-token", "--json", "--gallery", str(one), str(two),
    ], text=True, capture_output=True, check=True)
    gallery_output = json.loads(gallery.stdout)
    assert all("artifact" not in item for item in gallery_output["assets"])
    assert all(item["asset"]["markdown"].startswith("![") for item in gallery_output["assets"])


def test_media_publish_rejects_unknown_session(running_server):
    base, _ = running_server
    with pytest.raises(urllib.error.HTTPError) as ei:
        _publish(base, session="missing")
    assert ei.value.code == 404


def test_media_publish_accepts_allowlisted_artifact_files(running_server):
    base, _ = running_server
    status, out = _publish(base, blob=b"%PDF-1.7\nshowcase", ctype="application/pdf")
    assert status == 200
    assert out["asset"]["mime_type"] == "application/pdf"
    assert out["asset"]["markdown"] == ""
    with urllib.request.urlopen(urllib.request.Request(
        base + out["asset"]["url"], headers=AUTH), timeout=3) as resp:
        assert resp.headers["Content-Type"] == "application/pdf"
        assert resp.read().startswith(b"%PDF-")
    with urllib.request.urlopen(urllib.request.Request(
        base + "/media?session=lena-7b4b&limit=10", headers=AUTH), timeout=3) as resp:
        assert json.loads(resp.read())["assets"] == []


def test_media_publish_rejects_unsafe_file_type(running_server):
    base, _ = running_server
    with pytest.raises(urllib.error.HTTPError) as ei:
        _publish(base, blob=b"\x7fELF", ctype="application/x-executable")
    assert ei.value.code == 400


def test_portrait_endpoint_is_authenticated_and_selects_media_primary(running_server):
    base, _ = running_server
    _, published = _publish(base)
    with pytest.raises(urllib.error.HTTPError) as unauthorized:
        urllib.request.urlopen(
            base + "/agent-portraits?session=lena-7b4b", timeout=3)
    assert unauthorized.value.code == 401

    add_body = json.dumps({
        "action": "add_media_asset",
        "session": "lena-7b4b",
        "asset_id": published["asset_id"],
    }).encode()
    add_request = urllib.request.Request(
        base + "/agent-portraits", data=add_body, method="POST",
        headers={**AUTH, "Content-Type": "application/json"})
    with urllib.request.urlopen(add_request, timeout=3) as response:
        added = json.loads(response.read())
    portrait = added["portraits"][0]
    assert portrait["media_asset_id"] == published["asset_id"]
    assert portrait["role"] == "alternate"

    select_body = json.dumps({
        "action": "select_primary",
        "session": "lena-7b4b",
        "portrait_id": portrait["portrait_id"],
    }).encode()
    select_request = urllib.request.Request(
        base + "/agent-portraits", data=select_body, method="POST",
        headers={**AUTH, "Content-Type": "application/json"})
    with urllib.request.urlopen(select_request, timeout=3) as response:
        selected = json.loads(response.read())
    assert selected["primary_portrait_id"] == portrait["portrait_id"]

    portrait_url = next(
        row["url"] for row in selected["portraits"]
        if row["portrait_id"] == portrait["portrait_id"])
    with urllib.request.urlopen(urllib.request.Request(
        base + portrait_url, headers=AUTH), timeout=3) as response:
        assert response.read() == PNG_1X1


def test_portrait_generation_rejects_non_object_json(running_server):
    base, _ = running_server
    request = urllib.request.Request(
        base + "/agent-portrait-generation", data=b"[]", method="POST",
        headers={**AUTH, "Content-Type": "application/json"})

    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(request, timeout=3)

    assert error.value.code == 400
