"""Cartesia Realtime STT over the websocket — the only path that takes vocabulary.

The batch endpoint accepts no biasing at all, so Ink-Whisper has always
transcribed blind while every other provider was handed the glossary. Keyterms
ride on the connection URL here, which is why this is a socket and not a POST.

Only Ink-2 and Ink-preview honour keyterms; Cartesia ignores them on
Ink-Whisper with a warning, so this never sends them there. A setting that
silently does nothing is worse than one that is plainly absent.
"""
from __future__ import annotations

import json
from typing import Any, Iterable
from urllib.parse import urlencode

WS_URL = "wss://api.cartesia.ai/stt/websocket"
API_VERSION = "2026-08-14"

# Cartesia's documented ceilings for one connection.
MAX_KEYTERMS = 100
MAX_KEYTERM_CHARS = 1200

# 100 ms of 16 kHz mono s16le, the chunk size Cartesia's guide recommends.
DEFAULT_CHUNK_BYTES = 3200

ENCODING = "pcm_s16le"

# Models that accept keyterms. Ink-Whisper is deliberately absent.
BIASING_MODELS = frozenset({"ink-2", "ink-preview"})


class CartesiaSTTStreamError(RuntimeError):
    pass


def fit_keyterms(terms: Iterable[str]) -> list[str]:
    """Trim to what one connection accepts, keeping the earliest terms.

    Ranking upstream has already put the most valuable terms first, so
    truncation from the tail loses the least.
    """
    kept: list[str] = []
    used = 0
    for raw in terms:
        term = (raw or "").strip()
        if not term:
            continue
        if len(kept) >= MAX_KEYTERMS:
            break
        if used + len(term) > MAX_KEYTERM_CHARS:
            break
        kept.append(term)
        used += len(term)
    return kept


def connection_url(*, model: str, sample_rate: int,
                   keyterms: Iterable[str] | None = None) -> str:
    """The wss URL for one transcription, keyterms included where honoured."""
    params: list[tuple[str, str]] = [
        ("model", model),
        ("encoding", ENCODING),
        ("sample_rate", str(int(sample_rate))),
        ("cartesia_version", API_VERSION),
    ]
    if model in BIASING_MODELS:
        params.extend(("keyterm", term) for term in fit_keyterms(keyterms or []))
    return f"{WS_URL}?{urlencode(params)}"


def _chunks(pcm: bytes, size: int) -> Iterable[bytes]:
    for start in range(0, len(pcm), size):
        yield pcm[start:start + size]


def transcribe_pcm(sock: Any, *, pcm: bytes,
                   chunk_bytes: int = DEFAULT_CHUNK_BYTES) -> str:
    """Send `pcm`, finalize, and return the assembled transcript.

    `text` on a transcript message is a delta from the last final chunk, so
    only `is_final` messages accumulate — adding partials would duplicate
    every word as it firms up.
    """
    for chunk in _chunks(pcm, max(1, int(chunk_bytes))):
        sock.send_binary(chunk)
    sock.send("finalize")
    sock.send("close")

    parts: list[str] = []
    try:
        while True:
            raw = sock.recv()
            if not raw:
                break
            try:
                message = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(message, dict):
                continue
            kind = message.get("type")
            if kind == "error":
                raise CartesiaSTTStreamError(
                    str(message.get("message")
                        or message.get("title")
                        or "cartesia stt stream error"))
            if kind == "transcript" and message.get("is_final"):
                parts.append(str(message.get("text") or ""))
            elif kind == "done":
                break
    finally:
        try:
            sock.close()
        except Exception:  # noqa: BLE001 - a close failure must not mask a result
            pass
    return "".join(parts).strip()


def pcm_16k_mono(audio_bytes: bytes) -> bytes:
    """Decode any uploaded clip to the raw s16le 16 kHz mono the socket wants.

    The batch path let Cartesia do this; the socket takes only raw frames, so
    the decode moves here. Reuses PyAV, already a dependency of the local
    whisper path, rather than adding a second decoder.
    """
    import io
    import av  # type: ignore
    from av.audio.resampler import AudioResampler  # type: ignore

    with av.open(io.BytesIO(audio_bytes), mode="r") as container:
        stream = next((c for c in container.streams if c.type == "audio"), None)
        if stream is None:
            raise CartesiaSTTStreamError("uploaded media has no audio stream")
        resampler = AudioResampler(format="s16", layout="mono", rate=16_000)
        out = bytearray()
        for frame in container.decode(stream):
            for converted in resampler.resample(frame):
                out += bytes(converted.planes[0])[:converted.samples * 2]
        for converted in resampler.resample(None):
            out += bytes(converted.planes[0])[:converted.samples * 2]
    return bytes(out)


class _Socket:
    """Adapts `websocket.WebSocket` to the small surface transcribe_pcm uses."""

    def __init__(self, raw):
        self._raw = raw

    def send_binary(self, payload: bytes) -> None:
        self._raw.send_binary(payload)

    def send(self, payload: str) -> None:
        self._raw.send(payload)

    def recv(self):
        return self._raw.recv()

    def close(self) -> None:
        self._raw.close()


def transcribe(*, audio_bytes: bytes, content_type: str, api_key: str,
               model: str = "ink-2", keyterms: list[str] | None = None,
               timeout: float = 30.0) -> tuple[str, float]:
    """Adapter-shaped entry point: returns `(text, elapsed_seconds)`."""
    import time
    import websocket

    del content_type  # the decode below determines the real format
    pcm = pcm_16k_mono(audio_bytes)
    url = connection_url(model=model, sample_rate=16_000, keyterms=keyterms or [])
    started = time.monotonic()
    raw = websocket.create_connection(
        url, timeout=timeout, header=[f"X-API-Key: {api_key}"])
    text = transcribe_pcm(_Socket(raw), pcm=pcm)
    return text, time.monotonic() - started
