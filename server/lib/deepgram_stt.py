"""Deepgram pre-recorded transcription (Nova-3) with keyterm prompting."""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request


class DeepgramSTTError(RuntimeError):
    pass


def transcribe(*, audio_bytes: bytes, content_type: str, api_key: str,
               model: str = "nova-3", keyterms: list[str] | None = None,
               language: str = "en", timeout: float = 30.0
               ) -> tuple[str, float]:
    if not api_key:
        raise DeepgramSTTError("Deepgram API key is not configured")
    params: list[tuple[str, str]] = [
        ("model", model), ("smart_format", "true"), ("language", language)]
    # Repeated `keyterm` parameters; the API caps the lot at 500 tokens and
    # the compiler already kept us well under that.
    params.extend(("keyterm", term) for term in (keyterms or []))
    request = urllib.request.Request(
        "https://api.deepgram.com/v1/listen?" + urllib.parse.urlencode(params),
        data=audio_bytes, method="POST",
        headers={"Authorization": f"Token {api_key}",
                 "Content-Type": content_type or "audio/webm"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")[:200]
        raise DeepgramSTTError(
            f"Deepgram HTTP {error.code}: {detail or error.reason}") from error
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise DeepgramSTTError(f"Deepgram request failed: {error}") from error
    try:
        alternative = body["results"]["channels"][0]["alternatives"][0]
        text = str(alternative.get("transcript") or "")
    except (KeyError, IndexError, TypeError) as error:
        raise DeepgramSTTError("Deepgram response had no transcript") from error
    duration = 0.0
    try:
        duration = float(body.get("metadata", {}).get("duration") or 0.0)
    except (TypeError, ValueError):
        pass
    return text, duration
