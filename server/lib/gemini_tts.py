"""Gemini TTS (`streamGenerateContent`, Gemini API or Vertex AI) as an MP3 byte stream.

Gemini streams headerless 16-bit mono PCM at 24 kHz over SSE. The clip
pipeline speaks MP3 (chunked-file delivery, `/audio/<name>.mp3` replay), so
the PCM is piped through ffmpeg and the encoder's output is handed to
`on_chunk` as it appears. First audio arrives ~1-2.5 s after the request on
gemini-3.8-flash-tts, slower than Cartesia; agents opt in through
`[tts.agents.<name>] provider = "gemini"` (lib.tts_mode).

    POST https://generativelanguage.googleapis.com/v1beta/models/<model>:streamGenerateContent?alt=sse
    headers: x-goog-api-key
    body: contents[].parts[].text, generationConfig.responseModalities=["AUDIO"],
          generationConfig.speechConfig.voiceConfig = prebuiltVoiceConfig.voiceName
          (or `voice` for a Voice Design id, `voice_...`)

`backend="vertex"` sends the same body to the Vertex AI global endpoint with a
Vertex express key. Its SSE stream carries the same PCM parts. Vertex has no
fixed daily request cap for the model (the Gemini API Tier 1 allows 100 a
day), but Voice Design ids exist only on the surface that created them.

    POST https://aiplatform.googleapis.com/v1/publishers/google/models/<model>:streamGenerateContent?alt=sse
"""
from __future__ import annotations

import base64
import json
import pathlib
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from typing import Callable, Iterator

from .log import log_exception

_URLS = {
    "gemini_api": ("https://generativelanguage.googleapis.com/v1beta/models/"
                   "{model}:streamGenerateContent?alt=sse"),
    # Regional endpoints (europe-west4) answer 404 for gemini-3.8-flash-tts.
    "vertex": ("https://aiplatform.googleapis.com/v1/publishers/google/models/"
               "{model}:streamGenerateContent?alt=sse"),
}
_SAMPLE_RATE = 24000
_READ_BYTES = 16 * 1024
# Gemini answers 429/500/503 under load ("high demand"). Nothing has been
# played yet when that happens, so the request is retried before failing.
_RETRY_STATUS = {429, 500, 503}
_RETRY_DELAYS = (1.0, 2.5)


class GeminiTTSError(Exception):
    """Wraps any failure of the Gemini TTS path."""


def _emit(*a, **kw) -> None:
    try:
        from . import eventlog
        eventlog.emit(*a, **kw)
    except Exception as e:  # noqa: BLE001
        log_exception("geminiTtsEmitFail", e)


def voice_config(voice: str) -> dict:
    """Prebuilt voice name ("Kore") or a stored Voice Design id ("voice_...")."""
    if voice.startswith("voice_"):
        return {"voice": voice}
    return {"prebuiltVoiceConfig": {"voiceName": voice}}


def iter_pcm(lines) -> Iterator[bytes]:
    """Decode the PCM payloads out of an SSE line iterator."""
    for raw in lines:
        line = raw.decode("utf-8", "replace").strip() if isinstance(raw, bytes) else raw.strip()
        if not line.startswith("data:"):
            continue
        event = json.loads(line[5:])
        if "error" in event:
            raise GeminiTTSError(str(event["error"])[:300])
        for candidate in event.get("candidates", []):
            for part in (candidate.get("content") or {}).get("parts", []):
                data = (part.get("inlineData") or {}).get("data")
                if data:
                    yield base64.b64decode(data)


