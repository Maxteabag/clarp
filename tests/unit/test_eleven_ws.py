"""Tests for the ElevenLabs WebSocket client (lib/eleven_ws.py).

The unit under test opens a WS to ElevenLabs, sends init+text+EOS,
receives base64-encoded audio chunks, decodes them, and pipes them
to a callback (or directly to a file). We mock the WS client so the
tests don't need network — and stay deterministic.

Wire format (per ElevenLabs realtime API):
  client → server:
    1) {"text": " ", "xi_api_key": "<key>", "voice_settings": {...},
        "generation_config": {"chunk_length_schedule": [...]}}
    2) {"text": "actual content to synthesize"}
    3) {"text": ""}                                  ← EOS marker
  server → client:
    {"audio": "<base64-mp3>", "isFinal": false}     ← N of these
    {"audio": "<base64-mp3>", "isFinal": true}      ← last one
"""
from __future__ import annotations

import base64
import json
import pathlib
import sys

import pytest

_SERVER_DIR = pathlib.Path(__file__).resolve().parents[2] / "server"
sys.path.insert(0, str(_SERVER_DIR))


# ---- Mock WebSocket ----------------------------------------------------


class FakeServerScript:
    """Pre-programmed sequence of server-side responses. Each entry is the
    JSON the server will return when the client calls recv() next."""
    def __init__(self, frames: list[dict]):
        self._frames = list(frames)
        self.closed = False

    def next_frame(self) -> str:
        if not self._frames:
            raise StopIteration()
        return json.dumps(self._frames.pop(0))


class FakeWS:
    """In-memory WebSocket double. Captures everything the client sends,
    serves pre-programmed server responses."""
    def __init__(self, script: FakeServerScript):
        self.script = script
        self.sent_messages: list[dict] = []
        self.closed = False

    def send(self, raw: str) -> None:
        if self.closed:
            raise RuntimeError("send on closed ws")
        self.sent_messages.append(json.loads(raw))

    def recv(self) -> str:
        if self.closed:
            raise RuntimeError("recv on closed ws")
        try:
            return self.script.next_frame()
        except StopIteration:
            # Mimic the underlying websocket-client behaviour: when the
            # server closes the connection, recv() raises.
            self.closed = True
            raise ConnectionResetError("server closed connection")

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def mock_ws(monkeypatch):
    """Replace eleven_ws's WebSocket factory with our FakeWS. Yields a
    function the test calls to set up the canned server script."""
    holder: dict = {"ws": None, "url": None}

    def make_factory(script: FakeServerScript):
        def factory(url, **kw):
            ws = FakeWS(script)
            holder["ws"] = ws
            holder["url"] = url
            return ws
        return factory

    def install(script: FakeServerScript):
        from lib import eleven_ws
        monkeypatch.setattr(eleven_ws, "_open_ws",
                            make_factory(script))
        return holder

    return install


# ---- Tests --------------------------------------------------------------


def test_synth_streaming_opens_ws_to_correct_url(mock_ws, tmp_path):
    """URL must point at /v1/text-to-speech/<voice>/stream-input and
    carry the model_id + output_format query params."""
    from lib.eleven_ws import synthesize_streaming
    script = FakeServerScript([
        {"audio": base64.b64encode(b"\xff\xfb").decode(), "isFinal": True},
    ])
    holder = mock_ws(script)
    out = tmp_path / "out.mp3"
    synthesize_streaming(
        text="hello",
        voice_id="VOICE-1",
        out_path=out,
        api_key="k",
        model="eleven_flash_v2_5",
    )
    assert "wss://api.elevenlabs.io/v1/text-to-speech/VOICE-1/stream-input" in holder["url"]
    assert "model_id=eleven_flash_v2_5" in holder["url"]


def test_synth_sends_init_then_text_then_eos(mock_ws, tmp_path):
    """Three client-side messages in order: init (with api key), text,
    EOS marker (empty text)."""
    from lib.eleven_ws import synthesize_streaming
    script = FakeServerScript([
        {"audio": base64.b64encode(b"AB").decode(), "isFinal": True},
    ])
    holder = mock_ws(script)
    synthesize_streaming(
        text="hi there",
        voice_id="V", out_path=tmp_path / "o.mp3",
        api_key="secret-key-123", model="eleven_flash_v2_5",
    )
    sent = holder["ws"].sent_messages
    assert len(sent) >= 3
    # Init must carry the API key.
    init = sent[0]
    assert init.get("xi_api_key") == "secret-key-123"
    # Subsequent: text payload.
    text_msg = next(m for m in sent[1:] if m.get("text") and m["text"] != "")
    assert text_msg["text"] == "hi there"
    # EOS = empty text.
    assert sent[-1] == {"text": ""}


