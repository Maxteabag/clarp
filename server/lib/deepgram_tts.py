"""Deepgram Flux/Aura text-to-speech MP3 synthesis."""
from __future__ import annotations

import json
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request

from .voice_markup import strip_ssml_for_plain_tts


class DeepgramError(RuntimeError):
    pass


def synthesize(*, text: str, voice_id: str, out_path: Path | None,
               api_key: str, on_chunk=None, timeout: float = 30.0,
               **_unused) -> int:
    if not api_key:
        raise DeepgramError("Deepgram API key is not configured")
    if not voice_id:
        raise DeepgramError("Deepgram voice is not configured")
    # Aura/Flux do not parse SSML; a surviving <break> tag is read aloud.
    text = strip_ssml_for_plain_tts(text)
    query = urllib.parse.urlencode({
        "model": voice_id, "encoding": "mp3", "container": "none"})
    api_version = "v2" if voice_id.startswith("flux-") else "v1"
    request = urllib.request.Request(
        f"https://api.deepgram.com/{api_version}/speak?{query}",
        data=json.dumps({"text": text}).encode(), method="POST",
        headers={
            "Authorization": f"Token {api_key}",
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        })
    total = 0
    output = None
    try:
        if out_path is not None:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            output = out_path.open("wb")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            index = 0
            while chunk := response.read(16 * 1024):
                if output is not None:
                    output.write(chunk)
                    output.flush()
                if on_chunk is not None:
                    on_chunk(index, chunk)
                total += len(chunk)
                index += 1
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")[:200]
        raise DeepgramError(
            f"Deepgram HTTP {error.code}: {detail or error.reason}") from error
    except (urllib.error.URLError, OSError) as error:
        raise DeepgramError(f"Deepgram request failed: {error}") from error
    finally:
        if output is not None:
            output.close()
    if not total:
        raise DeepgramError("Deepgram returned empty audio")
    return total
