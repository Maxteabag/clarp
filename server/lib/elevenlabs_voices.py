"""Server-scoped ElevenLabs voice discovery; API keys never reach clients.

The account's own library is the truth: premade voices get renamed and
reassigned upstream (the id long labelled "Bella" is now ElevenLabs' "Sarah"),
and professional or cloned voices only exist per account. A hardcoded snapshot
drifts from both.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

from . import config

_URL = "https://api.elevenlabs.io/v1/voices"
_CACHE_SECONDS = 900
_lock = threading.Lock()
_cache: tuple[float, list[tuple[str, str, str]]] | None = None


def _describe(voice: dict) -> str:
    labels = voice.get("labels") or {}
    parts = [
        str(labels.get(field) or "").strip()
        for field in ("accent", "gender", "age", "use_case", "description")
    ]
    return ", ".join(part for part in parts if part)[:120]


def english_voices(*, force: bool = False) -> list[tuple[str, str, str]]:
    """Live account voices, or [] when no key is configured.

    ElevenLabs' premade voices are multilingual rather than language-tagged, so
    the account library is taken as-is; anything explicitly labelled for another
    language is dropped.
    """
    global _cache
    now = time.monotonic()
    with _lock:
        if not force and _cache and now - _cache[0] < _CACHE_SECONDS:
            return list(_cache[1])
    key = config.load().eleven_key()
    if not key:
        return []
    request = urllib.request.Request(_URL, headers={"xi-api-key": key})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.load(response)
    except (urllib.error.URLError, OSError, ValueError):
        return []
    rows: list[tuple[str, str, str]] = []
    for voice in payload.get("voices") or []:
        language = str((voice.get("labels") or {}).get("language") or "").lower()
        if language and not language.startswith("en"):
            continue
        voice_id = str(voice.get("voice_id") or "")
        if not voice_id:
            continue
        name = str(voice.get("name") or voice_id).split(" - ")[0].strip()
        rows.append((voice_id, name, _describe(voice)))
    rows.sort(key=lambda row: row[1].lower())
    with _lock:
        _cache = (now, list(rows))
    return rows
