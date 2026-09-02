"""Cartesia TTS client (HTTP `/tts/bytes`, MP3 output).

The primary synthesis provider. Drop-in shape-compatible with
`eleven_ws.synthesize_streaming`: same `on_chunk(index, mp3_bytes)` +
`out_path` contract, so the worker and every `ClipDelivery` consume it
unchanged.

Why bytes, not WebSocket: Cartesia's WS endpoint only emits raw PCM, but
our whole delivery pipeline (ChunkedFile + HLS/ffmpeg) speaks MP3. The
HTTP `/tts/bytes` endpoint can stream MP3 bytes for a complete transcript,
so it slots in with zero re-encoding while still letting live deliveries
flush bytes as Cartesia produces them. Incremental WS input streaming
(with a PCM→MP3 transcode) is a later optimization if transcript chunking
still leaves too much latency.

Wire format (verified live 2026-06):
    POST https://api.cartesia.ai/tts/bytes
    headers: X-API-Key, Cartesia-Version, Content-Type: application/json
    body: {model_id, transcript, voice:{mode:"id",id}, language,
           output_format:{container:"mp3", sample_rate, bit_rate}}
    response: raw audio/mpeg bytes (HTTP 200)
"""
from __future__ import annotations

import json
import pathlib
import time
import urllib.error
import urllib.request
from typing import Callable


def _emit(*a, **kw):
    """Lazy eventlog import — same pattern as the rest of the lib."""
    try:
        from . import eventlog
        eventlog.emit(*a, **kw)
    except Exception:
        pass


class CartesiaError(Exception):
    """Wraps any failure of the Cartesia HTTP path."""


_URL = "https://api.cartesia.ai/tts/bytes"
# The dated API version the request body schema below targets. Cartesia
# pins behaviour to this header; bump deliberately alongside body changes.
_VERSION = "2025-11-04"
# Match the rest of the pipeline: 44.1 kHz mono MP3 @ 128 kbps.
_SAMPLE_RATE = 44100
_BIT_RATE = 128000
# Read Cartesia's streaming HTTP response in bounded pieces so live deliveries
# (broker / ffmpeg stdin) start flushing before the whole body is buffered.
# The broker and ffmpeg both treat the stream as an opaque ordered byte
# sequence, so the split points are immaterial.
_CHUNK_BYTES = 16 * 1024


def synthesize(*,
               text: str,
               voice_id: str,
               out_path: pathlib.Path | None,
               api_key: str,
               model: str = "sonic-3.5",
               language: str = "en",
               timeout: float = 30.0,
               on_chunk: Callable[[int, bytes], None] | None = None,
               trace_id: str | None = None,
               ) -> int:
    """Synthesize `text` via Cartesia, writing MP3 bytes to `out_path`
    (or skipping the file write when `out_path is None`, the HLS path).

    Returns total bytes written. Raises `CartesiaError` on any failure
    (no key, HTTP error, empty body). `on_chunk(index, mp3_bytes)` is
    invoked as response bytes arrive, mirroring eleven_ws so the active
    `ClipDelivery` consumes either provider identically.
    """
    if not api_key:
        raise CartesiaError("api_key required")
    if not voice_id:
        raise CartesiaError("voice_id required")

    body = json.dumps({
        "model_id": model,
        "transcript": text,
        "voice": {"mode": "id", "id": voice_id},
        "language": language,
        "output_format": {
            "container": "mp3",
            "sample_rate": _SAMPLE_RATE,
            "bit_rate": _BIT_RATE,
        },
    }).encode("utf-8")
    req = urllib.request.Request(_URL, data=body, method="POST", headers={
        "X-API-Key": api_key,
        "Cartesia-Version": _VERSION,
        "Content-Type": "application/json",
    })

    t_start = time.perf_counter()
    t_first_chunk: float | None = None
    bytes_written = 0
    chunk_count = 0
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            f = None
            try:
                if out_path is not None:
                    out_path.parent.mkdir(parents=True, exist_ok=True)
                    f = out_path.open("wb")
                while True:
                    chunk = resp.read(_CHUNK_BYTES)
                    if not chunk:
                        break
                    if t_first_chunk is None:
                        t_first_chunk = time.perf_counter()
                        _emit("cartesia", "firstChunk", trace_id=trace_id,
                              detail={"voice_id": voice_id,
                                      "bytes": len(chunk),
                                      "ttfb_ms": int(
                                          (t_first_chunk - t_start) * 1000)})
                    if on_chunk is not None:
                        try:
                            on_chunk(chunk_count, chunk)
                        except Exception:
                            # Callback errors shouldn't tear down a completed
                            # synth, matching eleven_ws semantics.
                            pass
                    if f is not None:
                        f.write(chunk)
                        f.flush()
                    bytes_written += len(chunk)
                    chunk_count += 1
            finally:
                if f is not None:
                    f.close()
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        _emit("cartesia", "httpError", level="error", trace_id=trace_id,
              detail={"voice_id": voice_id, "status": e.code, "body": detail})
        raise CartesiaError(f"HTTP {e.code}: {detail or e.reason}") from e
    except (urllib.error.URLError, OSError) as e:
        _emit("cartesia", "connectFail", level="error", trace_id=trace_id,
              detail={"voice_id": voice_id, "error": str(e)})
        raise CartesiaError(f"request failed: {e}") from e

    if not bytes_written:
        if out_path is not None:
            try:
                out_path.unlink()
            except OSError:
                pass
        raise CartesiaError("empty response body")

    duration_ms = int((time.perf_counter() - t_start) * 1000)
    _emit("cartesia", "synth", trace_id=trace_id,
          duration_ms=duration_ms,
          detail={"voice_id": voice_id, "model": model,
                  "text_len": len(text), "bytes": bytes_written,
                  "chunks": chunk_count})

    return bytes_written
