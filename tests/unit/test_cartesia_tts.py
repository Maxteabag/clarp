from __future__ import annotations

import pathlib
import sys

import pytest

_SERVER_DIR = pathlib.Path(__file__).resolve().parents[2] / "server"
sys.path.insert(0, str(_SERVER_DIR))

from lib import cartesia_tts  # noqa: E402


class _FakeStreamingResponse:
    def __init__(self, chunks: list[bytes]):
        self._chunks = list(chunks)
        self.read_sizes: list[int] = []

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        assert size > 0, "Cartesia bytes response must be read incrementally"
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


def test_cartesia_bytes_streams_chunks_to_callback_and_file(monkeypatch, tmp_path):
    response = _FakeStreamingResponse([b"abc", b"defg"])

    def fake_urlopen(_req, timeout):
        assert timeout == 30.0
        return response

    monkeypatch.setattr(cartesia_tts.urllib.request, "urlopen", fake_urlopen)

    out = tmp_path / "clip.mp3"
    observed: list[tuple[int, bytes, int]] = []

    def on_chunk(idx: int, data: bytes) -> None:
        observed.append((idx, data, out.stat().st_size if out.exists() else 0))

    n = cartesia_tts.synthesize(
        text="hello",
        voice_id="voice",
        out_path=out,
        api_key="key",
        on_chunk=on_chunk,
    )

    assert n == 7
    assert out.read_bytes() == b"abcdefg"
    assert observed == [
        (0, b"abc", 0),
        (1, b"defg", 3),
    ]
    assert response.read_sizes == [
        cartesia_tts._CHUNK_BYTES,
        cartesia_tts._CHUNK_BYTES,
        cartesia_tts._CHUNK_BYTES,
    ]


def test_cartesia_empty_stream_raises_and_removes_empty_file(monkeypatch, tmp_path):
    response = _FakeStreamingResponse([])
    monkeypatch.setattr(
        cartesia_tts.urllib.request,
        "urlopen",
        lambda *_args, **_kw: response,
    )

    out = tmp_path / "empty.mp3"
    with pytest.raises(cartesia_tts.CartesiaError, match="empty response body"):
        cartesia_tts.synthesize(
            text="hello",
            voice_id="voice",
            out_path=out,
            api_key="key",
        )

    assert not out.exists()
