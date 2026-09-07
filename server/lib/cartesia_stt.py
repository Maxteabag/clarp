"""Cartesia Ink batch speech-to-text.

The batch endpoint accepts no vocabulary biasing today; the compiler gives
Cartesia a zero budget so nothing is spent building a payload it cannot use.
Ink-2's turn detection lives on the streaming socket, which the phone drives
when the turn-taking strategy is `provider`.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from .eleven_stt import _filename_for, _multipart

CARTESIA_VERSION = "2026-08-14"


class CartesiaSTTError(RuntimeError):
    pass


def transcribe(*, audio_bytes: bytes, content_type: str, api_key: str,
               model: str = "ink-whisper", keyterms: list[str] | None = None,
               language: str = "en", timeout: float = 60.0
               ) -> tuple[str, float]:
    if not api_key:
        raise CartesiaSTTError("Cartesia API key is not configured")
    del keyterms  # no biasing on the batch endpoint
    body, ctype = _multipart(
        [("model", model), ("language", language)], "file",
        _filename_for(content_type), content_type or "audio/webm", audio_bytes)
    request = urllib.request.Request(
        "https://api.cartesia.ai/stt", data=body, method="POST",
        headers={"Authorization": f"Bearer {api_key}",
                 "Cartesia-Version": CARTESIA_VERSION,
                 "Content-Type": ctype})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")[:200]
        raise CartesiaSTTError(
            f"Cartesia HTTP {error.code}: {detail or error.reason}") from error
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise CartesiaSTTError(f"Cartesia request failed: {error}") from error
    text = payload.get("text")
    if not isinstance(text, str):
        raise CartesiaSTTError("Cartesia response had no text")
    try:
        duration = float(payload.get("duration") or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    return text, duration
