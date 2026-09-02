"""Clip-id streaming endpoint contract.

The migrated live path is keyed by SQLite clip_id, not by file discovery:
TTSWorker creates a clips row, broadcasts `/clips/<id>/stream`, and appends
ElevenLabs chunks to the in-memory broker while optionally teeing to disk.
"""
from __future__ import annotations

import pathlib
import socket
import sys
import threading
import time
import urllib.request

import pytest

_SERVER_DIR = pathlib.Path(__file__).resolve().parents[2] / "server"
sys.path.insert(0, str(_SERVER_DIR))

from lib import agents as agents_db                 # noqa: E402
from lib.audio_stream import AudioStream             # noqa: E402
from lib.context import ServerContext, StubSTT       # noqa: E402
from lib.protocol import ClipProducerStatus          # noqa: E402
from lib.tts_engine import FakeTTSEngine             # noqa: E402

import importlib.util as _ilu                        # noqa: E402
_spec = _ilu.spec_from_file_location(
    "claude_pwa_server_for_clip_stream", _SERVER_DIR / "server.py")
assert _spec and _spec.loader
_srv_mod = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_srv_mod)
build_server = _srv_mod.build_server


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def running_server(tmp_path):
    static = pathlib.Path(__file__).resolve().parents[2] / "static"
    audio = tmp_path / "audio"; audio.mkdir()
    agent_id = agents_db.create_agent(
        persona="Mike", voice_id="V_MIKE",
        cwd=str(tmp_path), session="claude",
    )
    ctx = ServerContext(
        root=tmp_path, static=static, audio_dir=audio,
        agents_path=tmp_path / "agents.json",
        default_session="claude",
        tts=FakeTTSEngine(audio),
        stream=AudioStream(audio),
        stt=StubSTT(text="", ends_terminal=False),
        roster_names=("Mike",),
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
    try:
        yield base, ctx, audio, agent_id
    finally:
        srv.shutdown()
        srv.server_close()


def test_clip_stream_endpoint_forwards_live_broker_chunks(running_server):
    base, ctx, audio, agent_id = running_server
    target = audio / "live.mp3"
    clip_id = agents_db.record_clip(
        agent_id=agent_id,
        path=str(target),
        voice_id="V_MIKE",
        producer_status=ClipProducerStatus.STREAMING,
    )
    assert clip_id
    ctx.clip_broker.open(clip_id)

    result: dict = {}

    def fetch_stream():
        with urllib.request.urlopen(f"{base}/clips/{clip_id}/stream", timeout=5) as r:
            result["transfer"] = r.headers.get("Transfer-Encoding", "")
            result["body"] = r.read()

    t = threading.Thread(target=fetch_stream, daemon=True)
    t.start()
    time.sleep(0.1)
    ctx.clip_broker.append(clip_id, b"AAAA")
    ctx.clip_broker.append(clip_id, b"BBBB")
    ctx.clip_broker.finish(clip_id)
    t.join(timeout=5)

    assert result["transfer"].lower() == "chunked"
    assert result["body"] == b"AAAABBBB"


def test_clip_stream_endpoint_falls_back_to_saved_file_after_live_stream(running_server):
    base, _ctx, audio, agent_id = running_server
    target = audio / "complete.mp3"
    target.write_bytes(b"COMPLETE")
    clip_id = agents_db.record_clip(
        agent_id=agent_id,
        path=str(target),
        voice_id="V_MIKE",
        byte_count=8,
        producer_status=ClipProducerStatus.COMPLETE,
    )
    assert clip_id

    with urllib.request.urlopen(f"{base}/clips/{clip_id}/stream", timeout=5) as r:
        assert r.read() == b"COMPLETE"


def test_raw_pcm_clip_stream_uses_octet_stream_content_type(running_server):
    base, _ctx, audio, agent_id = running_server
    target = audio / "complete.pcm"
    target.write_bytes(b"\x00\x00\x00\x00")
    clip_id = agents_db.record_clip(
        agent_id=agent_id,
        path=str(target),
        voice_id="V_MIKE",
        byte_count=4,
        producer_status=ClipProducerStatus.COMPLETE,
    )
    assert clip_id

    with urllib.request.urlopen(f"{base}/clips/{clip_id}/stream", timeout=5) as r:
        assert r.headers.get("Content-Type") == "application/octet-stream"
        assert r.read() == b"\x00\x00\x00\x00"


def test_audio_endpoint_supports_range_requests(running_server):
    """iOS AVPlayer won't stream a remote audio file unless the server honors
    byte ranges — without it, plain herald mp3s failed with
    CoreMediaErrorDomain -12640 and were silent. /audio must speak 206."""
    import urllib.error
    base, _ctx, audio, _agent_id = running_server
    data = bytes(range(256)) * 8                       # 2048 deterministic bytes
    (audio / "herald.mp3").write_bytes(data)

    # Full GET: 200, advertises range support, full body.
    with urllib.request.urlopen(base + "/audio/herald.mp3", timeout=2) as r:
        assert r.status == 200
        assert r.headers.get("Accept-Ranges") == "bytes"
        assert r.read() == data

    # Range GET: 206 Partial Content + exact slice + Content-Range.
    req = urllib.request.Request(base + "/audio/herald.mp3",
                                 headers={"Range": "bytes=0-1023"})
    with urllib.request.urlopen(req, timeout=2) as r:
        assert r.status == 206
        assert r.headers.get("Content-Range") == f"bytes 0-1023/{len(data)}"
        assert r.headers.get("Accept-Ranges") == "bytes"
        assert r.read() == data[0:1024]

    # Suffix range (last N bytes) — AVPlayer uses this to read trailers.
    req = urllib.request.Request(base + "/audio/herald.mp3",
                                 headers={"Range": "bytes=-10"})
    with urllib.request.urlopen(req, timeout=2) as r:
        assert r.status == 206
        assert r.read() == data[-10:]
