from __future__ import annotations

import base64
import json
import pathlib
import sys

_SERVER_DIR = pathlib.Path(__file__).resolve().parents[2] / "server"
sys.path.insert(0, str(_SERVER_DIR))

from lib import cartesia_ws  # noqa: E402


class _FakeWS:
    def __init__(self):
        self.sent: list[dict] = []
        self.frames = [
            json.dumps({
                "type": "chunk",
                "data": base64.b64encode(b"pcm-a").decode(),
                "done": False,
                "status_code": 206,
                "context_id": "ctx",
            }),
            json.dumps({
                "type": "chunk",
                "data": base64.b64encode(b"pcm-b").decode(),
                "done": True,
                "status_code": 206,
                "context_id": "ctx",
            }),
        ]
        self.closed = False

    def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    def recv(self) -> str:
        return self.frames.pop(0)

    def close(self) -> None:
        self.closed = True


def test_cartesia_websocket_streams_raw_pcm_chunks(monkeypatch, tmp_path):
    fake = _FakeWS()
    captured = {}

    def fake_open(url, **kw):
        captured["url"] = url
        captured["header"] = kw["header"]
        return fake

    monkeypatch.setattr(cartesia_ws, "_open_ws", fake_open)
    out = tmp_path / "clip.pcm"
    chunks: list[tuple[int, bytes]] = []

    total = cartesia_ws.synthesize_raw_pcm(
        text="hello",
        voice_id="voice",
        out_path=out,
        api_key="key",
        model="sonic-3.5",
        on_chunk=lambda idx, chunk: chunks.append((idx, chunk)),
    )

    assert "cartesia_version=2026-03-01" in captured["url"]
    assert captured["header"] == ["X-API-Key: key"]
    assert fake.sent[0]["output_format"] == {
        "container": "raw",
        "encoding": "pcm_f32le",
        "sample_rate": 44100,
    }
    assert fake.sent[0]["continue"] is False
    assert fake.sent[0]["max_buffer_delay_ms"] == 0
    assert chunks == [(0, b"pcm-a"), (1, b"pcm-b")]
    assert out.read_bytes() == b"pcm-apcm-b"
    assert total == len(b"pcm-apcm-b")
    assert fake.closed is True

