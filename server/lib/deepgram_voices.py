"""Server-scoped Deepgram voice discovery; API keys never reach clients.

Deepgram splits its catalogue across two API versions: Aura-2 is listed by
`/v1/models`, Flux by `/v2/models`, and the two also synthesize on different
`speak` endpoints. Both are merged here so the rest of Clarp sees one English
voice list.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

from . import config

_V1_MODELS = "https://api.deepgram.com/v1/models"
_V2_MODELS = "https://api.deepgram.com/v2/models"
_CACHE_SECONDS = 900
_lock = threading.Lock()
_cache: tuple[float, list[tuple[str, str, str]]] | None = None


def _get(url: str, key: str) -> dict:
    request = urllib.request.Request(
        url, headers={"Authorization": f"Token {key}"})
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.load(response)


def _describe(model: dict) -> str:
    metadata = model.get("metadata") or {}
    accent = str(metadata.get("accent") or "").strip()
    tags = [
        str(tag) for tag in (metadata.get("tags") or [])
        if tag not in ("masculine", "feminine")
    ][:3]
    return "; ".join(part for part in (accent, ", ".join(tags)) if part)


def _english(models: list[dict], prefix: str = "") -> list[tuple[str, str, str]]:
    rows = []
    for model in models:
        name = str(model.get("canonical_name") or "")
        if prefix and not name.startswith(prefix):
            continue
        if (model.get("languages") or [""])[0] != "en":
            continue
        rows.append((name, str(model.get("name") or name).title(), _describe(model)))
    return sorted(rows)


def english_voices(*, force: bool = False) -> list[tuple[str, str, str]]:
    """Live English Deepgram voices, or [] when no key is configured.

    Callers fall back to the snapshot in `voice_catalog` so the picker still
    lists voices on a Computer with no Deepgram credential — a voice can be
    chosen for a contact before the provider is ever set up.
    """
    global _cache
    now = time.monotonic()
    with _lock:
        if not force and _cache and now - _cache[0] < _CACHE_SECONDS:
            return list(_cache[1])
    key = config.load().deepgram_key()
    if not key:
        return []
    rows: list[tuple[str, str, str]] = []
    try:
        rows.extend(_english(_get(_V2_MODELS, key).get("tts") or []))
        rows.extend(_english(_get(_V1_MODELS, key).get("tts") or [], "aura-2-"))
    except (urllib.error.URLError, OSError, ValueError):
        return []
    with _lock:
        _cache = (now, list(rows))
    return rows
