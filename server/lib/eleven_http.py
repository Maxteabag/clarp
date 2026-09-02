"""Direct ElevenLabs TTS over HTTPS.

Replaces the previous `subprocess.run([sys.executable, elevenlabs_tts.py, ...])`
trip through an external skill. One stdlib `urllib.request` call, no SDK, no
shell-out. The hooks and the server both call into this module, so the
project is self-contained: clone, set the api key in config, run.

Returns the bytes written. Raises `ElevenError` on any failure so callers
can decide whether to log + swallow (hooks) or propagate (server).
"""
from __future__ import annotations

import json
import pathlib
import urllib.error
import urllib.request


class ElevenError(Exception):
    """Wraps any failure of the ElevenLabs HTTP call."""


_ENDPOINT = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"


def synthesize_to_file(
    text: str,
    voice_id: str,
    out_path: pathlib.Path,
    *,
    api_key: str,
    model: str = "eleven_flash_v2_5",
    speed: float = 1.2,
    stability: float = 0.5,
    similarity_boost: float = 0.75,
    timeout: float = 20.0,
) -> int:
    """POST to ElevenLabs, stream the MP3 response to `out_path`.

    Returns the number of bytes written. Raises ElevenError on any failure
    (no key, network error, non-2xx response, write failure).
    """
    if not api_key:
        raise ElevenError("ELEVEN_API_KEY not configured")
    if not voice_id:
        raise ElevenError("voice_id required")

    body = json.dumps({
        "text": text,
        "model_id": model,
        "voice_settings": {
            "stability": stability,
            "similarity_boost": similarity_boost,
            "speed": speed,
        },
    }).encode("utf-8")

    req = urllib.request.Request(
        _ENDPOINT.format(voice_id=voice_id),
        data=body,
        method="POST",
        headers={
            "xi-api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            audio = resp.read()
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            detail = ""
        raise ElevenError(f"ElevenLabs HTTP {e.code}: {detail or e.reason}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ElevenError(f"ElevenLabs request failed: {e}") from e

    if not audio:
        raise ElevenError("ElevenLabs returned empty body")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        out_path.write_bytes(audio)
    except OSError as e:
        raise ElevenError(f"write {out_path} failed: {e}") from e
    return len(audio)
