"""ElevenLabs Scribe speech-to-text with keyterm biasing."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
import uuid


class ElevenSTTError(RuntimeError):
    pass


def _multipart(fields: list[tuple[str, str]], file_field: str,
               filename: str, content_type: str, payload: bytes
               ) -> tuple[bytes, str]:
    boundary = f"----clarp{uuid.uuid4().hex}"
    out = bytearray()
    for name, value in fields:
        out += (f"--{boundary}\r\nContent-Disposition: form-data; "
                f"name=\"{name}\"\r\n\r\n{value}\r\n").encode()
    out += (f"--{boundary}\r\nContent-Disposition: form-data; "
            f"name=\"{file_field}\"; filename=\"{filename}\"\r\n"
            f"Content-Type: {content_type}\r\n\r\n").encode()
    out += payload + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


def _filename_for(content_type: str) -> str:
    ct = (content_type or "").lower()
    if "mp4" in ct or "m4a" in ct:
        return "audio.m4a"
    if "wav" in ct:
        return "audio.wav"
    if "ogg" in ct:
        return "audio.ogg"
    if "mpeg" in ct or "mp3" in ct:
        return "audio.mp3"
    return "audio.webm"


def transcribe(*, audio_bytes: bytes, content_type: str, api_key: str,
               model: str = "scribe_v2", keyterms: list[str] | None = None,
               language: str = "en", timeout: float = 60.0
               ) -> tuple[str, float]:
    if not api_key:
        raise ElevenSTTError("ElevenLabs API key is not configured")
    fields: list[tuple[str, str]] = [("model_id", model), ("language_code", language)]
    # Each term is its own `keyterms` field; the API allows up to 1000 terms of
    # under 50 characters and the compiler stays far below both.
    fields.extend(("keyterms", term[:49]) for term in (keyterms or []))
    body, ctype = _multipart(
        fields, "file", _filename_for(content_type),
        content_type or "audio/webm", audio_bytes)
    request = urllib.request.Request(
        "https://api.elevenlabs.io/v1/speech-to-text", data=body, method="POST",
        headers={"xi-api-key": api_key, "Content-Type": ctype})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")[:200]
        raise ElevenSTTError(
            f"ElevenLabs HTTP {error.code}: {detail or error.reason}") from error
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise ElevenSTTError(f"ElevenLabs request failed: {error}") from error
    text = payload.get("text")
    if not isinstance(text, str):
        raise ElevenSTTError("ElevenLabs response had no text")
    duration = 0.0
    words = payload.get("words") or []
    try:
        if words:
            duration = float(words[-1].get("end") or 0.0)
    except (TypeError, ValueError, AttributeError):
        duration = 0.0
    return text, duration
