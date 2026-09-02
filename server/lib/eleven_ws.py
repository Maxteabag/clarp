"""ElevenLabs realtime (WebSocket) TTS client.

Counterpart to lib.eleven_http but uses the WebSocket streaming endpoint:
text in via WS, audio chunks out via WS, decoded and written to disk
incrementally. Designed for the Phase B worker — TTSWorker calls this
function and gets back as soon as the EOS frame arrives.

Wire format (ElevenLabs realtime API):
    URL: wss://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream-input
         ?model_id=<model>&output_format=mp3_44100_128
    client → server messages (JSON text frames):
        1) {"text": " ", "xi_api_key": "...", "voice_settings": {...},
            "generation_config": {"chunk_length_schedule": [...]}}
        2) {"text": "actual content to synthesize"}
        3) {"text": ""}                      ← EOS marker
    server → client messages (JSON text frames):
        {"audio": "<base64>", "isFinal": false}   ← N of these
        {"audio": "<base64>", "isFinal": true}    ← last one
        {"message": "...", "code": "..."}          ← error
"""
from __future__ import annotations

import base64
import binascii
import json
import pathlib
import time
from typing import Callable

import websocket


def _emit(*a, **kw):
    """Lazy eventlog import — same pattern as the rest of the lib."""
    try:
        from . import eventlog
        eventlog.emit(*a, **kw)
    except Exception:
        pass


class ElevenWSError(Exception):
    """Wraps any failure of the ElevenLabs WS path."""


_WS_URL_TMPL = (
    "wss://api.elevenlabs.io/v1/text-to-speech/{voice}/stream-input"
    "?model_id={model}&output_format=mp3_44100_128"
)


def _open_ws(url: str, **kw) -> websocket.WebSocket:
    """Indirection point so tests can swap in a FakeWS without monkey-
    patching the websocket library."""
    return websocket.create_connection(url, **kw)