def synthesize(*,
               text: str,
               voice: str,
               out_path: pathlib.Path | None,
               api_key: str,
               model: str = "gemini-3.8-flash-tts",
               backend: str = "gemini_api",
               timeout: float = 60.0,
               on_chunk: Callable[[int, bytes], None] | None = None,
               trace_id: str | None = None,
               ) -> int:
    """Synthesize `text`, writing MP3 to `out_path` and/or `on_chunk`.

    Returns MP3 bytes produced. Raises `GeminiTTSError` on any failure.
    """
    if not api_key:
        raise GeminiTTSError("api_key required")
    if not voice:
        raise GeminiTTSError("voice required")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise GeminiTTSError("ffmpeg is required for Gemini TTS")
    if backend not in _URLS:
        raise GeminiTTSError(f"unknown Gemini TTS backend {backend!r}")

    body = json.dumps({
        "contents": [{"role": "user", "parts": [{"text": text}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": voice_config(voice)},
        },
    }).encode("utf-8")
    req = urllib.request.Request(
        _URLS[backend].format(model=model), data=body, method="POST",
        headers={"x-goog-api-key": api_key, "Content-Type": "application/json"})

    encoder = subprocess.Popen(
        [ffmpeg, "-loglevel", "error", "-f", "s16le", "-ar", str(_SAMPLE_RATE),
         "-ac", "1", "-i", "pipe:0", "-ar", "44100", "-c:a", "libmp3lame",
         "-b:a", "128k", "-flush_packets", "1", "-f", "mp3", "pipe:1"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    produced = {"bytes": 0, "error": None}
    t_start = time.perf_counter()
    t_first_pcm: list[float] = []

    def drain() -> None:
        f = None
        index = 0
        try:
            if out_path is not None:
                out_path.parent.mkdir(parents=True, exist_ok=True)
                f = out_path.open("wb")
            while True:
                chunk = encoder.stdout.read1(_READ_BYTES)
                if not chunk:
                    break
                if on_chunk is not None:
                    try:
                        on_chunk(index, chunk)
                    except Exception as e:  # noqa: BLE001 — match cartesia_tts
                        log_exception("geminiTtsOnChunkFail", e)
                if f is not None:
                    f.write(chunk)
                produced["bytes"] += len(chunk)
                index += 1
        except Exception as e:  # noqa: BLE001
            produced["error"] = e
        finally:
            if f is not None:
                f.close()

    reader = threading.Thread(target=drain, daemon=True, name="gemini-tts-mp3")
    reader.start()
    try:
        try:
            for attempt, delay in enumerate((*_RETRY_DELAYS, None)):
                try:
                    resp = urllib.request.urlopen(req, timeout=timeout)
                    break
                except urllib.error.HTTPError as e:
                    if delay is None or e.code not in _RETRY_STATUS:
                        raise
                    _emit("gemini_tts", "retry", level="warn", trace_id=trace_id,
                          detail={"status": e.code, "attempt": attempt + 1,
                                  "backend": backend})
                    e.close()
                    time.sleep(delay)
            with resp:
                for pcm in iter_pcm(resp):
                    if not t_first_pcm:
                        t_first_pcm.append(time.perf_counter())
                        _emit("gemini_tts", "firstChunk", trace_id=trace_id,
                              detail={"voice": voice, "model": model,
                                      "backend": backend,
                                      "ttfb_ms": int((t_first_pcm[0] - t_start) * 1000)})
                    encoder.stdin.write(pcm)
        finally:
            encoder.stdin.close()
        encoder.wait(timeout=30)
        reader.join(timeout=30)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise GeminiTTSError(f"Gemini ({backend}) HTTP {e.code}: {detail}") from e
    except GeminiTTSError:
        raise
    except Exception as e:  # noqa: BLE001
        raise GeminiTTSError(f"Gemini request failed: {e}") from e
    finally:
        if encoder.poll() is None:
            encoder.kill()
            encoder.wait()

    if produced["error"] is not None:
        raise GeminiTTSError(f"MP3 encode failed: {produced['error']}")
    if encoder.returncode != 0:
        stderr = encoder.stderr.read().decode("utf-8", "replace")[:300]
        raise GeminiTTSError(f"ffmpeg exited {encoder.returncode}: {stderr}")
    if not t_first_pcm or produced["bytes"] == 0:
        raise GeminiTTSError("Gemini returned no audio")
    _emit("gemini_tts", "complete", trace_id=trace_id,
          detail={"voice": voice, "model": model, "backend": backend,
                  "bytes": produced["bytes"],
                  "elapsed_ms": int((time.perf_counter() - t_start) * 1000)})
    return produced["bytes"]
