"""Cartesia WebSocket TTS client for raw PCM low-latency playback.

Cartesia's WebSocket endpoint streams raw PCM chunks. That is not suitable for
the browser `<audio>` pipeline, but it is ideal for the native app where we can
feed PCM directly into AVAudioEngine as chunks arrive.
"""
from __future__ import annotations

import base64
import binascii
import json
import pathlib
import time
import uuid
from typing import Callable
from urllib.parse import urlencode

import websocket

from .cartesia_tts import CartesiaError


def _emit(*a, **kw):
    try:
        from . import eventlog
        eventlog.emit(*a, **kw)
    except Exception:
        pass


_WS_URL = "wss://api.cartesia.ai/tts/websocket"
_VERSION = "2026-03-01"
_SAMPLE_RATE = 44100
_ENCODING = "pcm_f32le"


def _open_ws(url: str, **kw) -> websocket.WebSocket:
    return websocket.create_connection(url, **kw)


def synthesize_raw_pcm(*,
                       text: str,
                       voice_id: str,
                       out_path: pathlib.Path | None,
                       api_key: str,
                       model: str = "sonic-3.5",
                       language: str = "en",
                       timeout: float = 30.0,
                       on_chunk: Callable[[int, bytes], None] | None = None,
                       trace_id: str | None = None,
                       max_buffer_delay_ms: int = 0,
                       encoding: str = _ENCODING,
                       sample_rate: int = _SAMPLE_RATE,
                       ) -> int:
    """Stream raw PCM from Cartesia over WebSocket.

    `encoding`/`sample_rate` must match what the delivery advertises to the
    client in the clip payload (see clip_delivery.raw_pcm) — the client
    decodes from the advertisement, so a mismatch plays garbage."""
    if not api_key:
        raise CartesiaError("api_key required")
    if not voice_id:
        raise CartesiaError("voice_id required")

    url = f"{_WS_URL}?{urlencode({'cartesia_version': _VERSION})}"
    t_start = time.perf_counter()
    try:
        ws = _open_ws(url, timeout=timeout, header=[f"X-API-Key: {api_key}"])
    except Exception as e:  # noqa: BLE001
        _emit("cartesia_ws", "connectFail", level="error", trace_id=trace_id,
              detail={"voice_id": voice_id, "error": str(e),
                      "elapsed_ms": int((time.perf_counter() - t_start) * 1000)})
        raise CartesiaError(f"WS connect failed: {e}") from e

    context_id = str(uuid.uuid4())
    bytes_written = 0
    chunk_count = 0
    t_first_chunk: float | None = None
    try:
        ws.send(json.dumps({
            "model_id": model,
            "transcript": text,
            "voice": {"mode": "id", "id": voice_id},
            "language": language,
            "context_id": context_id,
            "continue": False,
            "max_buffer_delay_ms": max_buffer_delay_ms,
            "output_format": {
                "container": "raw",
                "encoding": encoding,
                "sample_rate": sample_rate,
            },
        }))

        if out_path is not None:
            out_path.parent.mkdir(parents=True, exist_ok=True)

        import contextlib
        with contextlib.ExitStack() as stack:
            f = stack.enter_context(out_path.open("wb")) if out_path else None
            saw_done = False
            while not saw_done:
                try:
                    raw = ws.recv()
                except (websocket.WebSocketConnectionClosedException,
                        ConnectionResetError, OSError) as e:
                    raise CartesiaError(f"WS closed before done: {e}") from e
                if not raw:
                    raise CartesiaError("empty frame from server")
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError as e:
                    raise CartesiaError(f"server sent non-JSON frame: {e}") from e

                msg_type = msg.get("type")
                if msg_type == "error" or "error" in msg:
                    err = msg.get("error") or msg.get("message") or msg
                    _emit("cartesia_ws", "serverError", level="error",
                          trace_id=trace_id,
                          detail={"voice_id": voice_id, "error": str(err)[:300]})
                    raise CartesiaError(str(err))
                if msg_type == "done":
                    saw_done = True
                    continue
                if msg_type != "chunk":
                    saw_done = bool(msg.get("done"))
                    continue

                audio_b64 = msg.get("data") or ""
                if audio_b64:
                    try:
                        decoded = base64.b64decode(audio_b64)
                    except (ValueError, binascii.Error) as e:
                        raise CartesiaError(f"bad base64 from server: {e}") from e
                    if t_first_chunk is None:
                        t_first_chunk = time.perf_counter()
                        _emit("cartesia_ws", "firstChunk", trace_id=trace_id,
                              detail={"voice_id": voice_id,
                                      "encoding": encoding,
                                      "sample_rate": sample_rate,
                                      "bytes": len(decoded),
                                      "ttfb_ms": int(
                                          (t_first_chunk - t_start) * 1000)})
                    if on_chunk is not None:
                        try:
                            on_chunk(chunk_count, decoded)
                        except Exception:
                            pass
                    if f is not None:
                        f.write(decoded)
                        f.flush()
                    bytes_written += len(decoded)
                    chunk_count += 1
                saw_done = bool(msg.get("done"))
    finally:
        try:
            ws.close()
        except Exception:
            pass

    if not bytes_written:
        if out_path is not None:
            try:
                out_path.unlink()
            except OSError:
                pass
        raise CartesiaError("empty response body")

    _emit("cartesia_ws", "synth", trace_id=trace_id,
          duration_ms=int((time.perf_counter() - t_start) * 1000),
          detail={"voice_id": voice_id, "model": model,
                  "text_len": len(text), "bytes": bytes_written,
                  "chunks": chunk_count,
                  "encoding": encoding,
                  "sample_rate": sample_rate,
                  "ttfb_ms": (int((t_first_chunk - t_start) * 1000)
                              if t_first_chunk else None)})
    return bytes_written