def synthesize_streaming(*,
                         text: str,
                         voice_id: str,
                         out_path: pathlib.Path | None,
                         api_key: str,
                         model: str = "eleven_flash_v2_5",
                         speed: float = 1.2,
                         stability: float = 0.5,
                         similarity_boost: float = 0.75,
                         timeout: float = 30.0,
                         on_chunk: Callable[[int, bytes], None] | None = None,
                         trace_id: str | None = None,
                         ) -> int:
    """Synthesize `text` via the ElevenLabs WebSocket endpoint, writing
    decoded MP3 bytes to `out_path` as they arrive (or skipping the file
    write entirely when `out_path is None`).

    Returns total bytes written / decoded. Raises `ElevenWSError` on any
    failure (no key, server-reported error, mid-stream disconnect).

    `on_chunk(index, decoded_bytes)` is invoked once per audio chunk
    received, BEFORE the chunk is written to disk. Callers (e.g. the
    TTSWorker) use this to fan bytes into ClipStreamBroker / ffmpeg /
    whatever the active delivery needs.

    When `out_path is None`, ALL output flows via `on_chunk` — this is
    the path HlsDelivery takes (bytes go to ffmpeg's stdin, no mp3 on
    disk). Chunked-file delivery passes a real path so the mp3 remains
    available as a replay/fallback artifact.
    """
    if not api_key:
        raise ElevenWSError("api_key required")
    if not voice_id:
        raise ElevenWSError("voice_id required")

    url = _WS_URL_TMPL.format(voice=voice_id, model=model)
    t_start = time.perf_counter()
    try:
        ws = _open_ws(url, timeout=timeout)
    except Exception as e:                   # noqa: BLE001 — translate any
        _emit("eleven_ws", "connectFail",
              level="error", trace_id=trace_id,
              detail={"voice_id": voice_id, "error": str(e),
                      "elapsed_ms": int((time.perf_counter() - t_start) * 1000)})
        raise ElevenWSError(f"WS connect failed: {e}") from e
    _emit("eleven_ws", "connect", trace_id=trace_id,
          detail={"voice_id": voice_id, "model": model,
                  "text_len": len(text),
                  "elapsed_ms": int((time.perf_counter() - t_start) * 1000)})

    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
    bytes_written = 0
    t_first_chunk: float | None = None
    chunk_count = 0
    try:
        # 1) Init message — auth + voice settings + chunk schedule. The
        #    initial text MUST be a single space (not empty); empty is
        #    the EOS marker.
        ws.send(json.dumps({
            "text": " ",
            "xi_api_key": api_key,
            "voice_settings": {
                "stability": stability,
                "similarity_boost": similarity_boost,
                "speed": speed,
            },
            # Smaller initial chunks → lower time-to-first-audio.
            "generation_config": {
                "chunk_length_schedule": [50, 90, 120, 150, 200],
            },
        }))
        # 2) The actual content.
        ws.send(json.dumps({"text": text}))
        # 3) EOS marker. After this the server flushes everything and
        #    sends isFinal=true.
        ws.send(json.dumps({"text": ""}))

        # Read audio chunks until isFinal. The mp3 file is optional: when
        # `out_path is None` (HlsDelivery, ffmpeg-on-stdin), the bytes
        # flow exclusively via `on_chunk`. ExitStack lets us conditionally
        # enter the file context without duplicating the loop body.
        import contextlib
        with contextlib.ExitStack() as stack:
            f = stack.enter_context(out_path.open("wb")) if out_path else None
            saw_final = False
            chunk_idx = 0
            while not saw_final:
                try:
                    raw = ws.recv()
                except (websocket.WebSocketConnectionClosedException,
                        ConnectionResetError, OSError) as e:
                    raise ElevenWSError(
                        f"WS closed before isFinal: {e}"
                    ) from e

                if not raw:
                    raise ElevenWSError("empty frame from server")

                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError as e:
                    raise ElevenWSError(
                        f"server sent non-JSON frame: {e}"
                    ) from e

                # Error frame: ElevenLabs sometimes returns
                # {"message": "...", "code": "..."} instead of audio.
                if "audio" not in msg:
                    err = (msg.get("message") or msg.get("error")
                           or f"unexpected frame: {msg}")
                    _emit("eleven_ws", "serverError", level="error",
                          trace_id=trace_id,
                          detail={"voice_id": voice_id,
                                  "code": msg.get("code"),
                                  "error": str(err)[:200]})
                    raise ElevenWSError(str(err))

                audio_b64 = msg.get("audio") or ""
                if audio_b64:
                    try:
                        decoded = base64.b64decode(audio_b64)
                    except (ValueError, binascii.Error) as e:
                        raise ElevenWSError(
                            f"bad base64 from server: {e}"
                        ) from e
                    if t_first_chunk is None:
                        t_first_chunk = time.perf_counter()
                        _emit("eleven_ws", "firstChunk", trace_id=trace_id,
                              detail={"voice_id": voice_id,
                                      "bytes": len(decoded),
                                      "ttfb_ms": int(
                                          (t_first_chunk - t_start) * 1000)})
                    if on_chunk is not None:
                        try:
                            on_chunk(chunk_idx, decoded)
                        except Exception:
                            # Callback errors shouldn't tear down the
                            # synthesis — log and continue.
                            pass
                    if f is not None:
                        f.write(decoded)
                        f.flush()            # make bytes visible to
                                              # streaming endpoint readers
                    bytes_written += len(decoded)
                    chunk_idx += 1
                    chunk_count += 1
                saw_final = bool(msg.get("isFinal"))
    finally:
        try:
            ws.close()
        except Exception:
            pass

    _emit("eleven_ws", "eos", trace_id=trace_id,
          duration_ms=int((time.perf_counter() - t_start) * 1000),
          detail={"voice_id": voice_id,
                  "bytes": bytes_written,
                  "chunks": chunk_count,
                  "ttfb_ms": (int((t_first_chunk - t_start) * 1000)
                              if t_first_chunk else None)})
    return bytes_written
