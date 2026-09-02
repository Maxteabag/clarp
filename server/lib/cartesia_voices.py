"""Server-scoped Cartesia voice discovery; API keys never reach clients."""
from __future__ import annotations

import json
import threading
import time
import urllib.parse
import urllib.request
import urllib.error

from . import config
from .agent_store import load_agents
from .voice import CARTESIA, resolve_voice

_URL = "https://api.cartesia.ai/voices"
_VERSION = "2026-08-14"
_lock = threading.Lock()
_cache: tuple[float, list[dict]] | None = None


def _fetch_page(key: str, *, cursor: str = "") -> dict:
    query = [("limit", "100"), ("language", "en"), ("expand[]", "preview_file_url")]
    if cursor:
        query.append(("starting_after", cursor))
    url = _URL + "?" + urllib.parse.urlencode(query)
    for header, value in (("Authorization", "Bearer " + key),
                          ("Authorization", key), ("X-API-Key", key)):
        request = urllib.request.Request(
            url, headers={header: value, "Cartesia-Version": _VERSION})
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code != 401 or header == "X-API-Key":
                raise
    return {}


def english_voices(*, force: bool = False) -> list[dict]:
    global _cache
    now = time.monotonic()
    with _lock:
        if not force and _cache and now - _cache[0] < 900:
            return [dict(row) for row in _cache[1]]
    key = config.load().cartesia_key()
    if not key:
        return []
    rows: list[dict] = []
    cursor = ""
    for _ in range(8):
        page = _fetch_page(key, cursor=cursor)
        rows.extend(page.get("data") or [])
        cursor = str(page.get("next_page") or "")
        if not page.get("has_more") or not cursor:
            break
    with _lock:
        _cache = (now, [dict(row) for row in rows])
    return rows


def cached_english_voice(voice_id: str) -> dict | None:
    """Resolve a voice already returned by the catalog without network I/O."""
    with _lock:
        if not _cache:
            return None
        row = next((item for item in _cache[1]
                    if str(item.get("id") or "") == voice_id), None)
        return dict(row) if row else None


def has_cached_catalog() -> bool:
    with _lock:
        return _cache is not None


def catalog(agents_path, *, force: bool = False) -> dict:
    agents = load_agents(agents_path)
    cfg = config.load()
    occupied: dict[str, str] = {}
    for session, info in agents.items():
        voice_id = (resolve_voice((info or {}).get("voice_id"), CARTESIA)
                    or cfg.cartesia_voice_for(str((info or {}).get("name") or "")))
        if voice_id:
            occupied[voice_id] = str((info or {}).get("name") or session)
    voices = []
    for row in english_voices(force=force):
        voice_id = str(row.get("id") or "")
        if not voice_id:
            continue
        voices.append({
            "id": voice_id,
            "name": str(row.get("name") or voice_id),
            "tagline": str(row.get("tagline") or ""),
            "description": str(row.get("description") or ""),
            "gender": str(row.get("gender") or ""),
            "language": str(row.get("language") or ""),
            "country": str(row.get("country") or ""),
            "preview_url": f"/cartesia-voice-preview?id={urllib.parse.quote(voice_id)}",
            "taken_by": occupied.get(voice_id),
            "selection_value": json.dumps({CARTESIA: voice_id}, separators=(",", ":")),
        })
    available = bool(cfg.cartesia_key()) and cfg.tts_provider == CARTESIA
    return {"available": available, "voices": voices if available else []}