def test_synth_decodes_base64_chunks_and_writes_in_order(mock_ws, tmp_path):
    """The audio file should be the concatenation of all decoded chunks
    in the order the server sent them."""
    from lib.eleven_ws import synthesize_streaming
    chunks = [b"\x00\x01\x02", b"\x03\x04", b"\x05\x06\x07\x08"]
    script = FakeServerScript([
        {"audio": base64.b64encode(chunks[0]).decode(), "isFinal": False},
        {"audio": base64.b64encode(chunks[1]).decode(), "isFinal": False},
        {"audio": base64.b64encode(chunks[2]).decode(), "isFinal": True},
    ])
    mock_ws(script)
    out = tmp_path / "out.mp3"
    bytes_written = synthesize_streaming(
        text="x", voice_id="V", out_path=out, api_key="k",
        model="eleven_flash_v2_5",
    )
    assert out.read_bytes() == b"".join(chunks)
    assert bytes_written == sum(len(c) for c in chunks)


def test_synth_invokes_on_chunk_callback_per_chunk(mock_ws, tmp_path):
    """Caller can pass on_chunk(decoded_bytes) — fires as each chunk
    arrives so the TTS worker can broadcast SSE on the first chunk."""
    from lib.eleven_ws import synthesize_streaming
    chunks = [b"a", b"bc", b"def"]
    script = FakeServerScript([
        {"audio": base64.b64encode(chunks[0]).decode(), "isFinal": False},
        {"audio": base64.b64encode(chunks[1]).decode(), "isFinal": False},
        {"audio": base64.b64encode(chunks[2]).decode(), "isFinal": True},
    ])
    mock_ws(script)
    observed: list[tuple[int, bytes]] = []
    synthesize_streaming(
        text="x", voice_id="V", out_path=tmp_path / "o.mp3", api_key="k",
        model="eleven_flash_v2_5",
        on_chunk=lambda idx, data: observed.append((idx, data)),
    )
    assert observed == [(0, b"a"), (1, b"bc"), (2, b"def")]


def test_synth_terminates_on_is_final_true(mock_ws, tmp_path):
    """Even if the server keeps the WS open, the client stops reading
    after receiving isFinal=true. (We assert by giving the script extra
    frames that should NOT be read.)"""
    from lib.eleven_ws import synthesize_streaming
    script = FakeServerScript([
        {"audio": base64.b64encode(b"X").decode(), "isFinal": True},
        # Anything after this should NOT be consumed.
        {"audio": base64.b64encode(b"NEVER").decode(), "isFinal": True},
    ])
    mock_ws(script)
    out = tmp_path / "out.mp3"
    synthesize_streaming(
        text="x", voice_id="V", out_path=out, api_key="k",
        model="eleven_flash_v2_5",
    )
    assert out.read_bytes() == b"X"


def test_synth_raises_eleven_error_on_missing_api_key(tmp_path):
    """An empty api_key should fail fast — opening a WS without auth
    just wastes the round trip."""
    from lib.eleven_ws import synthesize_streaming, ElevenWSError
    with pytest.raises(ElevenWSError):
        synthesize_streaming(
            text="x", voice_id="V", out_path=tmp_path / "o.mp3",
            api_key="", model="eleven_flash_v2_5",
        )


def test_synth_propagates_server_error_message(mock_ws, tmp_path):
    """ElevenLabs sometimes returns a JSON error message instead of
    audio: {"error": "..."}. Surface it as ElevenWSError so callers can
    log + mark queue row failed."""
    from lib.eleven_ws import synthesize_streaming, ElevenWSError
    script = FakeServerScript([
        {"message": "Authorization failed", "code": "auth"},
    ])
    mock_ws(script)
    with pytest.raises(ElevenWSError) as exc_info:
        synthesize_streaming(
            text="x", voice_id="V", out_path=tmp_path / "o.mp3",
            api_key="k", model="eleven_flash_v2_5",
        )
    assert "Authorization failed" in str(exc_info.value)


def test_synth_handles_connection_reset_mid_stream(mock_ws, tmp_path):
    """If the WS dies before isFinal arrives, we should raise so the
    worker marks the row failed (rather than silently producing a
    truncated file marked done)."""
    from lib.eleven_ws import synthesize_streaming, ElevenWSError
    script = FakeServerScript([
        {"audio": base64.b64encode(b"partial").decode(), "isFinal": False},
        # No more frames — the script raises ConnectionResetError next call.
    ])
    mock_ws(script)
    with pytest.raises(ElevenWSError):
        synthesize_streaming(
            text="x", voice_id="V", out_path=tmp_path / "o.mp3",
            api_key="k", model="eleven_flash_v2_5",
        )


def test_synth_closes_ws_after_completion(mock_ws, tmp_path):
    """Resource cleanup: the WS must close after isFinal, success or fail."""
    from lib.eleven_ws import synthesize_streaming
    script = FakeServerScript([
        {"audio": base64.b64encode(b"X").decode(), "isFinal": True},
    ])
    holder = mock_ws(script)
    synthesize_streaming(
        text="x", voice_id="V", out_path=tmp_path / "o.mp3", api_key="k",
        model="eleven_flash_v2_5",
    )
    assert holder["ws"].closed is True
